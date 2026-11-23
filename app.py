"""Live contest judge: Flask + Flask-SocketIO.

Run:    python app.py
Setup:  python manage.py createsuperuser        (once)
"""

import os

# Eventlet needs patching before the standard library is used anywhere else.
if os.environ.get("JUDGE_ASYNC", "threading") == "eventlet":
    import eventlet
    eventlet.monkey_patch()

import csv
import hmac
import io
import re
import secrets
import sys
import time
from datetime import timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from flask_socketio import SocketIO, emit, join_room

import contests
import db
import judging
import services as sv
import totp
from judge_engine import selftest
from services import QuestionFileError

LANGUAGES = {
    "c": "C (gcc)",
    "cpp": "C++17 (g++)",
    "java": "Java 17+",
    "python": "Python 3",
    "pypy": "PyPy 3",
}
MAX_CODE_BYTES = 64 * 1024
SUBMIT_COOLDOWN_SEC = 10
STALE_PENDING_SEC = 300          # a verdict still Pending after 5 minutes is considered lost
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SEC = 600
IDLE_TIMEOUT_MIN = int(os.environ.get("JUDGE_IDLE_MIN", "30"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("JUDGE_SECRET") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("JUDGE_HTTPS") == "1"
ASYNC_MODE = os.environ.get("JUDGE_ASYNC", "threading")   # "eventlet" in Docker, "threading" for local runs
socketio = SocketIO(app, async_mode=ASYNC_MODE)
TWO_FA_PENDING_SEC = 300
_login_failures = {}   # "user|ip" -> [fail_count, locked_until]


# ------------------------------------------------------------ plumbing ----

def get_db():
    if "db" not in g:
        g.db = db.get_conn()
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def _check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        expected = session.get("csrf", "")
        if not sent or not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Your form expired. Go back, reload the page and try again.")


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.context_processor
def _inject_nav_title():
    row = db.current_contest(get_db())
    return {"nav_title": row["title"] if row else "Contest Judge"}


# ------------------------------------------------------- contest state ----

def contest_info(conn):
    """The active contest as the templates use it. status: not_configured,
    before, running or ended."""
    row = db.current_contest(conn)
    if row is None:
        return {"id": None, "row": None, "title": "Contest", "start": None, "end": None,
                "status": "not_configured", "max_attempts": contests.DEFAULT_MAX_ATTEMPTS}
    return {
        "id": row["id"], "row": row, "title": row["title"],
        "start": sv.from_iso(row["start_at"]) if row["start_at"] else None,
        "end": sv.from_iso(row["end_at"]) if row["end_at"] else None,
        "status": contests.contest_status(row),
        "max_attempts": row["max_attempts"],
    }


def status_payload(info):
    return {
        "status": info["status"],
        "title": info["title"],
        "start": sv.to_iso(info["start"]) if info["start"] else None,
        "end": sv.to_iso(info["end"]) if info["end"] else None,
        "now": sv.to_iso(sv.utcnow()),  # lets the browser correct for its own clock
    }


def leaderboard_for_active(conn):
    info = contest_info(conn)
    if info["row"] is None:
        return []
    return contests.leaderboard(conn, info["row"])


def push_leaderboard():
    conn = db.get_conn()
    try:
        rows = leaderboard_for_active(conn)
    finally:
        conn.close()
    socketio.emit("leaderboard", rows)


def on_judged(submission_id, user_id):
    """Called from a judge thread when a verdict is written."""
    conn = db.get_conn()
    try:
        s = conn.execute(
            "SELECT id, verdict, passed, total, max_time_ms, compile_output "
            "FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        payload = dict(s)
    finally:
        conn.close()
    socketio.emit("submission_update", payload, to=f"user:{user_id}")
    push_leaderboard()


# ------------------------------------------------------------- auth ----

def current_user():
    """Strict session check, run on every request and socket connection:
      1. the token must be the account's current one (one login per account),
      2. the request must come from the IP the session started from,
      3. the session must have been used within IDLE_TIMEOUT_MIN minutes,
      4. participants must belong to the active contest.
    A failed IP check rejects only this request. The real owner's session survives.
    """
    if "user" in g:
        return g.user
    g.user = None
    g.reason = None
    uid, tok = session.get("uid"), session.get("tok")
    if not (uid and tok):
        return None

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    active = db.current_contest(conn)
    if row is None or row["session_token"] != tok:
        session.clear()
        g.reason = "elsewhere"
    elif row["session_ip"] != request.remote_addr:
        session.clear()
        g.reason = "ip_changed"
    elif not row["session_seen"] or \
            sv.utcnow() - sv.from_iso(row["session_seen"]) > timedelta(minutes=IDLE_TIMEOUT_MIN):
        conn.execute("UPDATE users SET session_token = NULL, session_ip = NULL WHERE id = ?", (uid,))
        conn.commit()
        session.clear()
        g.reason = "idle"
    elif not row["is_admin"] and (active is None or row["contest_id"] != active["id"]):
        session.clear()
        g.reason = "contest_closed"
    else:
        conn.execute("UPDATE users SET session_seen = ? WHERE id = ?", (sv.to_iso(sv.utcnow()), uid))
        conn.commit()
        g.user = row
    return g.user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            if g.reason:
                return redirect(url_for("login", reason=g.reason))
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    return redirect(url_for("admin_dashboard") if user["is_admin"] else url_for("problems"))


def start_session(conn, row):
    """Log the account in on this browser. Replaces any session the account had elsewhere."""
    token = db.new_session_token()
    now = sv.to_iso(sv.utcnow())
    conn.execute(
        "UPDATE users SET session_token = ?, session_ip = ?, session_ua = ?, "
        "session_started = ?, session_seen = ? WHERE id = ?",
        (token, request.remote_addr, (request.user_agent.string or "")[:200], now, now, row["id"]),
    )
    conn.commit()
    session.clear()
    session["uid"] = row["id"]
    session["tok"] = token
    nxt = request.args.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("index"))


def _locked(key):
    fails, locked_until = _login_failures.get(key, [0, 0])
    if locked_until > time.time():
        return int((locked_until - time.time()) // 60) + 1
    return 0


def _record_failure(key):
    fails, _ = _login_failures.get(key, [0, 0])
    fails += 1
    _login_failures[key] = [fails, time.time() + LOGIN_LOCK_SEC if fails >= LOGIN_MAX_FAILS else 0]


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        key = f"{username.lower()}|{request.remote_addr}"
        wait = _locked(key)
        if wait:
            return render_template("login.html", error=f"Too many failed attempts. Try again in about {wait} min.",
                                   reason=None), 429
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None or not db.verify_password(row["password_hash"], password):
            _record_failure(key)
            return render_template("login.html", error="Invalid username or password.", reason=None), 401
        _login_failures.pop(key, None)

        if row["is_admin"] and row["totp_enabled"]:
            # Password is correct. Admins with 2FA must also enter a code.
            session.clear()
            session["pending_uid"] = row["id"]
            session["pending_at"] = time.time()
            return redirect(url_for("login_2fa"))

        active = db.current_contest(conn)
        if not row["is_admin"] and (active is None or row["contest_id"] != active["id"]):
            return render_template("login.html", error="This account is not part of the active contest.",
                                   reason=None), 403
        return start_session(conn, row)
    return render_template("login.html", error=None, reason=request.args.get("reason"))


@app.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    uid = session.get("pending_uid")
    if not uid or time.time() - session.get("pending_at", 0) > TWO_FA_PENDING_SEC:
        session.pop("pending_uid", None)
        return redirect(url_for("login"))
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ? AND is_admin = 1 AND totp_enabled = 1",
                       (uid,)).fetchone()
    if row is None:
        session.clear()
        return redirect(url_for("login"))
    key = f"2fa|{uid}|{request.remote_addr}"
    if request.method == "POST":
        wait = _locked(key)
        if wait:
            return render_template("login_2fa.html", error=f"Too many wrong codes. Try again in about {wait} min."), 429
        counter = totp.verify(row["totp_secret"], request.form.get("code", ""),
                              last_counter=row["totp_last"] if row["totp_last"] is not None else -1)
        if counter is None:
            _record_failure(key)
            return render_template("login_2fa.html", error="That code is not right. Check your phone and try again."), 401
        _login_failures.pop(key, None)
        conn.execute("UPDATE users SET totp_last = ? WHERE id = ?", (counter, uid))
        conn.commit()
        session.pop("pending_uid", None)
        session.pop("pending_at", None)
        return start_session(conn, row)
    return render_template("login_2fa.html", error=None)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    conn = get_db()
    conn.execute("UPDATE users SET session_token = NULL, session_ip = NULL WHERE id = ?", (g.user["id"],))
    conn.commit()
    session.clear()
    return redirect(url_for("login"))


# -------------------------------------------------------- participant ----

EMPTY_PROGRESS = {"solved": False, "solved_min": None, "wrong": 0, "attempts": 0, "pending": False}


def user_progress(conn, uid, start):
    rows = conn.execute(
        "SELECT problem_id, verdict, submitted_at FROM submissions "
        "WHERE user_id = ? ORDER BY submitted_at, id", (uid,)
    ).fetchall()
    return sv.progress_for([dict(r) for r in rows], start)


@app.route("/problems")
@login_required
def problems():
    conn = get_db()
    info = contest_info(conn)
    problem_list, progress = [], {}
    if info["status"] in ("running", "ended"):
        problem_list = contests.problem_list(conn, info["id"])
        progress = user_progress(conn, g.user["id"], info["start"])
    return render_template("problems.html", info=info, problems=problem_list,
                           progress=progress, limit=info["max_attempts"],
                           status=status_payload(info))


@app.route("/problem/<int:pid>")
@login_required
def problem(pid):
    conn = get_db()
    info = contest_info(conn)
    if info["status"] not in ("running", "ended"):
        return redirect(url_for("problems"))
    p = conn.execute("SELECT * FROM problems WHERE id = ? AND contest_id = ?",
                     (pid, info["id"])).fetchone()
    if p is None:
        abort(404)
    samples = conn.execute(
        "SELECT input, output FROM test_cases WHERE problem_id = ? AND is_sample = 1 ORDER BY id",
        (pid,),
    ).fetchall()
    pr = user_progress(conn, g.user["id"], info["start"]).get(pid, EMPTY_PROGRESS)
    limit = info["max_attempts"]
    if info["status"] != "running":
        closed = "The contest has ended. Submissions are closed."
    elif pr["solved"]:
        closed = "You already solved this problem. Each problem can be solved once."
    elif limit and pr["attempts"] >= limit:
        closed = f"You have used all {limit} attempts for this problem."
    else:
        closed = None
    return render_template("problem.html", problem=p, samples=samples, languages=LANGUAGES,
                           info=info, can_submit=closed is None, closed=closed,
                           pr=pr, limit=limit, status=status_payload(info))


@app.route("/submit/<int:pid>", methods=["POST"])
@login_required
def submit(pid):
    conn = get_db()
    info = contest_info(conn)
    back = url_for("problem", pid=pid)
    if info["status"] != "running":
        flash("The contest is not running right now.", "error")
        return redirect(back)

    lang = request.form.get("language", "")
    code = request.form.get("code", "")
    if lang not in LANGUAGES:
        flash("Unsupported language.", "error")
        return redirect(back)
    if not code.strip():
        flash("Code cannot be empty.", "error")
        return redirect(back)
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        flash("Code is larger than 64 KB.", "error")
        return redirect(back)
    if conn.execute("SELECT 1 FROM problems WHERE id = ? AND contest_id = ?",
                    (pid, info["id"])).fetchone() is None:
        abort(404)

    uid = g.user["id"]
    prog = user_progress(conn, uid, info["start"]).get(pid, EMPTY_PROGRESS)
    if prog["solved"]:
        flash("You already solved this problem.", "error")
        return redirect(back)
    limit = info["max_attempts"]
    if limit and prog["attempts"] >= limit:
        flash(f"You have used all {limit} attempts for this problem.", "error")
        return redirect(back)

    # A verdict stuck in Pending for too long means judging was lost. Close it out.
    stale_before = sv.to_iso(sv.utcnow() - timedelta(seconds=STALE_PENDING_SEC))
    conn.execute(
        "UPDATE submissions SET verdict = 'Internal Error', "
        "compile_output = 'Judging was interrupted. Please resubmit.' "
        "WHERE user_id = ? AND verdict = 'Pending' AND submitted_at < ?",
        (uid, stale_before),
    )
    conn.commit()
    if conn.execute("SELECT id FROM submissions WHERE user_id = ? AND verdict = 'Pending'",
                    (uid,)).fetchone():
        flash("Your previous submission is still being judged. Wait a few seconds, "
              "then try again. It appears on My submissions.", "error")
        return redirect(back)

    now = sv.utcnow()
    last = conn.execute(
        "SELECT submitted_at FROM submissions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,)
    ).fetchone()
    if last and (now - sv.from_iso(last["submitted_at"])).total_seconds() < SUBMIT_COOLDOWN_SEC:
        flash(f"Please wait {SUBMIT_COOLDOWN_SEC} seconds between submissions.", "error")
        return redirect(back)

    digest = sv.code_hash(code)
    dup = conn.execute(
        "SELECT id, verdict FROM submissions WHERE user_id = ? AND problem_id = ? "
        "AND code_hash = ? AND verdict != 'Internal Error' ORDER BY id DESC LIMIT 1",
        (uid, pid, digest),
    ).fetchone()
    if dup:
        flash(f"You already submitted this exact code for this problem (submission #{dup['id']}: "
              f"{dup['verdict']}). Change your code before submitting again.", "error")
        return redirect(back)

    cur = conn.execute(
        "INSERT INTO submissions(user_id, problem_id, language, code, code_hash, verdict, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, 'Pending', ?)",
        (uid, pid, lang, code, digest, sv.to_iso(now)),
    )
    conn.commit()
    judging.submit_judging(cur.lastrowid, on_judged)
    flash("Submitted. Judging...", "ok")
    return redirect(url_for("my_submissions"))


@app.route("/submissions")
@login_required
def my_submissions():
    conn = get_db()
    rows = conn.execute(
        "SELECT s.id, s.language, s.verdict, s.passed, s.total, s.max_time_ms, "
        "s.compile_output, s.submitted_at, p.position, p.title "
        "FROM submissions s JOIN problems p ON p.id = s.problem_id "
        "WHERE s.user_id = ? ORDER BY s.id DESC", (g.user["id"],)
    ).fetchall()
    has_pending = any(r["verdict"] == "Pending" for r in rows)
    return render_template("submissions.html", submissions=rows, languages=LANGUAGES,
                           info=contest_info(conn), has_pending=has_pending)


@app.route("/leaderboard")
@login_required
def leaderboard():
    conn = get_db()
    info = contest_info(conn)
    return render_template("leaderboard.html", rows=leaderboard_for_active(conn),
                           info=info, status=status_payload(info))


# --------------------------------------------------------- admin: dashboard ----

def _to_local_input(d):
    return d.astimezone().strftime("%Y-%m-%dT%H:%M") if d else ""


@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    info = contest_info(conn)
    cid = info["id"]
    counts = {"users": 0, "problems": 0, "submissions": 0}
    recent = []
    if cid:
        counts = {
            "users": conn.execute("SELECT COUNT(*) FROM users WHERE contest_id = ?", (cid,)).fetchone()[0],
            "problems": conn.execute("SELECT COUNT(*) FROM problems WHERE contest_id = ?", (cid,)).fetchone()[0],
            "submissions": conn.execute(
                "SELECT COUNT(*) FROM submissions s JOIN problems p ON p.id = s.problem_id "
                "WHERE p.contest_id = ?", (cid,)).fetchone()[0],
        }
        recent = conn.execute(
            "SELECT s.id, s.language, s.verdict, s.submitted_at, u.username, p.title "
            "FROM submissions s JOIN users u ON u.id = s.user_id JOIN problems p ON p.id = s.problem_id "
            "WHERE p.contest_id = ? ORDER BY s.id DESC LIMIT 30", (cid,)).fetchall()
    return render_template("admin_dashboard.html", info=info, counts=counts, recent=recent,
                           start_input=_to_local_input(info["start"]),
                           end_input=_to_local_input(info["end"]),
                           status=status_payload(info))


@app.route("/admin/contest", methods=["POST"])
@admin_required
def admin_save_contest():
    conn = get_db()
    info = contest_info(conn)
    if info["id"] is None:
        flash("No active contest.", "error")
        return redirect(url_for("admin_contests"))
    try:
        contests.update_contest(conn, info["id"], request.form.get("title", ""),
                                request.form.get("start", ""), request.form.get("end", ""),
                                request.form.get("max_attempts", "0"))
        flash("Contest saved. Participants see changes live.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_dashboard"))


# ------------------------------------------------------- admin: contests ----

@app.route("/admin/contests")
@admin_required
def admin_contests():
    conn = get_db()
    return render_template("admin_contests.html", contests=contests.list_contests(conn),
                           active=db.current_contest(conn), status=status_payload(contest_info(conn)))


@app.route("/admin/contests/new", methods=["POST"])
@admin_required
def admin_new_contest():
    conn = get_db()
    try:
        contests.create_contest(conn, request.form.get("title", ""), request.form.get("start", ""),
                                request.form.get("end", ""), request.form.get("max_attempts", "30"))
        flash("New contest created. Next: import its problems and add its participants.", "ok")
        return redirect(url_for("admin_dashboard"))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("admin_contests"))


