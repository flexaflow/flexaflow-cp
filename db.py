"""SQLite schema, migrations and helpers.

Model:
  contests     one row per contest (title, start, end, attempt limit, archived flag)
  problems     belong to one contest (imported from that contest's problemset JSON)
  users        participants belong to one contest; admins have contest_id NULL
  submissions  belong to a problem, so to a contest

Only one contest is "current" at a time: the newest one that is not archived.
The database starts empty. The admin is created with `python manage.py createsuperuser`.
"""

import os
import secrets
import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("JUDGE_DB", os.path.join(BASE_DIR, "judge.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS contests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    start_at     TEXT,                 -- UTC ISO-8601
    end_at       TEXT,                 -- UTC ISO-8601
    max_attempts INTEGER NOT NULL DEFAULT 30,   -- per problem per participant, 0 = unlimited
    archived     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    full_name       TEXT,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    contest_id      INTEGER REFERENCES contests(id) ON DELETE CASCADE,
    session_token   TEXT,
    session_ip      TEXT,
    session_ua      TEXT,
    session_started TEXT,
    session_seen    TEXT
);

CREATE TABLE IF NOT EXISTS problems (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id     INTEGER REFERENCES contests(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,   -- A, B, C... order inside the contest
    title          TEXT NOT NULL,
    statement      TEXT NOT NULL,
    time_limit_sec REAL NOT NULL DEFAULT 2.0,
    mem_limit_mb   INTEGER NOT NULL DEFAULT 256
);

CREATE TABLE IF NOT EXISTS test_cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id  INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    input       TEXT NOT NULL,
    output      TEXT NOT NULL,
    is_sample   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS submissions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    problem_id     INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    language       TEXT NOT NULL,
    code           TEXT NOT NULL,
    code_hash      TEXT,
    verdict        TEXT NOT NULL DEFAULT 'Pending',
    max_time_ms    REAL NOT NULL DEFAULT 0,
    passed         INTEGER NOT NULL DEFAULT 0,
    total          INTEGER NOT NULL DEFAULT 0,
    compile_output TEXT NOT NULL DEFAULT '',
    submitted_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_user ON submissions(user_id);

CREATE TABLE IF NOT EXISTS settings (        -- legacy, read only during migration
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added to databases created by older versions.
ADDED_COLUMNS = {
    "users": {
        "contest_id": "INTEGER",
        "session_ip": "TEXT",
        "session_ua": "TEXT",
        "session_started": "TEXT",
        "session_seen": "TEXT",
        "totp_secret": "TEXT",
        "totp_enabled": "INTEGER NOT NULL DEFAULT 0",
        "totp_last": "INTEGER",
    },
    "problems": {"contest_id": "INTEGER"},
    "submissions": {"code_hash": "TEXT"},
}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def _add_missing_columns(conn):
    for table, columns in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate(conn):
    """Bring databases from older versions up to date. Safe to run repeatedly."""
    import services

    _add_missing_columns(conn)

    # The old version stored one contest in the settings table. Turn it into contest #1.
    if conn.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 0 \
            and get_setting(conn, "contest_start"):
        cur = conn.execute(
            "INSERT INTO contests(title, start_at, end_at, max_attempts, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (get_setting(conn, "contest_title", "Contest"),
             get_setting(conn, "contest_start"), get_setting(conn, "contest_end"),
             int(get_setting(conn, "max_attempts", "30")),
             services.to_iso(services.utcnow())),
        )
        cid = cur.lastrowid
        conn.execute("UPDATE users SET contest_id = ? WHERE is_admin = 0 AND contest_id IS NULL", (cid,))
        conn.execute("UPDATE problems SET contest_id = ? WHERE contest_id IS NULL", (cid,))

    conn.execute("CREATE INDEX IF NOT EXISTS idx_problem_contest ON problems(contest_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_hash ON submissions(user_id, problem_id, code_hash)")

    rows = conn.execute("SELECT id, code FROM submissions WHERE code_hash IS NULL").fetchall()
    conn.executemany("UPDATE submissions SET code_hash = ? WHERE id = ?",
                     [(services.code_hash(r["code"]), r["id"]) for r in rows])
    conn.commit()


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    conn.close()
    # Password hashes live here: only the server user may read it.
    for path in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(path):
            os.chmod(path, 0o600)


def current_contest(conn):
    """The contest that is live or being prepared: the newest one not archived."""
    return conn.execute(
        "SELECT * FROM contests WHERE archived = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()


# ------------------------------------------------------------ users ----

def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)


def new_session_token() -> str:
    return secrets.token_hex(32)
