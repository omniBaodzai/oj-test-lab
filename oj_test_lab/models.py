"""Core models and the Problem definition interface."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

GENERATOR_VERSION = "1.0.0"

PROBLEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class CompareMode(str, Enum):
    EXACT = "EXACT"
    TRIM = "TRIM"


@dataclass(frozen=True)
class TestCase:
    __test__ = False  # not a pytest class

    id: int
    category: str
    input_text: str


@dataclass(frozen=True)
class Candidate:
    """A solution under evaluation.

    `kind` documents how it runs. The MVP only has "simulated-python":
    trusted built-in functions that imitate a bug. A future sandboxed runner
    can add kinds such as "compiled-cpp" with the same `run` signature.
    """

    id: str
    description: str
    run: Callable[[str], str]
    kind: str = "simulated-python"


@dataclass(frozen=True)
class CppCandidate:
    """A trusted C++ demonstration solution shipped with the project (real execution)."""

    id: str
    description: str
    source_path: Path


@dataclass(frozen=True)
class Problem:
    id: str
    title: str
    # generate(count, seed) -> list of (category, input_text)
    generate: Callable[[int, int], list[tuple[str, str]]]
    # validate(input_text) -> list of error messages (empty = valid)
    validate: Callable[[str], list[str]]
    # reference(input_text) -> expected output text (input must be valid)
    reference: Callable[[str], str]
    wrong_solutions: tuple[Candidate, ...] = ()
    cpp_candidates: tuple[CppCandidate, ...] = ()
    compare_mode: CompareMode = CompareMode.EXACT
    default_count: int = 28
    min_count: int = 1
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not PROBLEM_ID_RE.match(self.id):
            raise ValueError(f"Unsafe problem id: {self.id!r}")