@app.route("/admin/contests/<int:cid>")
@admin_required
def admin_contest_detail(cid):
    conn = get_db()
    row = contests.get_contest(conn, cid)
    if row is None:
        abort(404)
    subs = conn.execute(
        "SELECT s.id, s.verdict, s.language, s.submitted_at, u.username, p.title "
        "FROM submissions s JOIN users u ON u.id = s.user_id JOIN problems p ON p.id = s.problem_id "
        "WHERE p.contest_id = ? ORDER BY s.id DESC LIMIT 300", (cid,)).fetchall()
    return render_template("admin_contest_detail.html", c=row,
                           c_status=contests.contest_status(row),
                           board=contests.leaderboard(conn, row), subs=subs,
                           problems=contests.problem_list(conn, cid),
                           status=status_payload(contest_info(conn)))


@app.route("/admin/contests/<int:cid>/end", methods=["POST"])
@admin_required
def admin_end_contest(cid):
    try:
        contests.end_contest(get_db(), cid)
        flash("Contest ended. Submissions are closed. You can now export and archive it.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_contest_detail", cid=cid))


@app.route("/admin/contests/<int:cid>/archive", methods=["POST"])
@admin_required
def admin_archive_contest(cid):
    try:
        contests.archive_contest(get_db(), cid)
        flash("Contest archived. Its participants are signed out and its data is kept read-only.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_contest_detail", cid=cid))


