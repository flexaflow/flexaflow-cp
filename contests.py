"""Contest lifecycle, participants, problems, leaderboard and export.

Flask-free, so every rule here can be tested directly. app.py only handles
requests and calls these functions.
"""

import csv
import datetime as dt
import io
import json
import secrets
import zipfile

import db
import services as sv
from services import QuestionFileError, validate_questions

EXT = {"c": "c", "cpp": "cpp", "java": "java", "python": "py", "pypy": "py"}
DEFAULT_MAX_ATTEMPTS = 30  # per problem per participant; 0 = unlimited


# ---------------------------------------------------------- lifecycle ----

def contest_status(row, now=None) -> str:
    """'archived', 'not_configured', 'before', 'running' or 'ended'."""
    if row is None:
        return "not_configured"
    if row["archived"]:
        return "archived"
    start = sv.from_iso(row["start_at"]) if row["start_at"] else None
    end = sv.from_iso(row["end_at"]) if row["end_at"] else None
    return sv.contest_status(start, end, now)


def _parse_times(start_text, end_text):
    try:
        start = sv.parse_local_datetime(start_text)
        end = sv.parse_local_datetime(end_text)
    except ValueError:
        raise ValueError("Enter a valid start and end date and time.")
    if end <= start:
        raise ValueError("End time must be after start time.")
    return sv.to_iso(start), sv.to_iso(end)


