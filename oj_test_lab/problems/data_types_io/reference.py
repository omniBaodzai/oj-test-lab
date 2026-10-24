"""Trusted reference solution for `data-types-io`.

Works on exact integers and decimal strings; no binary floating point.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from . import spec
from .validator import validate_input


def format_fixed(token: str, places: int) -> str:
    """Exact fixed-point formatting of a decimal literal (HALF_EVEN if rounding were needed)."""
    quantum = Decimal(1).scaleb(-places)
    value = Decimal(token).quantize(quantum, rounding=ROUND_HALF_EVEN)
    return f"{value:.{places}f}"


def solve(input_text: str) -> str:
    errors = validate_input(input_text)
    if errors:
        raise ValueError("invalid input: " + "; ".join(errors))
    x, y, c, f, d = input_text[:-1].split("\n")
    return "\n".join(
        [str(int(x)), str(int(y)), c, format_fixed(f, spec.F_PLACES), format_fixed(d, spec.D_PLACES)]
    ) + "\n"
