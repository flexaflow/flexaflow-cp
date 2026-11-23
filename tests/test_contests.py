"""Contest lifecycle, isolation, export and migration tests.

    python -m unittest tests/test_contests.py -v
"""

import datetime as dt
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
import zipfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ["JUDGE_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

try:
    import werkzeug.security  # noqa: F401
except ImportError:  # werkzeug is not installed in every test environment
    wz = types.ModuleType("werkzeug")
    sec = types.ModuleType("werkzeug.security")
    sec.generate_password_hash = lambda p: "h:" + p
    sec.check_password_hash = lambda h, p: h == "h:" + p
    sys.modules["werkzeug"], sys.modules["werkzeug.security"] = wz, sec

import contests  # noqa: E402
import db  # noqa: E402
import services as sv  # noqa: E402

PROBLEMSET = json.dumps({"problems": [
    {"title": "A + B", "statement": "sum",
     "tests": [{"input": "1 2\n", "output": "3\n", "sample": True}]},
    {"title": "Echo", "statement": "echo",
     "tests": [{"input": "x\n", "output": "x\n"}]},
]})


def iso(delta_minutes):
    return (sv.utcnow() + dt.timedelta(minutes=delta_minutes)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = os.environ["JUDGE_DB"]
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        db.init_db()
        self.conn = db.get_conn()

    def tearDown(self):
        self.conn.close()

    def new_running(self, title="C1"):
        cid = contests.create_contest(self.conn, title, iso(-60), iso(60), 5)
        return contests.get_contest(self.conn, cid)

    def add_submission(self, uid, pid, verdict, code="print(1)"):
        self.conn.execute(
            "INSERT INTO submissions(user_id, problem_id, language, code, code_hash, verdict, submitted_at) "
            "VALUES (?, ?, 'python', ?, ?, ?, ?)",
            (uid, pid, code, sv.code_hash(code), verdict, sv.to_iso(sv.utcnow())))
        self.conn.commit()


class LifecycleTests(Base):
    def test_only_one_active_contest(self):
        self.new_running("first")
        with self.assertRaises(ValueError):
            contests.create_contest(self.conn, "second", iso(0), iso(60), 5)

    def test_end_only_when_running(self):
        cid = contests.create_contest(self.conn, "future", iso(60), iso(120), 5)
        with self.assertRaises(ValueError):
            contests.end_contest(self.conn, cid)

    def test_end_then_archive(self):
        row = self.new_running()
        contests.end_contest(self.conn, row["id"])
        ended = contests.get_contest(self.conn, row["id"])
        self.assertEqual(contests.contest_status(ended), "ended")
        contests.archive_contest(self.conn, row["id"])
        arch = contests.get_contest(self.conn, row["id"])
        self.assertEqual(contests.contest_status(arch), "archived")
        self.assertIsNone(db.current_contest(self.conn))

    def test_cannot_archive_running(self):
        row = self.new_running()
        with self.assertRaises(ValueError):
            contests.archive_contest(self.conn, row["id"])

    def test_new_contest_after_archive(self):
        row = self.new_running("old")
        contests.end_contest(self.conn, row["id"])
        contests.archive_contest(self.conn, row["id"])
        cid = contests.create_contest(self.conn, "new", iso(-5), iso(55), 10)
        self.assertEqual(db.current_contest(self.conn)["id"], cid)

    def test_archived_contest_is_read_only(self):
        row = self.new_running()
        contests.end_contest(self.conn, row["id"])
        contests.archive_contest(self.conn, row["id"])
        with self.assertRaises(ValueError):
            contests.update_contest(self.conn, row["id"], "x", iso(-1), iso(1), 3)
        with self.assertRaises(ValueError):
            contests.import_problems(self.conn, row["id"], PROBLEMSET)

    def test_archive_signs_out_participants(self):
        row = self.new_running()
        contests.create_participant(self.conn, row["id"], "stu01", "", "secret1")
        self.conn.execute("UPDATE users SET session_token = 'tok' WHERE username = 'stu01'")
        self.conn.commit()
        contests.end_contest(self.conn, row["id"])
        contests.archive_contest(self.conn, row["id"])
        tok = self.conn.execute("SELECT session_token FROM users WHERE username='stu01'").fetchone()[0]
        self.assertIsNone(tok)

    def test_bad_times_and_attempts_rejected(self):
        with self.assertRaises(ValueError):
            contests.create_contest(self.conn, "x", iso(60), iso(0), 5)
        with self.assertRaises(ValueError):
            contests.create_contest(self.conn, "x", iso(0), iso(60), 5000)


class IsolationTests(Base):
    def test_participants_and_problems_are_per_contest(self):
        a = self.new_running("A")
        contests.import_problems(self.conn, a["id"], PROBLEMSET)
        contests.create_participant(self.conn, a["id"], "alice1", "Alice", "")
        contests.end_contest(self.conn, a["id"])
        contests.archive_contest(self.conn, a["id"])

        b_id = contests.create_contest(self.conn, "B", iso(-5), iso(55), 5)
        b = contests.get_contest(self.conn, b_id)
        self.assertEqual(contests.problem_list(self.conn, b_id), [])
        self.assertEqual(contests.participants(self.conn, b_id), [])

        contests.import_problems(self.conn, b_id, PROBLEMSET)
        contests.create_participant(self.conn, b_id, "bob002", "Bob", "")
        self.assertEqual(len(contests.problem_list(self.conn, b_id)), 2)
        self.assertEqual([u["username"] for u in contests.participants(self.conn, b_id)], ["bob002"])

    def test_leaderboard_ignores_other_contests(self):
        a = self.new_running("A")
        contests.import_problems(self.conn, a["id"], PROBLEMSET)
        contests.create_participant(self.conn, a["id"], "alice1", "", "")
        alice = self.conn.execute("SELECT id FROM users WHERE username='alice1'").fetchone()[0]
        pid = contests.problem_list(self.conn, a["id"])[0]["id"]
        self.add_submission(alice, pid, "Accepted")
        board = contests.leaderboard(self.conn, contests.get_contest(self.conn, a["id"]))
        self.assertEqual(board[0]["solved"], 1)
        contests.end_contest(self.conn, a["id"])
        contests.archive_contest(self.conn, a["id"])

        b_id = contests.create_contest(self.conn, "B", iso(-5), iso(55), 5)
        self.assertEqual(contests.leaderboard(self.conn, contests.get_contest(self.conn, b_id)), [])

    def test_usernames_are_unique_across_contests(self):
        a = self.new_running("A")
        contests.create_participant(self.conn, a["id"], "stu01", "", "secret1")
        contests.end_contest(self.conn, a["id"])
        contests.archive_contest(self.conn, a["id"])
        b_id = contests.create_contest(self.conn, "B", iso(-5), iso(55), 5)
        with self.assertRaises(ValueError):
            contests.create_participant(self.conn, b_id, "stu01", "", "secret1")

    def test_failed_import_saves_nothing(self):
        row = self.new_running()
        bad = json.dumps({"problems": [
            {"title": "ok", "statement": "s", "tests": [{"input": "1", "output": "1"}]},
            {"title": "broken", "statement": "s", "tests": []},
        ]})
        with self.assertRaises(Exception):
            contests.import_problems(self.conn, row["id"], bad)
        self.assertEqual(contests.problem_list(self.conn, row["id"]), [])


class ExportTests(Base):
    def test_export_contains_everything(self):
        row = self.new_running("Export me")
        contests.import_problems(self.conn, row["id"], PROBLEMSET)
        contests.create_participant(self.conn, row["id"], "alice1", "Alice", "secret1")
        alice = self.conn.execute("SELECT id FROM users WHERE username='alice1'").fetchone()[0]
        pid = contests.problem_list(self.conn, row["id"])[0]["id"]
        self.add_submission(alice, pid, "Accepted", code="print(3)\n")
        contests.end_contest(self.conn, row["id"])
        row = contests.get_contest(self.conn, row["id"])

        data = contests.build_export(self.conn, row)
        z = zipfile.ZipFile(io.BytesIO(data))
        names = set(z.namelist())
        self.assertTrue({"contest.json", "leaderboard.csv", "participants.csv", "submissions.csv"} <= names)
        self.assertTrue(any(n.startswith("code/") and n.endswith(".py") for n in names))
        meta = json.loads(z.read("contest.json"))
        self.assertEqual(meta["title"], "Export me")
        self.assertEqual(len(meta["problems"]), 2)
        self.assertNotIn("password", z.read("participants.csv").decode().lower())
        # the exported problemset can be imported again into a new contest
        contests.archive_contest(self.conn, row["id"])
        new_id = contests.create_contest(self.conn, "again", iso(-5), iso(55), 5)
        n = contests.import_problems(self.conn, new_id, json.dumps({"problems": meta["problems"]}))
        self.assertEqual(n, 2)


class MigrationTests(unittest.TestCase):
    """A database made by the single-contest version becomes contest #1."""

    def test_old_database_is_migrated(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        os.environ["JUDGE_DB"] = path
        db.DB_PATH = path
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
              password_hash TEXT NOT NULL, full_name TEXT, is_admin INTEGER NOT NULL DEFAULT 0, session_token TEXT);
            CREATE TABLE problems (id INTEGER PRIMARY KEY AUTOINCREMENT, position INTEGER NOT NULL,
              title TEXT NOT NULL, statement TEXT NOT NULL, time_limit_sec REAL NOT NULL DEFAULT 2.0,
              mem_limit_mb INTEGER NOT NULL DEFAULT 256);
            CREATE TABLE test_cases (id INTEGER PRIMARY KEY AUTOINCREMENT, problem_id INTEGER NOT NULL,
              input TEXT NOT NULL, output TEXT NOT NULL, is_sample INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
              problem_id INTEGER NOT NULL, language TEXT NOT NULL, code TEXT NOT NULL,
              verdict TEXT NOT NULL DEFAULT 'Pending', max_time_ms REAL NOT NULL DEFAULT 0,
              passed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
              compile_output TEXT NOT NULL DEFAULT '', submitted_at TEXT NOT NULL);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO settings VALUES ('contest_title', 'Old Lab Contest');
            INSERT INTO settings VALUES ('contest_start', '2026-11-20T10:00:00+00:00');
            INSERT INTO settings VALUES ('contest_end', '2026-11-20T13:00:00+00:00');
            INSERT INTO settings VALUES ('max_attempts', '7');
            INSERT INTO users(username, password_hash, is_admin) VALUES ('admin', 'x', 1);
            INSERT INTO users(username, password_hash, is_admin) VALUES ('stu01', 'x', 0);
            INSERT INTO problems(position, title, statement) VALUES (1, 'A', 's');
            INSERT INTO submissions(user_id, problem_id, language, code, verdict, submitted_at)
              VALUES (2, 1, 'python', 'print(1)', 'Accepted', '2026-11-20T10:05:00+00:00');
        """)
        c.commit()
        c.close()

        db.init_db()
        db.init_db()  # running twice is harmless
        conn = db.get_conn()
        cur = db.current_contest(conn)
        self.assertEqual(cur["title"], "Old Lab Contest")
        self.assertEqual(cur["max_attempts"], 7)
        self.assertEqual(conn.execute("SELECT contest_id FROM users WHERE username='stu01'").fetchone()[0], cur["id"])
        self.assertIsNone(conn.execute("SELECT contest_id FROM users WHERE username='admin'").fetchone()[0])
        self.assertEqual(conn.execute("SELECT contest_id FROM problems").fetchone()[0], cur["id"])
        self.assertIsNotNone(conn.execute("SELECT code_hash FROM submissions").fetchone()[0])
        conn.close()


if __name__ == "__main__":
    unittest.main()
