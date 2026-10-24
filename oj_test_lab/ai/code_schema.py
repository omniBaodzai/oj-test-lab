"""Strict validation of the AI's code proposal. Text-level checks only: the code is NEVER parsed or run."""

from __future__ import annotations

import re
from typing import Any

from .errors import AIResponseError
from .schema import _check_text

SCHEMA_NAME = "code_proposal"
MAX_CODE_CHARS = 20_000

STRING_FIELDS: dict[str, int] = {
    "problem_id": 64,
    "algorithm_explanation": 4000,
    "complexity": 2000,
    "test_strategy": 4000,
    "numeric_range_analysis": 4000,
}
CODE_FIELDS = ("generator_code", "reference_code")
LIST_FIELDS: dict[str, tuple[int, int, int]] = {
    "edge_cases": (1, 50, 500),
    "adversarial_tests": (1, 30, 500),
    "assumptions": (0, 30, 800),
    "risks": (0, 30, 800),
}
QUESTION_MAX_ITEMS = 20
QUESTION_MAX_LEN = 800
FIELD_ORDER = ("problem_id", "algorithm_explanation", "complexity", "test_strategy", "numeric_range_analysis",
               "generator_code", "reference_code", "edge_cases", "adversarial_tests", "assumptions", "risks",
               "unresolved_questions")

JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{name: {"type": "string"} for name in (*STRING_FIELDS, *CODE_FIELDS)},
        **{name: {"type": "array", "items": {"type": "string"}} for name in LIST_FIELDS},
        "unresolved_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "blocking": {"type": "boolean"}},
                "required": ["question", "blocking"],
                "additionalProperties": False,
            },
        },
    },
    "required": list(FIELD_ORDER),
    "additionalProperties": False,
}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _check_code(name: str, value: object, errors: list[str]) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{name}: must be a string")
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        errors.append(f"{name}: must not be empty")
    elif len(text) > MAX_CODE_CHARS:
        errors.append(f"{name}: too long ({len(text)} > {MAX_CODE_CHARS})")
    elif _CONTROL.search(text):
        errors.append(f"{name}: contains control characters")
    else:
        return text.strip("\n") + "\n"
    return None


def validate_code_response(data: object, expected_problem_id: str) -> dict[str, Any]:
    """Return the cleaned response dict, or raise AIResponseError listing every problem found."""
    if not isinstance(data, dict):
        raise AIResponseError("AI response is not a JSON object")
    errors: list[str] = []
    for key in sorted(set(data) - set(FIELD_ORDER)):
        errors.append(f"{key[:40]!r}: unexpected field")  # e.g. an AI-supplied "approved"
    for key in FIELD_ORDER:
        if key not in data:
            errors.append(f"{key}: missing")
    clean: dict[str, Any] = {}
    for name, max_len in STRING_FIELDS.items():
        if name in data:
            value = _check_text(name, data[name], max_len, errors)
            if value is not None:
                clean[name] = value
    for name in CODE_FIELDS:
        if name in data:
            value = _check_code(name, data[name], errors)
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
    if "unresolved_questions" in data:
        raw_q = data["unresolved_questions"]
        if not isinstance(raw_q, list) or len(raw_q) > QUESTION_MAX_ITEMS:
            errors.append(f"unresolved_questions: must be a list of at most {QUESTION_MAX_ITEMS} objects")
        else:
            questions = []
            for i, item in enumerate(raw_q):
                if not isinstance(item, dict) or set(item) != {"question", "blocking"} or not isinstance(item.get("blocking"), bool):
                    errors.append(f"unresolved_questions[{i}]: must be an object with exactly 'question' (string) and 'blocking' (boolean)")
                    continue
                text = _check_text(f"unresolved_questions[{i}].question", item["question"], QUESTION_MAX_LEN, errors)
                if text is not None:
                    questions.append({"question": text, "blocking": item["blocking"]})
            if len(questions) == len(raw_q):
                clean["unresolved_questions"] = questions
    if clean.get("problem_id") is not None and clean["problem_id"] != expected_problem_id:
        errors.append("problem_id: does not match the specification")
    if "generator_code" in clean and not re.search(r"^def\s+generate\s*\(", clean["generator_code"], re.MULTILINE):
        errors.append("generator_code: must define `def generate(seed, count)`")
    if "reference_code" in clean and not re.search(r"\bmain\s*\(", clean["reference_code"]):
        errors.append("reference_code: must define main()")
    if errors:
        shown = errors[:15] + ([f"... and {len(errors) - 15} more"] if len(errors) > 15 else [])
        raise AIResponseError("AI response does not match the code proposal schema: " + "; ".join(shown))
    return {name: clean[name] for name in FIELD_ORDER}


# Advisory text patterns for the human reviewer. They are NOT a security check and cannot prove safety.
_PY_PATTERNS = (
    (r"\b(eval|exec|compile|__import__|globals|getattr|setattr)\s*\(", "dynamic code execution / reflection"),
    (r"^\s*(import|from)\s+(os|sys|subprocess|socket|shutil|ctypes|pathlib|requests|urllib|http|importlib|multiprocessing|threading|pickle|marshal|builtins)\b",
     "imports a system/network/process module"),
    (r"\b(open|input|breakpoint)\s*\(", "file or console I/O call"),
)
_CPP_PATTERNS = (
    (r"\b(system|popen|fork|execl|execv|execvp|execlp|socket|connect|fopen|freopen|remove|rename)\s*\(", "process, network or file call"),
    (r"#\s*include\s*[<\"](fstream|filesystem|thread|future|unistd\.h|sys/|windows\.h|dlfcn\.h|winsock)", "includes a file/process/network header"),
    (r"\b(asm|__asm__)\b", "inline assembly"),
    (r"#\s*include\s*\"", "includes a local file"),
)


def static_warnings(generator_code: str, reference_code: str) -> list[str]:
    """Advisory regex findings for the reviewer. Absence of warnings does NOT mean the code is safe."""
    found: list[str] = []
    for label, code, patterns in (("generator_code", generator_code, _PY_PATTERNS), ("reference_code", reference_code, _CPP_PATTERNS)):
        for pattern, message in patterns:
            if re.search(pattern, code, re.MULTILINE):
                found.append(f"{label}: {message}")
    return found