def _check_attempts(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = -1
    if not 0 <= n <= 1000:
        raise ValueError("Attempts per problem must be a number from 0 to 1000 (0 = unlimited).")
    return n


def create_contest(conn, title, start_text, end_text, max_attempts):
    if db.current_contest(conn) is not None:
        raise ValueError("A contest is already active. End and archive it before starting a new one.")
    title = (title or "").strip() or "Contest"
    start_iso, end_iso = _parse_times(start_text, end_text)
    cur = conn.execute(
        "INSERT INTO contests(title, start_at, end_at, max_attempts, created_at) VALUES (?, ?, ?, ?, ?)",
        (title, start_iso, end_iso, _check_attempts(max_attempts), sv.to_iso(sv.utcnow())),
    )
    conn.commit()
    return cur.lastrowid


def update_contest(conn, cid, title, start_text, end_text, max_attempts):
    row = get_contest(conn, cid)
    if row is None or row["archived"]:
        raise ValueError("Only the active contest can be changed.")
    start_iso, end_iso = _parse_times(start_text, end_text)
    conn.execute(
        "UPDATE contests SET title = ?, start_at = ?, end_at = ?, max_attempts = ? WHERE id = ?",
        ((title or "").strip() or "Contest", start_iso, end_iso, _check_attempts(max_attempts), cid),
    )
    conn.commit()


def end_contest(conn, cid):
    """Stop the contest now. Submissions close immediately."""
    row = get_contest(conn, cid)
    if contest_status(row) != "running":
        raise ValueError("Only a running contest can be ended.")
    conn.execute("UPDATE contests SET end_at = ? WHERE id = ?", (sv.to_iso(sv.utcnow()), cid))
    conn.commit()


def archive_contest(conn, cid):
    """Make the contest read-only and sign out its participants.
    Data is kept, so it can still be viewed and exported later."""
    row = get_contest(conn, cid)
    if contest_status(row) != "ended":
        raise ValueError("Only an ended contest can be archived. End it first.")
    conn.execute("UPDATE contests SET archived = 1 WHERE id = ?", (cid,))
    conn.execute(
        "UPDATE users SET session_token = NULL, session_ip = NULL WHERE contest_id = ?", (cid,))
    conn.commit()


def get_contest(conn, cid):
    return conn.execute("SELECT * FROM contests WHERE id = ?", (cid,)).fetchone()


def list_contests(conn):
    rows = conn.execute("SELECT * FROM contests ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = contest_status(r)
        d["problems"] = conn.execute("SELECT COUNT(*) FROM problems WHERE contest_id = ?", (r["id"],)).fetchone()[0]
        d["participants"] = conn.execute(
            "SELECT COUNT(*) FROM users WHERE contest_id = ?", (r["id"],)).fetchone()[0]
        d["submissions"] = conn.execute(
            "SELECT COUNT(*) FROM submissions s JOIN problems p ON p.id = s.problem_id "
            "WHERE p.contest_id = ?", (r["id"],)).fetchone()[0]
        out.append(d)
    return out


# ---------------------------------------------------------- participants ----

def create_participant(conn, cid, username, full_name, password):
    """Returns the password actually set (generated if none was given)."""
    username = (username or "").strip()
    if not sv.USERNAME_RE.match(username):
        raise ValueError(f"'{username}': use 3-32 letters, digits, _ . or -")
    password = (password or "").strip() or secrets.token_urlsafe(6)
    if len(password) < 6:
        raise ValueError(f"'{username}': password must be at least 6 characters")
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise ValueError(f"'{username}' already exists. Usernames are unique across all contests.")
    conn.execute(
        "INSERT INTO users(username, password_hash, full_name, is_admin, contest_id) VALUES (?, ?, ?, 0, ?)",
        (username, db.hash_password(password), (full_name or "").strip(), cid),
    )
    return password


def participants(conn, cid):
    return conn.execute(
        "SELECT u.id, u.username, u.full_name, u.session_token IS NOT NULL AS online, "
        "(SELECT COUNT(*) FROM submissions s WHERE s.user_id = u.id) AS subs "
        "FROM users u WHERE u.contest_id = ? ORDER BY u.username", (cid,)
    ).fetchall()


# ------------------------------------------------------------ problems ----

def import_problems(conn, cid, raw_text) -> int:
    row = get_contest(conn, cid)
    if row is None or row["archived"]:
        raise ValueError("Only the active contest can receive problems.")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise QuestionFileError(f"Not valid JSON: {e}")
    problems = validate_questions(data)
    start_pos = conn.execute(
        "SELECT COALESCE(MAX(position), 0) FROM problems WHERE contest_id = ?", (cid,)).fetchone()[0]
    try:
        for offset, p in enumerate(problems, start=1):
            cur = conn.execute(
                "INSERT INTO problems(contest_id, position, title, statement, time_limit_sec, mem_limit_mb) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cid, start_pos + offset, p["title"], p["statement"], p["time_limit_sec"], p["mem_limit_mb"]),
            )
            conn.executemany(
                "INSERT INTO test_cases(problem_id, input, output, is_sample) VALUES (?, ?, ?, ?)",
                [(cur.lastrowid, t["input"], t["output"], t["sample"]) for t in p["tests"]],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(problems)


def problem_list(conn, cid):
    return conn.execute(
        "SELECT p.id, p.position, p.title, p.time_limit_sec, p.mem_limit_mb, "
        "(SELECT COUNT(*) FROM test_cases t WHERE t.problem_id = p.id) AS tests, "
        "(SELECT COUNT(*) FROM test_cases t WHERE t.problem_id = p.id AND t.is_sample = 1) AS samples "
        "FROM problems p WHERE p.contest_id = ? ORDER BY p.position", (cid,)
    ).fetchall()


# --------------------------------------------------------- leaderboard ----

def leaderboard(conn, contest_row):
    users = conn.execute(
        "SELECT id, username, full_name FROM users WHERE contest_id = ? AND is_admin = 0",
        (contest_row["id"],)).fetchall()
    subs = conn.execute(
        "SELECT s.user_id, s.problem_id, s.verdict, s.submitted_at FROM submissions s "
        "JOIN problems p ON p.id = s.problem_id WHERE p.contest_id = ? "
        "ORDER BY s.submitted_at, s.id", (contest_row["id"],)).fetchall()
    start = sv.from_iso(contest_row["start_at"]) if contest_row["start_at"] else None
    end = sv.from_iso(contest_row["end_at"]) if contest_row["end_at"] else None
    return sv.compute_leaderboard([dict(u) for u in users], [dict(s) for s in subs], start, end)


# -------------------------------------------------------------- export ----

def build_export(conn, contest_row) -> bytes:
    """A zip with everything about one contest:
      contest.json      title, schedule and the problemset (same format as the import file)
      leaderboard.csv   final standings
      participants.csv  who took part and how many submissions each made (no passwords)
      submissions.csv   every submission
      code/             the source code of every submission, one file each
    """
    cid = contest_row["id"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        problems = []
        for p in conn.execute("SELECT * FROM problems WHERE contest_id = ? ORDER BY position", (cid,)):
            tests = conn.execute(
                "SELECT input, output, is_sample FROM test_cases WHERE problem_id = ? ORDER BY id",
                (p["id"],)).fetchall()
            problems.append({
                "title": p["title"], "statement": p["statement"],
                "time_limit_sec": p["time_limit_sec"], "mem_limit_mb": p["mem_limit_mb"],
                "tests": [{"input": t["input"], "output": t["output"], "sample": bool(t["is_sample"])}
                          for t in tests],
            })
        z.writestr("contest.json", json.dumps({
            "title": contest_row["title"], "start": contest_row["start_at"],
            "end": contest_row["end_at"], "max_attempts": contest_row["max_attempts"],
            "problems": problems,
        }, indent=2, ensure_ascii=False))

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["rank", "username", "full_name", "solved", "penalty_min"])
        for r in leaderboard(conn, contest_row):
            w.writerow([r["rank"], r["username"], r["full_name"], r["solved"], r["penalty"]])
        z.writestr("leaderboard.csv", out.getvalue())

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["username", "full_name", "submissions"])
        for u in participants(conn, cid):
            w.writerow([u["username"], u["full_name"] or "", u["subs"]])
        z.writestr("participants.csv", out.getvalue())

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["id", "username", "problem", "language", "verdict", "passed", "total",
                    "max_time_ms", "submitted_at_utc"])
        subs = conn.execute(
            "SELECT s.*, u.username, p.title, p.position FROM submissions s "
            "JOIN users u ON u.id = s.user_id JOIN problems p ON p.id = s.problem_id "
            "WHERE p.contest_id = ? ORDER BY s.id", (cid,)).fetchall()
        for s in subs:
            w.writerow([s["id"], s["username"], s["title"], s["language"], s["verdict"],
                        s["passed"], s["total"], round(s["max_time_ms"]), s["submitted_at"]])
            ext = EXT.get(s["language"], "txt")
            name = f"code/{s['id']}_{s['username']}_{'ABCDEFGHIJKLMNOPQRSTUVWXYZ'[(s['position'] - 1) % 26]}.{ext}"
            z.writestr(name, s["code"])
        z.writestr("submissions.csv", out.getvalue())
    return buf.getvalue()
