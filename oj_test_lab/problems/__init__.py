"""Problem registry. To add a problem, create a subpackage exposing PROBLEM and register it here."""

from __future__ import annotations

from ..models import Problem
from .data_types_io import PROBLEM as _DATA_TYPES_IO

REGISTRY: dict[str, Problem] = {p.id: p for p in (_DATA_TYPES_IO,)}


def get_problem(problem_id: str) -> Problem:
    try:
        return REGISTRY[problem_id]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise KeyError(f"Unknown problem {problem_id!r}. Available: {known}") from None
