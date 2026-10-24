"""SIMULATED incorrect solutions for `data-types-io`.

These are trusted in-process Python functions that imitate typical bugs.
They are NOT real C++/Java programs; detecting them says nothing about real
compiled code until a sandboxed runner exists.
"""

from __future__ import annotations

from ...models import Candidate
from .reference import format_fixed


def _lines(text: str) -> list[str]:
    return text[:-1].split("\n")


def _join(x: str, y: str, c: str, f: str, d: str) -> str:
    return "\n".join([x, y, c, f, d]) + "\n"


def _wrap_int32(value: int) -> int:
    return (value + 2**31) % 2**32 - 2**31


def wrong_y_int32(text: str) -> str:
    x, y, c, f, d = _lines(text)
    return _join(x, str(_wrap_int32(int(y))), c, format_fixed(f, 2), format_fixed(d, 9))


def wrong_f_decimals(text: str) -> str:
    x, y, c, f, d = _lines(text)
    return _join(x, y, c, format_fixed(f, 3), format_fixed(d, 9))


def wrong_d_decimals(text: str) -> str:
    x, y, c, f, d = _lines(text)
    return _join(x, y, c, format_fixed(f, 2), format_fixed(d, 6))


def wrong_negative(text: str) -> str:
    """Drops the sign of X and F (e.g. uses abs())."""
    x, y, c, f, d = _lines(text)
    return _join(str(abs(int(x))), y, c, format_fixed(f.lstrip("-"), 2), format_fixed(d, 9))


WRONG_SOLUTIONS = (
    Candidate("wrong-y-int32-overflow", "Simulates storing Y in a signed 32-bit integer (wraps modulo 2^32)", wrong_y_int32),
    Candidate("wrong-f-decimals", "Prints F with 3 decimal places instead of 2", wrong_f_decimals),
    Candidate("wrong-d-decimals", "Prints D with 6 decimal places instead of 9", wrong_d_decimals),
    Candidate("wrong-negative-sign", "Drops the sign of X and F (abs)", wrong_negative),
)
