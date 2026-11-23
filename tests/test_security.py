"""Security tests for the judge. Run from the project root:

    python -m unittest tests/test_security.py -v

Sandbox tests need bubblewrap or util-linux unshare; they are skipped otherwise.
"""

import os
import socket
import sys
import threading
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from judge_engine import restrictions, selftest  # noqa: E402
from judge_engine.engine import TestCase, judge_submission  # noqa: E402
from judge_engine.sandbox import detect_backend  # noqa: E402

BACKEND = detect_backend()
NEEDS_SANDBOX = unittest.skipIf(BACKEND in (None, "none"), "no sandbox tool available")


class SourceCheckTests(unittest.TestCase):
    """Advisory checks: fast, readable messages, not the security boundary."""

    def test_blocks_networking_imports(self):
        self.assertTrue(restrictions.find_violations("python", "import socket\n"))
        self.assertTrue(restrictions.find_violations("python", "import os, socket as s\n"))
        self.assertTrue(restrictions.find_violations("python", "from urllib.request import urlopen\n"))

    def test_blocks_process_control(self):
        self.assertTrue(restrictions.find_violations("python", "import os\nos.system('ls')\n"))
        self.assertTrue(restrictions.find_violations("python", "import subprocess as sp\n"))
        self.assertTrue(restrictions.find_violations("cpp", "#include <sys/socket.h>\n"))
        self.assertTrue(restrictions.find_violations("cpp", "#include <unistd.h>\nint main(){fork();}"))
        self.assertTrue(restrictions.find_violations("java", "import java.net.Socket;\nclass Main{}"))

    def test_allows_normal_code(self):
        self.assertEqual(restrictions.find_violations("python", "import sys, math\nprint(1)\n"), [])
        self.assertEqual(restrictions.find_violations("python", "x=1\n# import socket\n"), [])
        self.assertEqual(restrictions.find_violations("cpp", "#include <iostream>\nint main(){}"), [])
        self.assertEqual(restrictions.find_violations("java", "import java.util.*;\nclass Main{}"), [])


@NEEDS_SANDBOX
class SandboxTests(unittest.TestCase):
    AB = [TestCase("3 4\n", "7\n"), TestCase("1 1\n", "2\n")]

    def test_selftest_network_and_process_isolation(self):
        _, checks = selftest.run_selftest()
        by = {c.name: c for c in checks}
        self.assertTrue(by["network"].passed, by["network"].detail)
        self.assertTrue(by["workdir"].passed, by["workdir"].detail)
        self.assertTrue(by["process"].passed, by["process"].detail)

    @unittest.skipIf(BACKEND != "bwrap", "file isolation is only guaranteed by bwrap")
    def test_selftest_file_isolation_bwrap(self):
        _, checks = selftest.run_selftest()
        by = {c.name: c for c in checks}
        self.assertTrue(by["read"].passed, by["read"].detail)
        self.assertTrue(by["write"].passed, by["write"].detail)

    def test_obfuscated_socket_is_blocked_by_sandbox(self):
        """Bypass the source check with string concatenation; the kernel must still block it."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        srv.settimeout(15)
        port = srv.getsockname()[1]
        hits = []

        def accept_once():
            try:
                conn, _ = srv.accept()
                hits.append(1)
                conn.close()
            except OSError:
                pass

        threading.Thread(target=accept_once, daemon=True).start()
        code = (
            'exec("imp"+"ort so"+"cket")\n'
            "s = socket.socket(); s.settimeout(3)\n"
            "try:\n"
            f"    s.connect(('127.0.0.1', {port})); print('CONNECTED')\n"
            "except OSError:\n"
            "    print('BLOCKED')\n"
        )
        try:
            r = judge_submission("python", code, [TestCase("", "BLOCKED\n")], 5, 256)
        finally:
            srv.close()
        self.assertEqual(r.test_results[0].stdout.strip(), "BLOCKED")
        self.assertEqual(hits, [], "host listener received a connection from the sandbox")

    def test_restricted_code_never_compiles_or_runs(self):
        r = judge_submission("cpp", "#include <sys/socket.h>\nint main(){}", self.AB, 1, 256)
        self.assertEqual(r.verdict.value, "Restricted Code")
        self.assertEqual(r.total, 2)
        self.assertEqual(r.test_results, [])

    def test_normal_solutions_still_accepted(self):
        cpp = ('#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;'
               'std::cout<<a+b<<"\\n";}')
        self.assertEqual(judge_submission("cpp", cpp, self.AB, 1, 256).verdict.value, "Accepted")
        py = "a, b = map(int, input().split())\nprint(a + b)\n"
        self.assertEqual(judge_submission("python", py, self.AB, 1, 256).verdict.value, "Accepted")

    def test_infinite_loop_is_time_limited(self):
        r = judge_submission("python", "while True:\n    pass\n", self.AB[:1], 1, 256)
        self.assertEqual(r.verdict.value, "Time Limit Exceeded")

    def test_compile_error_reported(self):
        r = judge_submission("cpp", "int main({", self.AB, 1, 256)
        self.assertEqual(r.verdict.value, "Compilation Error")


if __name__ == "__main__":
    unittest.main()
