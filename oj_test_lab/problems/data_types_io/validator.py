"""Strict input validator for `data-types-io`."""

from __future__ import annotations

import re
import struct
from fractions import Fraction

from . import spec

_INT_RE = re.compile(r"-?(0|[1-9][0-9]*)")
_DEC_RE = re.compile(r"-?(0|[1-9][0-9]*)(\.([0-9]+))?")
_CHAR_RE = re.compile(r"[A-Za-z]")
_NAMES = ("X", "Y", "C", "F", "D")


def _check_int(name: str, token: str, lo: int, hi: int, errors: list[str]) -> None:
    if not _INT_RE.fullmatch(token) or token == "-0":
        errors.append(f"{name}: not a canonical integer")
        return
    if not lo <= int(token) <= hi:
        errors.append(f"{name}: out of range [{lo}, {hi}]")


def _check_decimal(
    name: str, token: str, lo: int, hi: int, places: int, binary_width: int, errors: list[str]
) -> None:
    m = _DEC_RE.fullmatch(token)
    if not m:
        errors.append(f"{name}: not a plain finite decimal literal")
        return
    frac = m.group(3) or ""
    value = Fraction(token)
    if token.startswith("-") and value == 0:
        errors.append(f"{name}: negative zero is not allowed")
        return
    if len(frac) > places:
        errors.append(f"{name}: more than {places} fractional digits")
        return
    if not lo <= value <= hi:
        errors.append(f"{name}: out of range [{lo}, {hi}]")
        return
    if binary_width == 32:
        stored = Fraction(struct.unpack("<f", struct.pack("<f", float(value)))[0])
    else:
        stored = Fraction(float(value))
    if stored != value:
        errors.append(f"{name}: not exactly representable in binary{binary_width}")


def validate_input(text: str) -> list[str]:
    """Return a list of error messages; empty list means the input is valid."""
    if not isinstance(text, str):
        return ["input must be text"]
    if not text.isascii():
        return ["input must be ASCII"]
    if "\r" in text:
        return ["CR characters are not allowed (use LF line endings)"]
    if not text.endswith("\n"):
        return ["input must end with a single LF"]
    lines = text[:-1].split("\n")
    if len(lines) != spec.LINE_COUNT:
        return [f"expected exactly {spec.LINE_COUNT} lines, got {len(lines)}"]
    errors: list[str] = []
    for name, line in zip(_NAMES, lines):
        if line != line.strip() or " " in line or "\t" in line:
            errors.append(f"{name}: whitespace is not allowed")
    if errors:
        return errors
    x, y, c, f, d = lines
    _check_int("X", x, spec.X_MIN, spec.X_MAX, errors)
    _check_int("Y", y, spec.Y_MIN, spec.Y_MAX, errors)
    if not _CHAR_RE.fullmatch(c):
        errors.append("C: must be exactly one ASCII letter")
    _check_decimal("F", f, spec.F_MIN, spec.F_MAX, spec.F_PLACES, 32, errors)
    _check_decimal("D", d, spec.D_MIN, spec.D_MAX, spec.D_PLACES, 64, errors)
    return errors


def canonical_key(text: str) -> tuple[int, int, str, Fraction, Fraction]:
    """Semantic identity of a valid input (so '0.5' and '0.50' are duplicates)."""
    x, y, c, f, d = text[:-1].split("\n")
    return int(x), int(y), c, Fraction(f), Fraction(d)
