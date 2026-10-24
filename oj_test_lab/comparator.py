"""Output comparison.

Newline policy (both modes): CRLF and lone CR are normalised to LF first.

EXACT: after newline normalisation the texts must be identical. A missing or
       extra trailing newline, trailing spaces, and any internal whitespace
       difference are all mismatches.
TRIM:  after newline normalisation, trailing spaces/tabs of every line are
       removed and leading/trailing blank lines of the whole text are dropped.
       Internal whitespace ("1  2" vs "1 2") is NEVER collapsed.
"""

from __future__ import annotations

from .models import CompareMode


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _trim(text: str) -> str:
    lines = [line.rstrip(" \t") for line in normalize_newlines(text).split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def outputs_match(expected: str, actual: str, mode: CompareMode = CompareMode.EXACT) -> bool:
    if mode is CompareMode.EXACT:
        return normalize_newlines(expected) == normalize_newlines(actual)
    if mode is CompareMode.TRIM:
        return _trim(expected) == _trim(actual)
    raise ValueError(f"Unknown compare mode: {mode}")
