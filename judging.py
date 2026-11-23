"""Background judging. Uses the existing judge_engine unchanged.

A thread pool is enough: judge_submission spends its time in subprocesses,
which release the GIL. Results are written to the DB, then `on_done` is called
so the app can push live updates over Socket.IO.

Guarantee: every submission that is queued ends with a final verdict. If the
judge crashes, the row is set to "Internal Error" instead of staying "Pending".
On server start, submissions left "Pending" by a previous run are re-queued.
"""

import os
import traceback
from concurrent.futures import ThreadPoolExecutor

import db
from judge_engine.engine import TestCase, judge_submission

_executor = None


def init_executor(max_workers: int = 3):
    """Create the thread pool once. Safe to call more than once."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="judge")


def submit_judging(submission_id: int, on_done):
    # Self-initialise so judging works however the app was started.
    init_executor(int(os.environ.get("JUDGE_WORKERS", "3")))
    _executor.submit(_safe_run, submission_id, on_done)


def requeue_pending(on_done):
    """Re-judge submissions that were still Pending when the server stopped."""
    conn = db.get_conn()
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM submissions WHERE verdict = 'Pending' ORDER BY id")]
    finally:
        conn.close()
    for sid in ids:
        submit_judging(sid, on_done)
    return len(ids)


def _mark_internal_error(submission_id: int, message: str):
    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE submissions SET verdict = 'Internal Error', compile_output = ? "
            "WHERE id = ? AND verdict = 'Pending'",
            (message, submission_id),
        )
        conn.commit()
    finally:
        conn.close()


def _safe_run(submission_id: int, on_done):
    """Never let a submission stay Pending: any failure becomes Internal Error."""
    try:
        _run(submission_id, on_done)
    except Exception:
        traceback.print_exc()
        _mark_internal_error(submission_id, "Judge error. Please resubmit.")
        try:
            conn = db.get_conn()
            try:
                row = conn.execute("SELECT user_id FROM submissions WHERE id = ?",
                                   (submission_id,)).fetchone()
            finally:
                conn.close()
            if row:
                on_done(submission_id, row["user_id"])
        except Exception:
            traceback.print_exc()


def _run(submission_id: int, on_done):
    conn = db.get_conn()
    try:
        s = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        p = conn.execute("SELECT * FROM problems WHERE id = ?", (s["problem_id"],)).fetchone()
        rows = conn.execute(
            "SELECT input, output FROM test_cases WHERE problem_id = ? ORDER BY id",
            (p["id"],),
        ).fetchall()
    finally:
        conn.close()

    tests = [TestCase(input=r["input"], expected_output=r["output"]) for r in rows]
    try:
        result = judge_submission(
            s["language"], s["code"], tests, p["time_limit_sec"], p["mem_limit_mb"]
        )
        verdict = result.verdict.value
        max_ms, passed, total = result.max_time_ms, result.passed, result.total
        compile_output = result.compile_output
    except Exception:
        traceback.print_exc()
        verdict, max_ms, passed, total = "Internal Error", 0, 0, len(tests)
        compile_output = "Judge error. Please resubmit."

    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE submissions SET verdict = ?, max_time_ms = ?, passed = ?, total = ?, "
            "compile_output = ? WHERE id = ?",
            (verdict, max_ms, passed, total, compile_output, submission_id),
        )
        conn.commit()
    finally:
        conn.close()

    on_done(submission_id, s["user_id"])
