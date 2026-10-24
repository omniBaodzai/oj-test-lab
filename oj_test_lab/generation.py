"""Real test generation from an APPROVED code proposal.

AI code is only ever run by `sandbox.UntrustedSandbox` (inside a container). This module never
executes, compiles or imports it. Any failure rejects the whole run; nothing is replaced or fabricated.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .ai.code_model import CodeProposal
from .ai.code_store import CodeProposalStore
from .comparator import outputs_match
from .models import GENERATOR_VERSION, CompareMode
from .paths import numbered_name, read_text, safe_join, write_text
from .sandbox import UntrustedSandbox
from .specs.store import SpecStore
from .suite import SuiteError, _dump, sha256, write_zip

SUITES_DIRNAME = "test-suites"
MIN_COUNT, MAX_COUNT = 1, 1000
MAX_SEED = 2**32 - 1
MAX_CASE_BYTES = 1_000_000
_SUITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_NUM = r"[+-]?\s*(?:\d+\s*\^\s*\d+|\d+)"
_RANGE_RE = re.compile(rf"^\s*({_NUM})\s*(?:<=|≤)\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*(?:<=|≤)\s*({_NUM})\s*\.?\s*$")


class GenerationError(SuiteError):
    """The run was rejected; `details` lists the individual failures."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


def _num(text: str) -> int:
    t = text.replace(" ", "")
    sign = -1 if t.startswith("-") else 1
    t = t.lstrip("+-")
    if "^" in t:
        base, exp = t.split("^")
        if int(exp) > 40:
            raise ValueError("exponent too large")
        return sign * int(base) ** int(exp)
    return sign * int(t)


def parse_ranges(constraints: tuple[str, ...] | list[str]) -> list[tuple[list[str], int, int]]:
    out = []
    for line in constraints:
        m = _RANGE_RE.match(line)
        if m:
            try:
                lo, hi = _num(m.group(1)), _num(m.group(3))
            except ValueError:
                continue
            out.append(([v.strip() for v in m.group(2).split(",")], lo, hi))
    return out


def check_ranges(constraints, inputs: list[str]) -> tuple[str, list[str]]:
    """Best-effort mechanical check. Returns (status, errors); status is 'checked' or 'skipped: reason'."""
    ranges = parse_ranges(constraints)
    if not ranges:
        return "skipped: no constraint of the form 'LO <= var[, var] <= HI' was recognised", []
    names = {v for r in ranges for v in r[0]}
    lo, hi = min(r[1] for r in ranges), max(r[2] for r in ranges)
    errors = []
    for i, text in enumerate(inputs, 1):
        tokens = text.split()
        if len(tokens) != len(names):
            return f"skipped: input has {len(tokens)} tokens but {len(names)} constrained variables", []
        for tok in tokens:
            try:
                value = int(tok)
            except ValueError:
                return "skipped: input contains non-integer tokens", []
            if not lo <= value <= hi:
                errors.append(f"test {i}: value {tok} is outside the declared envelope [{lo}, {hi}]")
    return f"checked: every integer lies within the envelope [{lo}, {hi}] (per-variable bounds are not distinguished)", errors


def _check_text(kind: str, i: int, text: object, errors: list[str]) -> None:
    if not isinstance(text, str):
        errors.append(f"{kind} {i}: not a string")
        return
    if not text or not text.endswith("\n"):
        errors.append(f"{kind} {i}: must be non-empty and end with a newline")
    if "\r" in text or _CONTROL_RE.search(text):
        errors.append(f"{kind} {i}: contains carriage return or control characters")
    if len(text.encode("utf-8", "replace")) > MAX_CASE_BYTES:
        errors.append(f"{kind} {i}: larger than {MAX_CASE_BYTES} bytes")


def check_request(count: object, seed: object) -> tuple[int, int]:
    if isinstance(count, bool) or not isinstance(count, int) or not MIN_COUNT <= count <= MAX_COUNT:
        raise GenerationError(f"count must be an integer between {MIN_COUNT} and {MAX_COUNT}")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
        raise GenerationError(f"seed must be an integer between 0 and {MAX_SEED}")
    return count, seed


