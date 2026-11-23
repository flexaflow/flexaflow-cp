"""Verdicts and result data structures for the judge engine."""

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    AC = "Accepted"
    WA = "Wrong Answer"
    TLE = "Time Limit Exceeded"
    MLE = "Memory Limit Exceeded"
    RE = "Runtime Error"
    CE = "Compilation Error"
    IE = "Internal Error"          # judge-side failure, not the submitter's fault
    RESTRICTED = "Restricted Code" # uses networking/process APIs not allowed in the contest
    PENDING = "Pending"


@dataclass
class TestCaseResult:
    index: int
    verdict: Verdict
    exec_time_ms: float = 0.0
    memory_kb: int = 0
    stdout: str = ""
    stderr: str = ""
    message: str = ""              # human-readable extra info (e.g. diff snippet)


@dataclass
class SubmissionResult:
    verdict: Verdict = Verdict.PENDING
    compile_output: str = ""
    max_time_ms: float = 0.0
    max_memory_kb: int = 0
    test_results: list = field(default_factory=list)  # list[TestCaseResult]
    passed: int = 0
    total: int = 0

    def summary(self) -> str:
        return f"{self.verdict.value} ({self.passed}/{self.total} test cases)"
