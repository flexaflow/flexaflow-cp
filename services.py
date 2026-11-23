"""Pure logic: contest timing, question-JSON validation, ICPC leaderboard.

Nothing in this file imports Flask, so it can be unit-tested with the stdlib.
"""

import datetime as dt
import re

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
PENALTY_PER_WRONG_MIN = 20
SUPPORTED_LANGUAGES = ["c", "cpp", "java", "python", "pypy"]


# ------------------------------------------------------------ time ----

def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_local_datetime(value: str) -> dt.datetime:
    """Parse an admin-entered 'YYYY-MM-DDTHH:MM' (or with seconds) as the
    server's local time and return an aware UTC datetime."""
    naive = dt.datetime.fromisoformat(value.strip())
    if naive.tzinfo is None:
        naive = naive.astimezone()  # attach the server's local timezone
    return naive.astimezone(dt.timezone.utc)


def to_iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat()


def from_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def contest_status(start: dt.datetime | None, end: dt.datetime | None,
                   now: dt.datetime | None = None) -> str:
    """Returns 'not_configured', 'before', 'running' or 'ended'."""
    if start is None or end is None:
        return "not_configured"
    now = now or utcnow()
    if now < start:
        return "before"
    if now < end:
        return "running"
    return "ended"


# ------------------------------------------------- question JSON import ----

class QuestionFileError(ValueError):
    pass


def validate_questions(data) -> list[dict]:
    """Validate the question file and return normalized problem dicts.

    Expected shape:
    {
      "problems": [
        {
          "title": "A + B",
          "statement": "...",
          "time_limit_sec": 2,        (optional, default 2)
          "mem_limit_mb": 256,        (optional, default 256)
          "tests": [
            {"input": "3 4\\n", "output": "7\\n", "sample": true},
            {"input": "10 20\\n", "output": "30\\n"}
          ]
        }
      ]
    }
    """
    if not isinstance(data, dict) or not isinstance(data.get("problems"), list):
        raise QuestionFileError('Top level must be an object with a "problems" list.')
    if not data["problems"]:
        raise QuestionFileError('"problems" is empty.')

    out = []
    for idx, p in enumerate(data["problems"], start=1):
        where = f"problem #{idx}"
        if not isinstance(p, dict):
            raise QuestionFileError(f"{where}: must be an object.")
        title = str(p.get("title", "")).strip()
        statement = str(p.get("statement", "")).strip()
        if not title or not statement:
            raise QuestionFileError(f"{where}: 'title' and 'statement' are required.")
        try:
            tl = float(p.get("time_limit_sec", 2.0))
            ml = int(p.get("mem_limit_mb", 256))
        except (TypeError, ValueError):
            raise QuestionFileError(f"{where} ({title}): time/memory limits must be numbers.")
        if tl <= 0 or ml <= 0:
            raise QuestionFileError(f"{where} ({title}): limits must be positive.")

        tests = p.get("tests")
        if not isinstance(tests, list) or not tests:
            raise QuestionFileError(f"{where} ({title}): needs at least one test in 'tests'.")
        norm_tests = []
        for t_idx, t in enumerate(tests, start=1):
            if not isinstance(t, dict) or "input" not in t or "output" not in t:
                raise QuestionFileError(
                    f"{where} ({title}) test #{t_idx}: needs 'input' and 'output'.")
            norm_tests.append({
                "input": str(t["input"]),
                "output": str(t["output"]),
                "sample": 1 if t.get("sample") else 0,
            })

        out.append({
            "title": title,
            "statement": statement,
            "time_limit_sec": tl,
            "mem_limit_mb": ml,
            "tests": norm_tests,
        })
    return out


# ----------------------------------------------------------- leaderboard ----

def compute_leaderboard(users, submissions, start: dt.datetime | None,
                        end: dt.datetime | None) -> list[dict]:
    """ICPC-style standings.

    users:       iterable of dicts with keys id, username, full_name
    submissions: iterable of dicts with keys user_id, problem_id, verdict,
                 submitted_at (ISO string) -- ordered by submitted_at ascending
    Only submissions made inside [start, end] count. Penalty = minutes from
    contest start to first Accepted + 20 per earlier wrong attempt on that
    problem. Ranked by solved (desc), penalty (asc), then username.
    """
    per_user = {u["id"]: {"username": u["username"], "full_name": u["full_name"] or "",
                          "solved": 0, "penalty": 0, "progress": {}}
                for u in users}

    for s in submissions:
        if s["verdict"] in ("Pending", "Internal Error", "Restricted Code"):
            continue
        row = per_user.get(s["user_id"])
        if row is None:
            continue
        at = from_iso(s["submitted_at"])
        if start and at < start:
            continue
        if end and at > end:
            continue
        prog = row["progress"].setdefault(s["problem_id"], {"solved": False, "wrong": 0})
        if prog["solved"]:
            continue
        if s["verdict"] == "Accepted":
            prog["solved"] = True
            row["solved"] += 1
            minutes = max(0, int((at - start).total_seconds() // 60)) if start else 0
            row["penalty"] += minutes + PENALTY_PER_WRONG_MIN * prog["wrong"]
        else:
            prog["wrong"] += 1

    ranked = sorted(per_user.values(),
                    key=lambda r: (-r["solved"], r["penalty"], r["username"]))
    return [
        {"rank": i, "username": r["username"], "full_name": r["full_name"],
         "solved": r["solved"], "penalty": r["penalty"]}
        for i, r in enumerate(ranked, start=1)
    ]


# ------------------------------------------------- progress and duplicates ----

import hashlib as _hashlib

# Verdicts that are not a real attempt: still judging, judge failure, or banned code.
NOT_COUNTED = {"Pending", "Internal Error", "Restricted Code"}


def normalize_code(code: str) -> str:
    """Same program with different line endings, trailing spaces or blank lines
    counts as the same code."""
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines if line.strip())


def code_hash(code: str) -> str:
    return _hashlib.sha256(normalize_code(code).encode("utf-8")).hexdigest()


def progress_for(rows, start: dt.datetime | None) -> dict:
    """rows: dicts with problem_id, verdict, submitted_at (ISO), oldest first.
    Returns problem_id -> progress for one participant:
      solved, solved_min (minutes from start), wrong (attempts before solving),
      attempts (counted attempts), pending (a verdict is still coming)."""
    out = {}
    for r in rows:
        p = out.setdefault(r["problem_id"], {
            "solved": False, "solved_min": None, "wrong": 0,
            "attempts": 0, "pending": False,
        })
        verdict = r["verdict"]
        if verdict == "Pending":
            p["pending"] = True
            continue
        if verdict in NOT_COUNTED or p["solved"]:
            continue
        p["attempts"] += 1
        if verdict == "Accepted":
            p["solved"] = True
            at = from_iso(r["submitted_at"])
            p["solved_min"] = max(0, int((at - start).total_seconds() // 60)) if start else 0
        else:
            p["wrong"] += 1
    return out
