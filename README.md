# Live Contest Judge (Flask + Socket.IO)

One-time, live competitive programming contest for a LAN (lab). No seed data:
the database starts empty. The admin is created from the command line, the
admin creates participant accounts and passwords, imports questions from JSON,
and sets the contest start and end time. Participants can only submit during
the window. Verdicts and the leaderboard update live over Socket.IO.

## 1. Install

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Install the language toolchains on the judge machine and make sure they are on PATH:

```bash
sudo apt install gcc g++ default-jdk python3 pypy3
```

Download the Socket.IO browser client once (on a machine with internet). The
lab LAN does not need internet afterwards:

```bash
curl -o static/socket.io.min.js https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js
```

## 2. Create the admin (once)

```bash
python manage.py createsuperuser
```

It asks for a username, a password (typed twice, at least 8 characters) and an
optional full name. There is no default admin.

## 3. Start the server

### Option A: Docker (recommended for the contest machine)

```bash
cp .env.example .env                     # then set JUDGE_SECRET in .env
docker compose up -d --build             # builds the image and starts the judge on port 8000
docker compose run --rm judge python manage.py createsuperuser
```

See section 10 ("Docker") for what the container needs and why.

### Option B: directly on the machine

```bash
export JUDGE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
python app.py
```

Local runs use Werkzeug's threading mode. The container uses eventlet (see "Server
engine" below).

Participants open `http://<judge-machine-LAN-ip>:8000`. Allow inbound TCP 8000 in the
firewall.

### Server engine

`JUDGE_ASYNC` chooses the engine:

| Value       | Use it for                                                                  |
|-------------|-----------------------------------------------------------------------------|
| `eventlet`  | Docker and production. Many simultaneous live connections on one process |
| `threading` | Local development (default when running `python app.py` directly)         |

Eventlet is set up before any other import, so it can patch threads and subprocesses
correctly. It is used directly as Flask-SocketIO's server, so gunicorn is not needed.
Only these two values are supported. Eventlet is in upstream maintenance mode; if that
matters to you, switching to gevent would need a code change in `app.py`, which is not done.

### Environment variables

| Variable       | Default | Purpose                                   |
|----------------|---------|-------------------------------------------|
| `JUDGE_SECRET` | random  | Cookie signing key. Set it, or everyone is logged out on restart |
| `JUDGE_PORT`   | `8000`  | Port                                      |
| `JUDGE_WORKERS`| `3`     | Submissions judged in parallel            |
| `JUDGE_DB`     | `judge.db` | Path to the SQLite file                |
| `JUDGE_ASYNC`  | `threading` | `eventlet` or `threading` (see above) |

## 4. Running contests (admin)

Each contest is separate: its own problemset, participants, submissions, leaderboard
and export. Only one contest is active at a time.

### The lifecycle

```
Create  →  Import problems  →  Add participants  →  Running  →  End  →  Export  →  Archive
```

1. **Contests → Start a new contest.** Enter a title, start and end times (server local
   time) and the attempt limit per problem (0 = unlimited). This is blocked while another
   contest is active.
2. **Problems → Import.** Upload that contest's problemset JSON (see section 5). Import is
   all-or-nothing.
3. **Participants → add or bulk-create.** Usernames must be unique across all contests,
   so use new ones for each contest. Passwords are shown once after creation. Copy them.
4. **Dashboard.** Check the schedule. Participants see a countdown and the problems unlock
   at the start time with no refresh needed.
5. **Running.** Watch submissions on the Dashboard. Click a number to read the code.
   You can change the schedule and attempt limit while the contest is active.
6. **End.** Either wait for the end time, or press **End contest now** on the contest
   page. Submissions close immediately.
7. **Export.** Download the ZIP: problemset JSON, leaderboard, participants, all
   submissions as CSV, and each submission's source code. Keep this file. Passwords are
   not included.
8. **Archive.** The contest becomes read-only and its participants are signed out. Its
   data stays visible under **Contests → Open**, so you can review it later. Then you can
   start the next contest.

Pages: **Dashboard** (active contest), **Contests** (create, end, archive, export, history),
**Participants**, **Problems**, **Sessions** (who is logged in, from which IP).

### Command-line import (optional)

```bash
python manage.py import_questions questions.json      # into the ACTIVE contest
```

## 5. Question JSON format

```json
{
  "problems": [
    {
      "title": "A + B",
      "statement": "Read two integers a and b on one line. Print their sum.",
      "time_limit_sec": 2,
      "mem_limit_mb": 256,
      "tests": [
        {"input": "3 4\n", "output": "7\n", "sample": true},
        {"input": "10 20\n", "output": "30\n"}
      ]
    }
  ]
}
```

- `title`, `statement`, and at least one entry in `tests` are required.
- `time_limit_sec` (default 2) and `mem_limit_mb` (default 256) are optional.
- `"sample": true` shows that test on the problem page. All tests are used for judging, and hidden test contents are never shown to participants.
- Problems are labelled A, B, C… in the order they appear in the file.
- Importing is all-or-nothing. A single invalid problem saves nothing.
- A ready example is in `examples/questions_example.json`.

