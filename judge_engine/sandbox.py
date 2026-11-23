"""Sandbox for untrusted code: network, filesystem and process isolation.

Backends, strongest first (chosen automatically, or force with JUDGE_SANDBOX):

  bwrap    bubblewrap. Private network, PID, IPC and UTS namespaces. System
           directories are read-only. Only the job's own folder is writable.
           Host files such as judge.db are not visible at all.
  unshare  util-linux. Private network, PID, IPC and UTS namespaces only.
           Host files stay visible, so the server refuses to start with this
           backend unless JUDGE_ALLOW_WEAK_SANDBOX=1.
  none     No isolation. Development only (JUDGE_SANDBOX=none).

Every backend also applies rlimits (CPU time, memory, file size, process count)
and kills the whole process group on timeout.

Output goes to files outside the sandbox, not to pipes, so a program that
prints forever is stopped by RLIMIT_FSIZE instead of filling server memory.
"""

import glob
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from typing import List, Optional

from .languages import LanguageConfig

# Read-only system locations needed to run compilers and interpreters.
RO_SYSTEM_PATHS = [
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
    "/etc/alternatives", "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d",
]
# Extra read-only paths, e.g. JUDGE_EXTRA_RO="/opt/pypy:/opt/jdk"
RO_SYSTEM_PATHS += [p for p in os.environ.get("JUDGE_EXTRA_RO", "").split(":") if p]
# The JDK reads its configuration from /etc/java-* (symlinked from /usr/lib/jvm).
RO_SYSTEM_PATHS += sorted(glob.glob("/etc/java-*"))

SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"
WALL_CLOCK_GRACE_SEC = 1.0
COMPILE_TIMEOUT_SEC = 15
MAX_OUTPUT_BYTES = 8 * 1024 * 1024  # 8 MB per output stream


class SandboxUnavailable(RuntimeError):
    pass


def detect_backend() -> Optional[str]:
    """Return the backend name, or None if no usable sandbox tool exists."""
    requested = os.environ.get("JUDGE_SANDBOX", "auto").lower()
    if requested == "none":
        return "none"
    if requested == "bwrap":
        return "bwrap" if shutil.which("bwrap") else None
    if requested == "unshare":
        return "unshare" if shutil.which("unshare") else None
    if shutil.which("bwrap"):
        return "bwrap"
    if shutil.which("unshare"):
        return "unshare"
    return None


def wrap_command(argv: List[str], workdir: str, backend: Optional[str] = None) -> List[str]:
    """Wrap argv so it runs inside the sandbox with `workdir` as its only writable folder."""
    backend = backend or detect_backend()
    if backend is None:
        raise SandboxUnavailable(
            "No sandbox tool found. Install bubblewrap (apt install bubblewrap) "
            "or util-linux unshare."
        )
    if backend == "none":
        return list(argv)

    if backend == "bwrap":
        cmd = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",          # network, pid, ipc, uts, user, mount, cgroup
            "--cap-drop", "ALL",
            "--clearenv",
            "--setenv", "PATH", SANDBOX_PATH,
            "--setenv", "HOME", "/work",
            "--setenv", "LANG", "C.UTF-8",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", workdir, "/work",
            "--chdir", "/work",
        ]
        for p in RO_SYSTEM_PATHS:
            if os.path.islink(p):
                cmd += ["--symlink", os.readlink(p), p]
            elif os.path.exists(p):
                cmd += ["--ro-bind", p, p]
        return cmd + list(argv)

    # unshare: network + process isolation only
    cmd = ["unshare", "--fork", "--kill-child", "--pid", "--mount-proc",
           "--net", "--ipc", "--uts"]
    if os.geteuid() != 0:
        cmd = ["unshare", "--user", "--map-root-user", "--fork", "--kill-child",
               "--pid", "--mount-proc", "--net", "--ipc", "--uts"]
    return cmd + list(argv)


def sandbox_env(workdir: str) -> dict:
    return {"PATH": SANDBOX_PATH, "HOME": workdir, "LANG": "C.UTF-8", "TMPDIR": workdir}


