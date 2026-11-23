"""Entry point for judging a submission.

Usage:
    from judge_engine.engine import judge_submission, TestCase

    result = judge_submission(
        language="cpp",
        code=source_code,
        test_cases=[TestCase(input="1 2\\n", expected_output="3\\n"), ...],
        time_limit_sec=2.0,
        mem_limit_mb=256,
    )
    print(result.summary())

Order of checks for every submission:
  1. Source check (restrictions.py): obvious networking/process code -> Restricted Code.
  2. Compile inside the sandbox.
  3. Run each test inside the sandbox.
"""

import shutil
import signal
import tempfile
from dataclasses import dataclass
from typing import List, Optional

from . import restrictions
from .languages import get_language
from .sandbox import (SandboxUnavailable, compile_in_sandbox, detect_backend,
                      run_in_sandbox)
from .verdict import SubmissionResult, TestCaseResult, Verdict


@dataclass
class TestCase:
    input: str
    expected_output: str


def _normalize(s: str) -> str:
    """Standard judge comparison: ignore trailing whitespace per line and
    trailing blank lines, but keep everything else exact."""
    lines = [line.rstrip() for line in s.rstrip("\n").split("\n")]
    return "\n".join(lines)


def _classify(outcome: dict, expected: str, time_limit_sec: float,
              mem_limit_mb: Optional[int]) -> Verdict:
    rc = outcome["returncode"]
    sig = None
    if rc is not None:
        if rc < 0:
            sig = -rc
        elif rc > 128:
            sig = rc - 128  # sandbox wrappers report 128+N for signal deaths

    if outcome["timed_out"]:
        return Verdict.TLE
    if sig == signal.SIGXCPU:
        return Verdict.TLE
    if sig is not None and outcome["elapsed_ms"] >= time_limit_sec * 1000 - 50:
        return Verdict.TLE
    if mem_limit_mb and sig in (signal.SIGSEGV, signal.SIGABRT, signal.SIGKILL, signal.SIGBUS):
        return Verdict.MLE
    if rc != 0:
        return Verdict.RE
    if _normalize(outcome["stdout"]) != _normalize(expected):
        return Verdict.WA
    return Verdict.AC


def judge_submission(
    language: str,
    code: str,
    test_cases: List[TestCase],
    time_limit_sec: float = 2.0,
    mem_limit_mb: Optional[int] = 256,
) -> SubmissionResult:
    lang = get_language(language)
    result = SubmissionResult(total=len(test_cases))

    violations = restrictions.find_violations(language, code)
    if violations:
        result.verdict = Verdict.RESTRICTED
        result.compile_output = "Not allowed in this contest: " + "; ".join(violations)
        return result

    backend = detect_backend()
    if backend is None:
        raise SandboxUnavailable("No sandbox tool available; refusing to run untrusted code.")

    workdir = tempfile.mkdtemp(prefix="judge_")
    try:
        filename = lang.source_filename(code)
        with open(f"{workdir}/{filename}", "w") as f:
            f.write(code)

        ok, output = compile_in_sandbox(workdir, lang, filename, backend)
        if not ok:
            result.verdict = Verdict.CE
            result.compile_output = output
            return result

        argv = lang.run_cmd(workdir, filename)
        # Java gets no RLIMIT_AS (JVM needs headroom); everything else does.
        effective_mem_limit = None if lang.compiled_artifact_is_class else mem_limit_mb

        worst_verdict = Verdict.AC
        for i, tc in enumerate(test_cases):
            job_dir = tempfile.mkdtemp(prefix="judge_job_")
            try:
                outcome = run_in_sandbox(
                    argv, workdir, job_dir, tc.input, time_limit_sec,
                    effective_mem_limit, backend=backend,
                )
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)

            verdict = _classify(outcome, tc.expected_output, time_limit_sec,
                                effective_mem_limit)

            result.test_results.append(TestCaseResult(
                index=i,
                verdict=verdict,
                exec_time_ms=outcome["elapsed_ms"],
                stdout=outcome["stdout"],
                stderr=outcome["stderr"],
            ))
            result.max_time_ms = max(result.max_time_ms, outcome["elapsed_ms"])

            if verdict == Verdict.AC:
                result.passed += 1
            elif worst_verdict == Verdict.AC:
                # ICPC-style: report the first failure as the overall verdict.
                worst_verdict = verdict

        result.verdict = worst_verdict
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