## 6. How the contest works

- **Before start**: participants see a countdown. Problem pages redirect to the list.
- **Running**: participants open problems and submit. Each submission is judged
  against every test. The verdict is pushed to the browser live.
- **After end**: problems stay readable. Submissions are closed.
- **Leaderboard**: ICPC style. Ranked by problems solved, then penalty. Penalty
  is minutes from the start to each first Accepted, plus 20 minutes for every
  wrong attempt on that problem. Only submissions inside the window count.
- **Strict sessions**: one login per account. Logging in on a second device logs out
  the first. A session is also bound to the IP address it started from, and ends after
  30 minutes without activity (change with `JUDGE_IDLE_MIN`). See section 11.
- **Your status on every problem**: Solved (with minutes after start and wrong tries),
  wrong attempts, judging, or not tried. The problem list and the problem page show it.
- **Solve once**: after a problem is Accepted you can't submit to it again.
- **Identical code is blocked**: resubmitting the same program is refused without judging.
  Whitespace, blank lines and line endings don't count as a change.
- **Attempt limit**: each participant gets a set number of attempts per problem (default 30).
  The admin sets it on the Dashboard, and 0 means unlimited. Attempts with a judge error or
  restricted code don't count.
- **Limits**: one pending submission at a time, 10 seconds between submissions, and 64 KB
  maximum code size.

## 7. Project layout

```
app.py          Flask routes, Socket.IO events, status ticker
contests.py     Contest lifecycle, participants, problems, leaderboard, export (no Flask)
db.py           SQLite schema, migrations from older versions, helpers
services.py     Pure logic: time, JSON validation, leaderboard, progress, duplicate check
judging.py      Background judging (thread pool), recovery of Pending submissions
judge_engine/   sandbox.py, restrictions.py, selftest.py, engine.py, verdict.py, languages.py
manage.py       createsuperuser, import_questions
templates/      Pages
static/         style.css, contest.js (live updates), socket.io.min.js (you download it)
examples/       questions_example.json
tests/          test_services, test_progress, test_contests, test_security
```

## 8. Tests

```bash
python -m unittest tests/test_services.py tests/test_progress.py tests/test_contests.py tests/test_security.py -v
```

- `test_services.py`: contest timing, JSON validation, leaderboard rules (no dependencies).
- `test_security.py`: source checks, and live sandbox tests (network blocked even when the
  source check is bypassed, time limits, compile errors). Needs bubblewrap or util-linux `unshare`.

## 9. Two-factor login for admins (optional)

Each admin can turn on a 6-digit code from an authenticator app, in addition to the password.

- **Turn on:** Security → Set up two-factor login. Add the key shown on the page to your app
  (choose "enter a setup key"), then type the code the app shows to confirm.
- **Login:** password first, then the code on a second page. Codes are valid for 30 seconds,
  and each one works only once.
- **Turn off:** Security → enter your password and a current code.
- **Lost phone:** the server operator runs `python manage.py reset_2fa <username>` (or inside
  Docker: `docker compose run --rm judge python manage.py reset_2fa <username>`). Then set it up again.

The secret is stored in the database, which is readable only by the server user. Protect the
server and its backups the same way you protect password hashes.

## 10. Docker

### What the image contains

Python 3.13, gcc/g++ (C and C++), JDK (Java), PyPy, and bubblewrap for the sandbox. The
Socket.IO client is downloaded at build time. The server runs as a non-root user (uid
10001). Data lives in the `judge-data` volume at `/data`.

### Why the compose file relaxes two Docker defaults

Judged code runs inside a bubblewrap sandbox that creates user namespaces. Docker's default
seccomp and AppArmor profiles block that, so `docker-compose.yml` sets `seccomp:unconfined`,
`apparmor:unconfined` and `SYS_ADMIN`. This is needed for the sandbox to start. The
start-up self-test (section 11, "Startup self-test") verifies that the sandbox works. If it
fails, the server refuses to start and logs why.

Those settings weaken the container's own protection. The sandbox keeps the judged code
away from the network, the database and other processes. Still, the strongest setup is a
dedicated VM or machine for the judge, not a shared host.

### Commands

```bash
docker compose up -d --build                                  # start
docker compose logs -f judge                                  # watch logs
docker compose run --rm judge python manage.py createsuperuser
docker compose run --rm judge python manage.py import_questions /data/problems.json
docker compose down                                           # stop (data is kept)
docker compose down -v                                        # stop and DELETE the database
```

To import a problemset from your machine, copy it into the container first:
`docker compose cp problems.json judge:/tmp/problems.json`, then import from `/tmp`.
The web admin page is simpler, though.

### Updating

```bash
git pull   # or replace the files
docker compose up -d --build
```

The database survives the rebuild because it is in the volume. Schema changes are applied
automatically at start.

## 11. Sandbox and security

### What submitted code can NOT do

Every compile and every test run happens inside a sandbox (`judge_engine/sandbox.py`):