def gate(store: CodeProposalStore, sandbox: UntrustedSandbox, code_id: str) -> CodeProposal:
    """Only approved, non-stale, untampered proposals on a ready sandbox may run."""
    p = store.load(code_id)  # verifies content hash
    if not p.approved or not p.reviewer:
        raise GenerationError("code proposal is not approved by a human reviewer")
    if p.approved_content_sha256 != p.content_sha256:
        raise GenerationError("approved content hash does not match the current code (tampered)")
    if stale := store.staleness(p):
        raise GenerationError("code proposal is stale", stale)
    sandbox.require_ready()
    return p


def suites_dir(workspace: Path) -> Path:
    return Path(workspace).resolve() / SUITES_DIRNAME


def generate_tests(workspace: Path, code_id: str, count: int, seed: int, sandbox: UntrustedSandbox) -> dict:
    count, seed = check_request(count, seed)
    store = CodeProposalStore(workspace)
    p = gate(store, sandbox, code_id)
    spec = SpecStore(workspace).load(p.spec_id)
    errors: list[str] = []

    first = sandbox.run_generator(p.generator_code, seed, count)
    if not first.ok:
        raise GenerationError("generator failed in sandbox", [first.error])
    inputs = first.inputs
    second = sandbox.run_generator(p.generator_code, seed, count)
    deterministic_gen = second.ok and second.inputs == inputs
    if not deterministic_gen:
        errors.append("generator is not deterministic for the same seed and count")
    if len(inputs) != count:
        errors.append(f"generator returned {len(inputs)} inputs, expected {count}")
    for i, text in enumerate(inputs, 1):
        _check_text("input", i, text, errors)
    if len(set(inputs)) != len(inputs):
        errors.append(f"{len(inputs) - len(set(inputs))} duplicate input(s)")
    if errors:
        raise GenerationError("generated inputs rejected", errors)
    range_status, range_errors = check_ranges(spec.constraints, inputs)
    if range_errors:
        raise GenerationError("inputs violate declared constraints", range_errors[:20])

    samples = [s.input if s.input.endswith("\n") else s.input + "\n" for s in spec.samples]
    batch = inputs + samples
    ref = sandbox.run_reference(p.reference_code, batch)
    if not ref.compiled:
        raise GenerationError("reference solution failed to compile", [ref.error[:2000]])
    if not ref.complete or len(ref.results) != len(batch):
        bad = [f"input {i + 1}: {r['status']} (exit {r.get('exit_code')}) {str(r.get('stderr', ''))[:200]}".strip()
               for i, r in enumerate(ref.results) if r["status"] != "OK"]
        raise GenerationError("reference solution failed on some input", bad[:20] or [ref.error[:500]])
    outputs = [r["stdout"] for r in ref.results]
    for i, text in enumerate(outputs, 1):
        _check_text("output", i, text, errors)
    if errors:
        raise GenerationError("reference outputs rejected", errors[:20])

    again = sandbox.run_reference(p.reference_code, batch)
    if not (again.complete and [r["stdout"] for r in again.results] == outputs):
        raise GenerationError("reference solution is not deterministic", [])

    sample_results = []
    for s, got in zip(spec.samples, outputs[count:]):
        ok = outputs_match(s.output, got, CompareMode.TRIM)
        sample_results.append({"input": s.input, "expected": s.output, "actual": got, "match": ok})
    mismatches = [r for r in sample_results if not r["match"]]
    if mismatches:
        raise GenerationError(
            "reference output does not match the specification samples",
            [f"sample input {r['input']!r}: expected {r['expected']!r}, got {r['actual']!r}" for r in mismatches],
        )
    outputs = outputs[:count]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    suite_id = f"{spec.id[:30].rstrip('-')}-{stamp}-{secrets.token_hex(3)}"
    root = suites_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".tmp-{suite_id}"
    final = root / suite_id
    try:
        tmp.mkdir()
        tests = []
        for n, (text_in, text_out) in enumerate(zip(inputs, outputs), 1):
            write_text(safe_join(tmp, numbered_name(n, "in")), text_in)
            write_text(safe_join(tmp, numbered_name(n, "out")), text_out)
            tests.append({
                "id": n, "category": "ai-generated",
                "input_file": numbered_name(n, "in"), "output_file": numbered_name(n, "out"),
                "input_sha256": sha256(text_in), "output_sha256": sha256(text_out),
            })
        isolation = sandbox.status().get("isolation") or {}
        manifest = {
            "problem": spec.id, "generator_version": GENERATOR_VERSION, "seed": seed,
            "test_count": count, "test_ids": list(range(1, count + 1)), "comparator": "TRIM", "tests": tests,
            "provenance": {
                "suite_id": suite_id, "code_proposal_id": p.code_proposal_id, "content_sha256": p.content_sha256,
                "spec_sha256": p.spec_sha256, "plan_sha256": p.plan_sha256,
                "approved_by": p.reviewer, "approved_at": p.approved_at,
                "ai_provider": p.provider, "ai_model": p.model,
                "sandbox_image": sandbox.status().get("image"), "sandbox_isolation": isolation.get("level"),
            },
        }
        write_text(safe_join(tmp, "manifest.json"), _dump(manifest))
        report = {
            "suite_id": suite_id, "problem": spec.id, "test_count": count, "seed": seed,
            "checks": {
                "count_matches": True, "inputs_unique": True, "inputs_well_formed": True,
                "generator_deterministic": deterministic_gen, "reference_deterministic": True,
                "outputs_well_formed": True, "constraint_range_check": range_status,
                "samples_checked": len(sample_results), "samples_all_match": True,
            },
            "samples": sample_results,
            "independent_verification": "not performed",
            "limitations": [
                "No independent brute-force solution was run; matching the samples does not prove the reference is correct.",
                "Constraint checking is mechanical and best-effort; it cannot understand arbitrary natural-language constraints.",
                "AI code ran in a Docker container, which is not a strong isolation boundary (see sandbox status).",
            ],
        }
        write_text(safe_join(tmp, "quality-report.json"), _dump(report))
        write_zip(tmp, manifest)
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"suite_id": suite_id, "manifest": manifest, "report": report}


