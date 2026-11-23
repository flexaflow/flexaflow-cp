"""
Language configuration for the judge.

Each LanguageConfig knows how to name the source file, how to compile it
(if needed), and how to build the run command. Add a new language by adding
one entry to LANGUAGES -- nothing else in the engine needs to change.

Deployment note: on the lab PC make sure these binaries are installed and on
PATH:
    C        -> gcc
    C++      -> g++
    Java     -> a JDK (javac + java), not just a JRE
    Python   -> python3
    PyPy     -> pypy3   (apt install pypy3, or download from pypy.org)
"""

import re
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class LanguageConfig:
    key: str                       # identifier used in submissions table, e.g. "cpp"
    display_name: str
    source_filename: Callable[[str], str]   # (code) -> filename to write
    compile_cmd: Optional[Callable[[str, str], List[str]]]  # (workdir, filename) -> argv, or None
    run_cmd: Callable[[str, str], List[str]]                # (workdir, filename) -> argv
    compiled_artifact_is_class: bool = False  # True for Java (run by class name, not file)


def _java_class_name(code: str) -> str:
    """Java requires the file to be named after its public class."""
    m = re.search(r'public\s+(?:final\s+|abstract\s+)?class\s+(\w+)', code)
    return m.group(1) if m else "Main"


LANGUAGES = {
    "c": LanguageConfig(
        key="c",
        display_name="C",
        source_filename=lambda code: "main.c",
        compile_cmd=lambda wd, fn: [
            "gcc", fn, "-O2", "-static", "-o", "main.out", "-lm",
        ],
        run_cmd=lambda wd, fn: ["./main.out"],
    ),
    "cpp": LanguageConfig(
        key="cpp",
        display_name="C++",
        source_filename=lambda code: "main.cpp",
        compile_cmd=lambda wd, fn: [
            "g++", fn, "-O2", "-static", "-std=c++17", "-o", "main.out",
        ],
        run_cmd=lambda wd, fn: ["./main.out"],
    ),
    "java": LanguageConfig(
        key="java",
        display_name="Java",
        source_filename=lambda code: f"{_java_class_name(code)}.java",
        compile_cmd=lambda wd, fn: ["javac", fn],
        run_cmd=lambda wd, fn: ["java", "-Xmx256m", fn[:-len(".java")]],
        compiled_artifact_is_class=True,
    ),
    "python": LanguageConfig(
        key="python",
        display_name="Python 3",
        source_filename=lambda code: "main.py",
        compile_cmd=None,
        run_cmd=lambda wd, fn: ["python3", fn],
    ),
    "pypy": LanguageConfig(
        key="pypy",
        display_name="PyPy 3",
        source_filename=lambda code: "main.py",
        compile_cmd=None,
        run_cmd=lambda wd, fn: ["pypy3", fn],
    ),
}


def get_language(key: str) -> LanguageConfig:
    if key not in LANGUAGES:
        raise ValueError(f"Unsupported language: {key}")
    return LANGUAGES[key]
