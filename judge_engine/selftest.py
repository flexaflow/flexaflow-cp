"""Start-up self-test: run real probes inside the sandbox and report whether
isolation holds. The server refuses to start if a required check fails.

Probes (each must behave as described for the sandbox to count as working):
  network   connecting to a TCP server running on the host is BLOCKED
  read      reading a host file outside the job folder is BLOCKED
  write     writing to a system path outside the job folder is BLOCKED
  workdir   writing inside the job folder is ALLOWED (sanity check)
  process   the sandbox cannot see the host's process list
"""

import os
import socket
import tempfile
from dataclasses import dataclass
from typing import List, Optional

from .sandbox import detect_backend, run_in_sandbox

PROBE = r'''
import os, socket, sys
results = []
def report(name, ok, detail=""):
    results.append(f"{name}={'OK' if ok else 'FAIL'} {detail}")

port = int(sys.argv[1]); secret = sys.argv[2]; syspath = sys.argv[3]

# network: should fail
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.close()
    report("network", False, "connected to host socket")
except OSError as e:
    report("network", True, f"blocked ({e.strerror or e})")

# read host file: should fail
try:
    open(secret).read()
    report("read", False, "host file readable")
except OSError as e:
    report("read", True, f"blocked ({e.strerror or e})")

# write system path: should fail
try:
    open(syspath, "w").write("x")
    report("write", False, "system path writable")
    os.remove(syspath)
except OSError as e:
    report("write", True, f"blocked ({e.strerror or e})")

# workdir: should succeed
try:
    with open("selftest_ok.txt", "w") as f:
        f.write("ok")
    report("workdir", True)
except OSError as e:
    report("workdir", False, str(e))

# process list: a host has hundreds of processes; the sandbox should see a handful
pids = [p for p in os.listdir("/proc") if p.isdigit()] if os.path.isdir("/proc") else []
report("process", len(pids) < 20, f"visible processes={len(pids)}")

print("\n".join(results))
'''


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def run_selftest() -> (Optional[str], List[Check]):
    backend = detect_backend()
    if backend is None:
        return None, [Check("sandbox", False, "no sandbox tool installed")]
    if backend == "none":
        return backend, [Check("sandbox", False, "JUDGE_SANDBOX=none: NO isolation")]

    workdir = tempfile.mkdtemp(prefix="judge_selftest_")
    job_dir = tempfile.mkdtemp(prefix="judge_selftest_job_")
    secret_dir = tempfile.mkdtemp(prefix="judge_selftest_secret_")
    secret_path = os.path.join(secret_dir, "secret.txt")
    syspath = "/etc/judge_selftest_probe"
    try:
        with open(secret_path, "w") as f:
            f.write("host secret")
        with open(os.path.join(workdir, "probe.py"), "w") as f:
            f.write(PROBE)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        outcome = run_in_sandbox(
            ["python3", "-I", "probe.py", str(port), secret_path, syspath],
            workdir, job_dir, "", 10, 128, backend=backend,
        )
        listener.close()

        lines = [l for l in outcome["stdout"].splitlines() if "=" in l]
        if not lines:
            return backend, [Check("probe", False,
                                   f"probe produced no output: {outcome['stderr'][:300]}")]
        checks = []
        for line in lines:
            name, rest = line.split("=", 1)
            passed = rest.startswith("OK")
            checks.append(Check(name, passed, rest[3:].strip()))
        return backend, checks
    finally:
        import shutil
        for d in (workdir, job_dir, secret_dir):
            shutil.rmtree(d, ignore_errors=True)
