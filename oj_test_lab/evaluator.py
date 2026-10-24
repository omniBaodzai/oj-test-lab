"""Evaluate candidate solutions against a test suite."""

from __future__ import annotations

from dataclasses import dataclass

from .comparator import outputs_match
from .models import Candidate, CompareMode, TestCase


@dataclass(frozen=True)
class CandidateResult:
    candidate: Candidate
    detected_by: list[int]  # ids of tests where the output differs from the reference
    errored_on: list[int]  # ids of tests where the candidate raised an exception

    @property
    def escaped(self) -> bool:
        return not self.detected_by and not self.errored_on


def evaluate_candidate(
    candidate: Candidate,
    cases: list[TestCase],
    expected: dict[int, str],
    mode: CompareMode,
) -> CandidateResult:
    """Run a trusted in-process candidate. Untrusted code must never be passed here;
    a future sandboxed runner should provide Candidate objects with the same interface."""
    detected: list[int] = []
    errored: list[int] = []
    for case in cases:
        try:
            actual = candidate.run(case.input_text)
        except Exception:
            errored.append(case.id)
            continue
        if not outputs_match(expected[case.id], actual, mode):
            detected.append(case.id)
    return CandidateResult(candidate, detected, errored)