@app.route("/admin/contests/<int:cid>/export")
@admin_required
def admin_export_contest(cid):
    conn = get_db()
    row = contests.get_contest(conn, cid)
    if row is None:
        abort(404)
    data = contests.build_export(conn, row)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", row["title"]).strip("-") or "contest"
    return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True,
                     download_name=f"contest-{cid}-{slug}.zip")


# ----------------------------------------------------- admin: participants ----

def _participants_page(conn, info, created=None, errors=()):
    return render_template("admin_users.html", users=contests.participants(conn, info["id"]),
                           created=created, errors=list(errors), info=info,
                           status=status_payload(info))


@app.route("/admin/users")
@admin_required
def admin_users():
    conn = get_db()
    info = contest_info(conn)
    if info["id"] is None:
        flash("No active contest. Start one on the Contests page.", "error")
        return redirect(url_for("admin_contests"))
    return _participants_page(conn, info)


@app.route("/admin/users/add", methods=["POST"])
@admin_required
def admin_add_user():
    conn = get_db()
    info = contest_info(conn)
    if info["id"] is None:
        return redirect(url_for("admin_contests"))
    username = request.form.get("username", "").strip()
    try:
        pw = contests.create_participant(conn, info["id"], username, request.form.get("full_name", ""),
                                         request.form.get("password", ""))
        conn.commit()
        return _participants_page(conn, info, created=[(username, pw)])
    except ValueError as e:
        conn.rollback()
        return _participants_page(conn, info, errors=[str(e)])


