"""Source-level checks: reject obvious networking and process-control code
before compiling, with a readable message.

IMPORTANT: these checks are a convenience, not the security boundary. Code can
hide imports (e.g. __import__("so" + "cket")). The real enforcement is the
sandbox in sandbox.py, which has no network and cannot see host files, so
even code that slips past these checks cannot reach the network.
"""

import re
from typing import List

# ------------------------------------------------------------ Python ----

_PY_MODULES = [
    "socket", "_socket", "ssl", "http", "urllib", "requests", "ftplib", "smtplib",
    "poplib", "imaplib", "telnetlib", "xmlrpc", "socketserver", "asyncio", "select",
    "selectors", "subprocess", "multiprocessing", "ctypes", "pty", "importlib",
]
_PY_IMPORT = re.compile(
    r"^[ \t]*(?:import[ \t]+([^\n#;]+)|from[ \t]+([\w\.]+)[ \t]+import\b)", re.MULTILINE
)
_PY_CALLS = re.compile(
    r"\b(os\.(system|popen|fork|forkpty|kill|spawn\w*|exec\w*|posix_spawn\w*)|__import__|eval\s*\(\s*['\"]\s*import)"
)

# ------------------------------------------------------------- C / C++ ----

_C_HEADERS = re.compile(
    r"#\s*include\s*[<\"]\s*(sys/socket\.h|sys/un\.h|netinet/[^>\"]+|netdb\.h|arpa/inet\.h|"
    r"sys/ptrace\.h|dlfcn\.h|sys/mount\.h|spawn\.h|sys/wait\.h)\s*[>\"]"
)
_C_CALLS = re.compile(
    r"\b(socket|connect|bind|listen|accept|getaddrinfo|gethostbyname|fork|vfork|"
    r"system|popen|execve|execvp|execv|execl|execlp|execle|dlopen|ptrace|mount)\s*\("
)
_C_ASM = re.compile(r"\b(__asm__|asm)\s*\(")

# ----------------------------------------------------------------- Java ----

_JAVA = re.compile(
    r"\bjava\.(net|rmi)\b|\bjavax\.net\b|\bjava\.nio\.channels\b|\bProcessBuilder\b|"
    r"\bRuntime\s*\.\s*getRuntime\s*\(\s*\)\s*\.\s*exec\b|\bjava\.lang\.ProcessImpl\b|"
    r"\bjava\.lang\.reflect\b"
)


def find_violations(language: str, code: str) -> List[str]:
    """Return human-readable reasons, or an empty list if the code looks allowed."""
    reasons: List[str] = []

    if language in ("python", "pypy"):
        for m in _PY_IMPORT.finditer(code):
            if m.group(2):  # from X import ...
                names = [m.group(2)]
            else:           # import a, b.c as d
                names = [part.strip().split()[0] for part in m.group(1).split(",") if part.strip()]
            for raw in names:
                top = raw.split(".")[0]
                if top in _PY_MODULES:
                    reasons.append(f"module '{top}' is not allowed")
        for m in _PY_CALLS.finditer(code):
            reasons.append(f"'{m.group(0)}' is not allowed")

    elif language in ("c", "cpp"):
        for m in _C_HEADERS.finditer(code):
            reasons.append(f"header <{m.group(1)}> is not allowed")
        for m in _C_CALLS.finditer(code):
            reasons.append(f"call to '{m.group(1)}' is not allowed")
        if _C_ASM.search(code):
            reasons.append("inline assembly is not allowed")

    elif language == "java":
        for m in _JAVA.finditer(code):
            reasons.append(f"'{m.group(0).strip()}' is not allowed")

    # De-duplicate while keeping order.
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out