| Blocked                          | How                                                      |
|----------------------------------|----------------------------------------------------------|
| Network (any socket, any host)   | Private network namespace, no interfaces, no route        |
| Reading the database / host files | bubblewrap does not mount them at all (see weak mode below) |
| Writing outside its own folder   | System dirs are read-only; only the job folder is writable |
| Seeing or signalling other processes | Private PID namespace                                  |
| Keeping processes after timeout  | Whole process group is killed                             |
| Huge output / disk filling       | Output goes to files capped at 8 MB (RLIMIT_FSIZE)        |
| CPU, memory, fork bombs          | rlimits: CPU time, address space (not Java), process count |
| Inherited secrets                | Environment is cleared; only PATH/HOME/LANG are set       |
| Capabilities                     | All dropped                                              |

Source checks (`judge_engine/restrictions.py`) also reject obvious networking and
process-control code with the verdict **Restricted Code**, which is not counted as
an attempt on the leaderboard. Those checks are only a courtesy message: code can
hide imports, so the sandbox is what actually enforces the rules. The test suite
verifies this by using a string-concatenated `import socket` that gets past the
source check and still cannot connect.

### Install the sandbox (required)

```bash
sudo apt install bubblewrap
```

On Ubuntu 24.04+, unprivileged user namespaces can be restricted by AppArmor. If
bubblewrap reports "Permission denied" for namespaces, check the AppArmor profile
for `/usr/bin/bwrap` (the package normally ships one).

### Startup self-test

Every start runs probes inside the sandbox and prints the result:

```
Sandbox backend: bwrap
  [PASS] network: blocked (...)
  [PASS] read: blocked (...)
  [PASS] write: blocked (...)
  [PASS] workdir: 
  [PASS] process: visible processes=1
```

If any check fails, the server **refuses to start**. The message tells you what to fix.

### Environment variables (security)

| Variable                    | Default | Meaning                                              |
|-----------------------------|---------|------------------------------------------------------|
| `JUDGE_SANDBOX`             | `auto`  | `auto`, `bwrap`, `unshare`, or `none` (development only) |
| `JUDGE_ALLOW_WEAK_SANDBOX`  | unset   | `1` allows the `unshare` backend. Host files stay visible to submitted code. Not recommended |
| `JUDGE_ALLOW_UNSAFE`        | unset   | `1` allows `JUDGE_SANDBOX=none`. Submitted code runs with NO isolation. Development only |
| `JUDGE_EXTRA_RO`            | unset   | Extra read-only paths for compilers/interpreters, separated by `:` (e.g. `/opt/pypy`) |
| `JUDGE_HTTPS`               | unset   | `1` sets the session cookie to Secure. Use only when serving over HTTPS |
| `JUDGE_IDLE_MIN`            | `30`    | Minutes without activity before a session ends |

### Web security

- **Forms:** every POST form carries a CSRF token. A forged cross-site request is rejected with 400.
- **Login lockout:** 5 failed attempts for the same username from the same IP lock that pair for 10 minutes. Failures return the same message whether or not the username exists.
- **One login per account:** logging in elsewhere invalidates the older session.
- **IP-bound sessions:** each login is tied to the IP it started from. The same cookie from
  any other IP is rejected with "Your network address changed". That rejection does not
  end the real owner's session. Log in again from the new IP to continue.
- **Idle timeout:** 30 minutes without a request ends the session (`JUDGE_IDLE_MIN`).
- **Admin session page** (Sessions in the menu): shows every logged-in account with its IP,
  browser, login time, last-seen time and idle minutes. Admins can end any session. The page
  refreshes every 15 seconds.
- **Cookies:** HttpOnly and SameSite=Strict.
- **Headers:** Content-Security-Policy (`script-src 'self'`, no inline scripts), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `Cache-Control: no-store`.
- **Database file:** permissions set to 0600 (it contains password hashes).
- **Passwords:** hashed with Werkzeug's default algorithm. Never stored in plain text.
- **Socket.IO:** only same-origin browsers can connect, and only with a valid login.

### Run it as a dedicated user

Create a user with no login shell and no sudo, and run the server as that user:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin judge
sudo chown -R judge: /path/to/contest-judge
sudo -u judge env JUDGE_SECRET=... python app.py
```

The process limit (`RLIMIT_NPROC`) counts all processes of the same user, so a
dedicated user avoids the limit being shared with other programs.

### What this does NOT protect against

- **Kernel exploits.** Namespaces are not a VM. For a hostile internet audience, use a VM per judge.
- **Machine resources shared by everyone.** Each test has time and memory limits, but a
  large number of simultaneous submissions can still slow the machine. Keep `JUDGE_WORKERS` modest.
- **IP-bound sessions and proxies.** The IP is the address the server sees. If you put a
  reverse proxy in front, every user will appear to have the proxy's IP and the binding
  will be meaningless. Serve the judge directly on the LAN. Students whose IP changes
  mid-contest (for example after a Wi-Fi reconnect) must log in again.
- **Plain-HTTP traffic on the LAN.** Anyone on the network can read passwords and
  session cookies. Put the contest on a closed network, or use HTTPS (reverse proxy)
  with `JUDGE_HTTPS=1`.
- **Cheating between participants.** Use the admin code view and keep phones and
  other devices out of reach during the contest.