@app.route("/admin/users/bulk", methods=["POST"])
@admin_required
def admin_bulk_users():
    """One participant per line: username,full name,password (password optional)."""
    conn = get_db()
    info = contest_info(conn)
    if info["id"] is None:
        return redirect(url_for("admin_contests"))
    created, errors = [], []
    for lineno, row in enumerate(csv.reader(io.StringIO(request.form.get("lines", ""))), start=1):
        if not row or not "".join(row).strip() or row[0].strip().startswith("#"):
            continue
        username = row[0].strip()
        full_name = row[1].strip() if len(row) > 1 else ""
        password = row[2].strip() if len(row) > 2 else ""
        try:
            created.append((username, contests.create_participant(conn, info["id"], username,
                                                                  full_name, password)))
        except ValueError as e:
            errors.append(f"line {lineno}: {e}")
    conn.commit()
    return _participants_page(conn, info, created=created, errors=errors)


def _participant(conn, uid):
    """A participant of the ACTIVE contest, or 404. Archived contests are read-only."""
    info = contest_info(conn)
    row = conn.execute("SELECT * FROM users WHERE id = ? AND contest_id = ? AND is_admin = 0",
                       (uid, info["id"])).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/admin/users/<int:uid>/reset", methods=["POST"])
@admin_required
def admin_reset_password(uid):
    conn = get_db()
    row = _participant(conn, uid)
    pw = request.form.get("password", "").strip() or secrets.token_urlsafe(6)
    if len(pw) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("admin_users"))
    # Clearing the token also signs the user out of any current session.
    conn.execute("UPDATE users SET password_hash = ?, session_token = NULL WHERE id = ?",
                 (db.hash_password(pw), uid))
    conn.commit()
    flash(f"New password for {row['username']}: {pw}", "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_delete_user(uid):
    conn = get_db()
    _participant(conn, uid)
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    flash("Participant deleted.", "ok")
    return redirect(url_for("admin_users"))


# ------------------------------------------------------- admin: problems ----

@app.route("/admin/problems")
@admin_required
def admin_problems():
    conn = get_db()
    info = contest_info(conn)
    if info["id"] is None:
        flash("No active contest. Start one on the Contests page.", "error")
        return redirect(url_for("admin_contests"))
    return render_template("admin_problems.html", problems=contests.problem_list(conn, info["id"]),
                           info=info, status=status_payload(info))


@app.route("/admin/problems/import", methods=["POST"])
@admin_required
def admin_import_problems():
    conn = get_db()
    info = contest_info(conn)
    if info["id"] is None:
        return redirect(url_for("admin_contests"))
    upload = request.files.get("file")
    try:
        if upload and upload.filename:
            text = upload.read().decode("utf-8-sig")
        else:
            text = request.form.get("json_text", "")
    except UnicodeDecodeError:
        flash("The file must be UTF-8 encoded JSON.", "error")
        return redirect(url_for("admin_problems"))
    if not text.strip():
        flash("Choose a JSON file or paste JSON.", "error")
        return redirect(url_for("admin_problems"))
    try:
        n = contests.import_problems(conn, info["id"], text)
        flash(f"Imported {n} problem(s) into this contest.", "ok")
    except (QuestionFileError, ValueError) as e:
        flash(f"Import failed, nothing was saved: {e}", "error")
    return redirect(url_for("admin_problems"))


@app.route("/admin/problems/<int:pid>/delete", methods=["POST"])
@admin_required
def admin_delete_problem(pid):
    conn = get_db()
    info = contest_info(conn)
    if info["id"] is None:
        abort(404)
    cur = conn.execute("DELETE FROM problems WHERE id = ? AND contest_id = ?", (pid, info["id"]))
    conn.commit()
    if cur.rowcount == 0:
        abort(404)
    flash("Problem deleted.", "ok")
    return redirect(url_for("admin_problems"))


@app.route("/admin/submissions/<int:sid>")
@admin_required
def admin_submission(sid):
    conn = get_db()
    s = conn.execute(
        "SELECT s.*, u.username, p.title FROM submissions s "
        "JOIN users u ON u.id = s.user_id JOIN problems p ON p.id = s.problem_id WHERE s.id = ?", (sid,)
    ).fetchone()
    if s is None:
        abort(404)
    return render_template("admin_submission.html", s=s, languages=LANGUAGES,
                           status=status_payload(contest_info(conn)))


# ------------------------------------------------------- admin: security ----

@app.route("/admin/security")
@admin_required
def admin_security():
    return render_template("admin_security.html", enabled=bool(g.user["totp_enabled"]),
                           pending_secret=session.get("totp_setup"),
                           status=status_payload(contest_info(get_db())))


@app.route("/admin/security/2fa/setup", methods=["POST"])
@admin_required
def admin_2fa_setup():
    if g.user["totp_enabled"]:
        flash("Two-factor login is already on.", "error")
        return redirect(url_for("admin_security"))
    session["totp_setup"] = totp.new_secret()
    return redirect(url_for("admin_security"))


@app.route("/admin/security/2fa/enable", methods=["POST"])
@admin_required
def admin_2fa_enable():
    secret = session.get("totp_setup")
    if not secret:
        flash("Start the setup again.", "error")
        return redirect(url_for("admin_security"))
    counter = totp.verify(secret, request.form.get("code", ""))
    if counter is None:
        flash("That code is not right. Check the time on your phone and try again.", "error")
        return redirect(url_for("admin_security"))
    conn = get_db()
    conn.execute("UPDATE users SET totp_secret = ?, totp_enabled = 1, totp_last = ? WHERE id = ?",
                 (secret, counter, g.user["id"]))
    conn.commit()
    session.pop("totp_setup", None)
    flash("Two-factor login is on. You will need a code from your phone at every admin login.", "ok")
    return redirect(url_for("admin_security"))


@app.route("/admin/security/2fa/disable", methods=["POST"])
@admin_required
def admin_2fa_disable():
    password = request.form.get("password", "")
    code = request.form.get("code", "")
    if not db.verify_password(g.user["password_hash"], password):
        flash("Password is wrong.", "error")
        return redirect(url_for("admin_security"))
    if totp.verify(g.user["totp_secret"], code, last_counter=g.user["totp_last"] or -1) is None:
        flash("Code is wrong.", "error")
        return redirect(url_for("admin_security"))
    conn = get_db()
    conn.execute("UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_last = NULL WHERE id = ?",
                 (g.user["id"],))
    conn.commit()
    flash("Two-factor login is off.", "ok")
    return redirect(url_for("admin_security"))


# ------------------------------------------------------- admin: sessions ----

@app.route("/admin/sessions")
@admin_required
def admin_sessions():
    conn = get_db()
    info = contest_info(conn)
    if info["id"] is None:
        flash("No active contest. Start one on the Contests page.", "error")
        return redirect(url_for("admin_contests"))
    now = sv.utcnow()
    rows = []
    for r in conn.execute(
        "SELECT id, username, full_name, is_admin, session_ip, session_ua, session_started, session_seen "
        "FROM users WHERE session_token IS NOT NULL AND (contest_id = ? OR is_admin = 1) "
        "ORDER BY session_seen DESC", (info["id"],)
    ):
        seen = sv.from_iso(r["session_seen"]) if r["session_seen"] else now
        rows.append({
            "id": r["id"], "username": r["username"], "full_name": r["full_name"] or "",
            "is_admin": r["is_admin"], "ip": r["session_ip"] or "-", "ua": (r["session_ua"] or "")[:60],
            "started": (r["session_started"] or "")[11:16], "seen": (r["session_seen"] or "")[11:19],
            "idle_min": int((now - seen).total_seconds() // 60),
        })
    return render_template("admin_sessions.html", sessions=rows, idle_limit=IDLE_TIMEOUT_MIN,
                           info=info, status=status_payload(info))


@app.route("/admin/sessions/<int:uid>/kick", methods=["POST"])
@admin_required
def admin_kick_session(uid):
    conn = get_db()
    conn.execute("UPDATE users SET session_token = NULL, session_ip = NULL WHERE id = ?", (uid,))
    conn.commit()
    flash("Session ended.", "ok")
    return redirect(url_for("admin_sessions"))


# ---------------------------------------------------------- socket.io ----

@socketio.on("connect")
def on_connect():
    user = current_user()
    if user is None:
        return False  # reject anonymous sockets
    join_room(f"user:{user['id']}")
    conn = get_db()
    info = contest_info(conn)
    emit("contest_status", status_payload(info))
    emit("leaderboard", leaderboard_for_active(conn))


_last_fingerprint = {"value": None}


def _status_ticker():
    """Watches the active contest and pushes changes to everyone connected,
    so the problem list unlocks at the start time without a page refresh."""
    while True:
        socketio.sleep(2)
        conn = db.get_conn()
        try:
            info = contest_info(conn)
        finally:
            conn.close()
        payload = status_payload(info)
        fp = (info["id"], payload["status"], payload["start"], payload["end"], payload["title"])
        if fp != _last_fingerprint["value"]:
            first_run = _last_fingerprint["value"] is None
            _last_fingerprint["value"] = fp
            if not first_run:
                socketio.emit("contest_status", payload)
                push_leaderboard()


# ---------------------------------------------------------------- main ----

def main():
    db.init_db()
    backend, checks = selftest.run_selftest()
    print(f"Sandbox backend: {backend or 'NONE'}")
    for c in checks:
        print(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    if not all(c.passed for c in checks):
        if backend == "unshare" and os.environ.get("JUDGE_ALLOW_WEAK_SANDBOX") == "1":
            print("WARNING: weak sandbox allowed by JUDGE_ALLOW_WEAK_SANDBOX=1. "
                  "Host files are visible to submitted code.")
        elif backend == "none" and os.environ.get("JUDGE_ALLOW_UNSAFE") == "1":
            print("WARNING: JUDGE_ALLOW_UNSAFE=1. Submitted code runs with NO isolation.")
        else:
            sys.exit("Refusing to start: the sandbox failed its self-test. "
                     "Install bubblewrap (apt install bubblewrap) and try again.")
    judging.init_executor(int(os.environ.get("JUDGE_WORKERS", "3")))
    requeued = judging.requeue_pending(on_judged)
    if requeued:
        print(f"Re-queued {requeued} submission(s) left Pending by a previous run.")
    static_js = os.path.join(app.static_folder, "socket.io.min.js")
    if not os.path.exists(static_js):
        print("WARNING: static/socket.io.min.js is missing. Live updates will not work.\n"
              "  Download it once:\n"
              "  curl -o static/socket.io.min.js https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js")
    socketio.start_background_task(_status_ticker)
    port = int(os.environ.get("JUDGE_PORT", "8000"))
    if "JUDGE_SECRET" not in os.environ:
        print("WARNING: JUDGE_SECRET not set; logins reset on every restart.")
    print(f"Contest judge on http://0.0.0.0:{port}")
    if ASYNC_MODE == "eventlet":
        socketio.run(app, host="0.0.0.0", port=port)            # eventlet WSGI server, no gunicorn needed
    else:
        socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)  # local development


if __name__ == "__main__":
    main()