# ------------------------------------------------------------ execution ----

def _limit_setter(cpu_seconds: int, mem_bytes: Optional[int], process_limit: int = 64):
    def _apply():
        resource_mod = resource
        # Process count is per real UID, so run the server as a dedicated user.
        resource_mod.setrlimit(resource_mod.RLIMIT_NPROC, (process_limit, process_limit))
        resource_mod.setrlimit(resource_mod.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource_mod.setrlimit(resource_mod.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
        resource_mod.setrlimit(resource_mod.RLIMIT_CORE, (0, 0))
        if mem_bytes is not None:
            resource_mod.setrlimit(resource_mod.RLIMIT_AS, (mem_bytes, mem_bytes))
    return _apply


def _kill_group(proc: subprocess.Popen):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _read_capped(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read(MAX_OUTPUT_BYTES)
    return data.decode("utf-8", errors="replace")


def _signal_of(returncode: Optional[int]) -> Optional[int]:
    """Signal that killed the process, if any. Wrappers report 128+N, so accept that too."""
    if returncode is None:
        return None
    if returncode < 0:
        return -returncode
    if returncode > 128:
        return returncode - 128
    return None


def run_in_sandbox(argv, workdir, job_dir, stdin_text, time_limit_sec, mem_limit_mb,
                   backend=None):
    """Run argv inside the sandbox. Returns a dict with raw results.

    job_dir is a host folder OUTSIDE the sandbox where stdin/stdout/stderr files live.
    """
    cpu_seconds = max(1, int(time_limit_sec) + 1)
    mem_bytes = mem_limit_mb * 1024 * 1024 if mem_limit_mb else None

    in_path = os.path.join(job_dir, "stdin.txt")
    out_path = os.path.join(job_dir, "stdout.txt")
    err_path = os.path.join(job_dir, "stderr.txt")
    with open(in_path, "wb") as f:
        f.write(stdin_text.encode("utf-8"))

    full_argv = wrap_command(argv, workdir, backend)
    start = time.monotonic()
    timed_out = False
    with open(in_path, "rb") as fi, open(out_path, "wb") as fo, open(err_path, "wb") as fe:
        proc = subprocess.Popen(
            full_argv, cwd=workdir, stdin=fi, stdout=fo, stderr=fe,
            env=sandbox_env(workdir), preexec_fn=_limit_setter(cpu_seconds, mem_bytes),
            start_new_session=True,
        )
        try:
            returncode = proc.wait(timeout=time_limit_sec + WALL_CLOCK_GRACE_SEC)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            returncode = proc.wait()
    elapsed_ms = (time.monotonic() - start) * 1000

    return {
        "timed_out": timed_out,
        "returncode": None if timed_out else returncode,
        "elapsed_ms": elapsed_ms,
        "stdout": _read_capped(out_path),
        "stderr": _read_capped(err_path),
    }


def compile_in_sandbox(workdir: str, lang: LanguageConfig, filename: str, backend=None):
    """Returns (success, output)."""
    if lang.compile_cmd is None:
        return True, ""
    argv = lang.compile_cmd(workdir, filename)
    job_dir = tempfile.mkdtemp(prefix="judge_job_")
    try:
        full_argv = wrap_command(argv, workdir, backend)
        out_path = os.path.join(job_dir, "compile_out.txt")
        with open(out_path, "wb") as fo:
            proc = subprocess.Popen(
                full_argv, cwd=workdir, stdin=subprocess.DEVNULL, stdout=fo,
                stderr=subprocess.STDOUT, env=sandbox_env(workdir),
                preexec_fn=_limit_setter(COMPILE_TIMEOUT_SEC, None, process_limit=256),
                start_new_session=True,
            )
            try:
                rc = proc.wait(timeout=COMPILE_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                proc.wait()
                return False, "Compilation timed out."
        output = _read_capped(out_path).strip()
        if rc != 0:
            return False, output
        return True, ""
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