# ---- reading suites back (used by the web app) ----

def check_suite_id(suite_id: str) -> str:
    if not isinstance(suite_id, str) or not _SUITE_ID_RE.match(suite_id):
        raise GenerationError("invalid suite id")
    return suite_id


def suite_path(workspace: Path, suite_id: str) -> Path:
    path = suites_dir(workspace) / check_suite_id(suite_id)
    if not path.is_dir():
        raise GenerationError(f"suite {suite_id!r} not found")
    return path


def load_suite_info(workspace: Path, suite_id: str) -> dict:
    d = suite_path(workspace, suite_id)
    return {
        "suite_id": suite_id,
        "manifest": json.loads(read_text(d / "manifest.json")),
        "report": json.loads(read_text(d / "quality-report.json")),
    }


_FILE_RE = re.compile(r"^(\d{2,4}\.(in|out)|manifest\.json|quality-report\.json)$")


def read_suite_file(workspace: Path, suite_id: str, name: str, limit: int = 20_000) -> dict:
    if not _FILE_RE.match(name):
        raise GenerationError("invalid file name")
    text = read_text(safe_join(suite_path(workspace, suite_id), name))
    return {"name": name, "truncated": len(text) > limit, "size": len(text), "content": text[:limit]}


def zip_path(workspace: Path, suite_id: str) -> Path:
    p = safe_join(suite_path(workspace, suite_id), "tests.zip")
    if not p.is_file():
        raise GenerationError("tests.zip missing")
    return p
