"""Strict schema for the AI's analysis. Nothing is invented: a missing or malformed field is an error."""

from __future__ import annotations

import json
import re
from typing import Any

from ..specs.store import _reject_duplicate_keys
from .errors import AIResponseError

SCHEMA_NAME = "problem_analysis"

# field -> max length
STRING_FIELDS: dict[str, int] = {
    "problem_id": 64,
    "summary": 2000,
    "input_analysis": 4000,
    "output_analysis": 4000,
    "constraint_interpretation": 4000,
    "suggested_algorithm": 4000,
    "generator_strategy": 4000,
    "reference_approach": 4000,
}
# field -> (min items, max items, max item length)
LIST_FIELDS: dict[str, tuple[int, int, int]] = {
    "test_categories": (1, 30, 500),
    "edge_cases": (1, 50, 500),
    "ambiguities": (0, 30, 800),
    "clarification_questions": (0, 30, 800),
    "risks_and_assumptions": (0, 30, 800),
}
FIELD_ORDER = tuple(STRING_FIELDS) + tuple(LIST_FIELDS)

# Plain subset of JSON Schema that strict structured-output routes accept; limits are checked locally.
JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{name: {"type": "string"} for name in STRING_FIELDS},
        **{name: {"type": "array", "items": {"type": "string"}} for name in LIST_FIELDS},
    },
    "required": list(FIELD_ORDER),
    "additionalProperties": False,
}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # allows \t \n \r


def _check_text(name: str, value: object, max_len: int, errors: list[str]) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{name}: must be a string")
    elif not value.strip():
        errors.append(f"{name}: must not be empty")
    elif len(value) > max_len:
        errors.append(f"{name}: too long ({len(value)} > {max_len})")
    elif _CONTROL.search(value):
        errors.append(f"{name}: contains control characters")
    else:
        return value.strip()
    return None


def validate_analysis(data: object, expected_problem_id: str) -> dict[str, Any]:
    """Return the cleaned analysis dict or raise AIResponseError listing every problem found."""
    if not isinstance(data, dict):
        raise AIResponseError("AI response is not a JSON object")
    errors: list[str] = []
    for key in sorted(set(data) - set(FIELD_ORDER)):
        errors.append(f"{key[:40]!r}: unexpected field")  # includes e.g. an AI-supplied "approved"
    for key in FIELD_ORDER:
        if key not in data:
            errors.append(f"{key}: missing")
    clean: dict[str, Any] = {}
    for name, max_len in STRING_FIELDS.items():
        if name in data:
            value = _check_text(name, data[name], max_len, errors)
            if value is not None:
                clean[name] = value
    for name, (lo, hi, item_max) in LIST_FIELDS.items():
        if name not in data:
            continue
        raw = data[name]
        if not isinstance(raw, list):
            errors.append(f"{name}: must be a list of strings")
        elif not lo <= len(raw) <= hi:
            errors.append(f"{name}: must contain between {lo} and {hi} items (got {len(raw)})")
        else:
            items = [_check_text(f"{name}[{i}]", item, item_max, errors) for i, item in enumerate(raw)]
            if None not in items:
                clean[name] = items
    if clean.get("problem_id") is not None and clean["problem_id"] != expected_problem_id:
        errors.append("problem_id: does not match the analyzed specification")
    if errors:
        shown = errors[:15] + ([f"... and {len(errors) - 15} more"] if len(errors) > 15 else [])
        raise AIResponseError("AI response does not match the proposal schema: " + "; ".join(shown))
    return {name: clean[name] for name in FIELD_ORDER}


def parse_json_text(text: str) -> object:
    """Parse model output as JSON. Tolerates exactly one outer ```json fence; nothing else."""
    body = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", body, re.DOTALL)
    if fence:
        body = fence.group(1)
    try:
        return json.loads(body, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise AIResponseError(f"AI response is not valid JSON ({str(exc)[:80]})") from None
