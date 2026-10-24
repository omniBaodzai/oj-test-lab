"""User-authored Problem Specification (data only).

This is NOT the executable `oj_test_lab.models.Problem` used by the engine. A
spec is a natural-language description written by a human. Validation here is
STRUCTURAL only (types, presence, lengths, id format). The tool does not
understand the statement or the constraints; whether they are consistent or
solvable is a semantic question left to humans (and, later, reviewed AI output).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..models import PROBLEM_ID_RE

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

MAX_TITLE = 200
MAX_STATEMENT = 20_000
MAX_DESCRIPTION = 5_000
MAX_CONSTRAINT = 1_000
MAX_CONSTRAINTS = 50
MAX_SAMPLE_TEXT = 5_000
MAX_SAMPLES = 20
MIN_TEST_COUNT, MAX_TEST_COUNT = 1, 10_000

# Names Windows refuses as file names; an id equal to one would break "<id>.json".
RESERVED_IDS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_FIELDS = (
    "schema_version", "id", "title", "statement", "input_description",
    "output_description", "constraints", "samples", "desired_test_count",
    "created_at", "updated_at",
)
_REQUIRED = (
    "id", "title", "statement", "input_description", "output_description",
    "constraints", "desired_test_count",
)


class SpecValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class Sample:
    input: str
    output: str


@dataclass(frozen=True)
class ProblemSpec:
    id: str
    title: str
    statement: str
    input_description: str
    output_description: str
    constraints: tuple[str, ...]
    desired_test_count: int
    samples: tuple[Sample, ...] = field(default_factory=tuple)
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Stable key order; this is the on-disk JSON schema."""
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "title": self.title,
            "statement": self.statement,
            "input_description": self.input_description,
            "output_description": self.output_description,
            "constraints": list(self.constraints),
            "samples": [{"input": s.input, "output": s.output} for s in self.samples],
            "desired_test_count": self.desired_test_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def check_spec_id(value: object) -> list[str]:
    if not isinstance(value, str):
        return ["id: must be a string"]
    if not PROBLEM_ID_RE.match(value) or value.endswith("-") or "--" in value:
        return ["id: must match [a-z0-9][a-z0-9-]{0,63}, no trailing '-' or '--'"]
    if value in RESERVED_IDS:
        return [f"id: {value!r} is a reserved file name on Windows"]
    return []


def _text(name: str, value: object, max_len: int, errors: list[str]) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{name}: must be a string")
        return None
    if not value.strip():
        errors.append(f"{name}: must not be empty")
        return None
    if len(value) > max_len:
        errors.append(f"{name}: too long ({len(value)} > {max_len} characters)")
        return None
    if "\x00" in value:
        errors.append(f"{name}: must not contain NUL characters")
        return None
    return value


def _timestamp(name: str, value: object, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{name}: must be an ISO-8601 string")
        return
    try:
        datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"{name}: not a valid ISO-8601 timestamp")


def parse_spec(data: object, *, require_timestamps: bool = False) -> ProblemSpec:
    """Structurally validate a decoded JSON object. Raises SpecValidationError with ALL problems.

    `schema_version`, `samples`, `created_at`, `updated_at` are optional on import
    (the store fills the timestamps in); stored files must have timestamps.
    """
    if not isinstance(data, dict):
        raise SpecValidationError(["top level must be a JSON object"])
    errors: list[str] = []
    for key in sorted(set(data) - set(_FIELDS)):
        errors.append(f"{key}: unknown field")
    for key in _REQUIRED:
        if key not in data:
            errors.append(f"{key}: required field is missing")

    version = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        errors.append("schema_version: must be an integer")
    elif version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"schema_version: unsupported version {version} (supported: {list(SUPPORTED_SCHEMA_VERSIONS)})")

    if "id" in data:
        errors += check_spec_id(data["id"])
    title = _text("title", data["title"], MAX_TITLE, errors) if "title" in data else None
    statement = _text("statement", data["statement"], MAX_STATEMENT, errors) if "statement" in data else None
    in_desc = _text("input_description", data["input_description"], MAX_DESCRIPTION, errors) if "input_description" in data else None
    out_desc = _text("output_description", data["output_description"], MAX_DESCRIPTION, errors) if "output_description" in data else None

    constraints: list[str] = []
    if "constraints" in data:
        raw = data["constraints"]
        if not isinstance(raw, list):
            errors.append("constraints: must be a list of strings")
        elif not raw:
            errors.append("constraints: must contain at least one item")
        elif len(raw) > MAX_CONSTRAINTS:
            errors.append(f"constraints: at most {MAX_CONSTRAINTS} items")
        else:
            for i, item in enumerate(raw):
                value = _text(f"constraints[{i}]", item, MAX_CONSTRAINT, errors)
                if value is not None:
                    constraints.append(value)

    samples: list[Sample] = []
    raw_samples = data.get("samples", [])
    if not isinstance(raw_samples, list):
        errors.append("samples: must be a list of {input, output} objects")
    elif len(raw_samples) > MAX_SAMPLES:
        errors.append(f"samples: at most {MAX_SAMPLES} items")
    else:
        for i, item in enumerate(raw_samples):
            if not isinstance(item, dict) or set(item) != {"input", "output"}:
                errors.append(f"samples[{i}]: must be an object with exactly the keys 'input' and 'output'")
                continue
            s_in = _text(f"samples[{i}].input", item["input"], MAX_SAMPLE_TEXT, errors)
            s_out = _text(f"samples[{i}].output", item["output"], MAX_SAMPLE_TEXT, errors)
            if s_in is not None and s_out is not None:
                samples.append(Sample(s_in, s_out))

    count = data.get("desired_test_count")
    if "desired_test_count" in data:
        if isinstance(count, bool) or not isinstance(count, int):
            errors.append("desired_test_count: must be an integer")
        elif not MIN_TEST_COUNT <= count <= MAX_TEST_COUNT:
            errors.append(f"desired_test_count: must be between {MIN_TEST_COUNT} and {MAX_TEST_COUNT}")

    for key in ("created_at", "updated_at"):
        if key in data:
            _timestamp(key, data[key], errors)
        elif require_timestamps:
            errors.append(f"{key}: required field is missing")

    if errors:
        raise SpecValidationError(errors)
    return ProblemSpec(
        id=data["id"], title=title, statement=statement,  # type: ignore[arg-type]
        input_description=in_desc, output_description=out_desc,  # type: ignore[arg-type]
        constraints=tuple(constraints), desired_test_count=count,  # type: ignore[arg-type]
        samples=tuple(samples), created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""), schema_version=version,
    )
