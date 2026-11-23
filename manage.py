"""Admin command line.

    python manage.py createsuperuser                 create the admin (interactive)
    python manage.py import_questions problems.json  import problems into the ACTIVE contest
    python manage.py reset_2fa <admin username>     turn off two-factor login (lost phone)
"""

import getpass
import sys

import contests
import db
import services as sv
from services import QuestionFileError


def createsuperuser():
    db.init_db()
    username = input("Admin username: ").strip()
    if not sv.USERNAME_RE.match(username):
        sys.exit("Username: use 3-32 letters, digits, _ . or -")
    while True:
        pw1 = getpass.getpass("Password: ")
        if len(pw1) < 8:
            print("Use at least 8 characters.")
            continue
        pw2 = getpass.getpass("Password (again): ")
        if pw1 != pw2:
            print("Passwords do not match. Try again.")
            continue
        break
    full_name = input("Full name (optional): ").strip()

    conn = db.get_conn()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            sys.exit(f"User '{username}' already exists.")
        conn.execute(
            "INSERT INTO users(username, password_hash, full_name, is_admin) VALUES (?, ?, ?, 1)",
            (username, db.hash_password(pw1), full_name),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"Admin '{username}' created. Start the server with: python app.py")


def import_questions(path):
    db.init_db()
    conn = db.get_conn()
    try:
        active = db.current_contest(conn)
        if active is None:
            sys.exit("No active contest. Create one in the web admin (Contests page) first.")
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        n = contests.import_problems(conn, active["id"], text)
    except (QuestionFileError, ValueError) as e:
        sys.exit(f"Import failed, nothing was saved: {e}")
    finally:
        conn.close()
    print(f"Imported {n} problem(s) into '{active['title']}'.")


def reset_2fa(username):
    db.init_db()
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_last = NULL "
            "WHERE username = ? AND is_admin = 1", (username,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        sys.exit(f"No admin named '{username}'.")
    print(f"Two-factor login turned off for '{username}'. Log in and set it up again if you want it.")


def main(argv):
    if len(argv) >= 2 and argv[1] == "createsuperuser":
        createsuperuser()
    elif len(argv) == 3 and argv[1] == "import_questions":
        import_questions(argv[2])
    elif len(argv) == 3 and argv[1] == "reset_2fa":
        reset_2fa(argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
