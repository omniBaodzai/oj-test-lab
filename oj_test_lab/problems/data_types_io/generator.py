"""Deterministic test generator for `data-types-io`.

A fixed block of mandatory cases (normal, zero, negative, integer boundary,
character, float-format) is always emitted first; remaining slots are filled
with seeded pseudo-random cases. All values are valid by construction and are
re-validated by the engine anyway.
"""

from __future__ import annotations

import random
from decimal import Decimal

from . import spec
from .validator import canonical_key, validate_input

# (category, X, Y, C, F, D)
MANDATORY: tuple[tuple[str, int, int, str, str, str], ...] = (
    ("normal", 5, 123456789012, "a", "3.25", "2.5"),
    ("normal", 42, 100, "Z", "12.5", "100.125"),
    ("normal", 123456, 987654321, "m", "0.5", "7.75"),
    ("zero", 0, 0, "A", "0", "0"),
    ("zero", 0, 5, "q", "0.0", "0.25"),
    ("zero", 7, 0, "Q", "1.5", "0.0"),
    ("negative", -1, -1, "n", "-0.25", "-0.5"),
    ("negative", -123, -4567890123, "N", "-12.75", "-1000.125"),
    ("negative", -5, 9, "c", "-3.5", "2.0"),
    ("integer-boundary", spec.X_MAX, 2147483647, "B", "1000000", "1000000000"),
    ("integer-boundary", spec.X_MIN, 2147483648, "b", "-1000000", "-1000000000"),
    ("integer-boundary", 1, -2147483648, "k", "1.25", "1.5"),
    ("integer-boundary", -1, -2147483649, "K", "-1.25", "-1.5"),
    ("integer-boundary", spec.X_MAX, 10**18, "y", "0.75", "0.125"),
    ("integer-boundary", spec.X_MIN, -(10**18), "Y", "-0.75", "-0.125"),
    ("integer-boundary", 999999999, 4294967296, "w", "2.5", "3.5"),
    ("character", 11, 22, "a", "1", "1"),
    ("character", 11, 22, "z", "1", "1"),
    ("character", 11, 22, "A", "1", "1"),
    ("character", 11, 22, "Z", "1", "1"),
    ("float-format", 8, 8, "f", "0.25", "0.001953125"),
    ("float-format", -8, -8, "g", "-999999.75", "-0.001953125"),
    ("float-format", 9, 9, "h", "100.5", "999999999.998046875"),
    ("float-format", -9, -9, "j", "0.75", "0.123046875"),
)
MIN_COUNT = len(MANDATORY)


def _plain(value: Decimal) -> str:
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _render(x: int, y: int, c: str, f: str, d: str) -> str:
    return f"{x}\n{y}\n{c}\n{f}\n{d}\n"


def _random_case(rng: random.Random) -> str:
    x = rng.randint(spec.X_MIN, spec.X_MAX)
    y_scale = rng.choice((10**3, 2**31, 2**40, 10**15, spec.Y_MAX))
    y = rng.randint(-y_scale, y_scale)
    c = rng.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
    f = _plain(Decimal(rng.randint(-4 * spec.F_MAX, 4 * spec.F_MAX)) / 4)
    d_bound = 512 * spec.D_MAX
    d = _plain(Decimal(rng.randint(-d_bound, d_bound)) / 512)
    return _render(x, y, c, f, d)


def generate(count: int, seed: int) -> list[tuple[str, str]]:
    if count < MIN_COUNT:
        raise ValueError(
            f"count must be >= {MIN_COUNT} to keep all mandatory coverage (got {count})"
        )
    cases = [(cat, _render(x, y, c, f, d)) for cat, x, y, c, f, d in MANDATORY]
    seen = {canonical_key(text) for _, text in cases}
    rng = random.Random(seed)
    while len(cases) < count:
        text = _random_case(rng)
        key = canonical_key(text)
        if key in seen or validate_input(text):
            continue
        seen.add(key)
        cases.append(("random", text))
    return cases
