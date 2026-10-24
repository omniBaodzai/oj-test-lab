"""Suite building, quality report, export and loading."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from .evaluator import evaluate_candidate
from .models import GENERATOR_VERSION, Problem, TestCase
from .paths import (
    TEST_FILE_RE,
    clean_managed_files,
    numbered_name,
    prepare_output_dir,
    read_text,
    safe_join,
    write_text,
)

_EPOCH = (2020, 1, 1, 0, 0, 0)


class SuiteError(Exception):
    pass


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_suite(problem: Problem, count: int, seed: int) -> list[TestCase]:
    if count < problem.min_count:
        raise SuiteError(f"count must be >= {problem.min_count} for problem {problem.id}")
    try:
        raw = problem.generate(count, seed)
    except ValueError as exc:
        raise SuiteError(str(exc)) from exc
    cases = [TestCase(i, cat, text) for i, (cat, text) in enumerate(raw, start=1)]
    problems = check_cases(problem, cases)
    if problems["invalid"] or problems["duplicates"]:
        raise SuiteError(f"generator produced bad tests: {problems}")
    return cases


def check_cases(problem: Problem, cases: list[TestCase]) -> dict:
    invalid: dict[str, list[str]] = {}
    duplicates: list[list[int]] = []
    seen: dict[str, int] = {}
    for case in cases:
        errors = problem.validate(case.input_text)
        if errors:
            invalid[numbered_name(case.id, "in")] = errors
        if case.input_text in seen:
            duplicates.append([seen[case.input_text], case.id])
        else:
            seen[case.input_text] = case.id
    return {"invalid": invalid, "duplicates": duplicates}


def expected_outputs(problem: Problem, cases: list[TestCase]) -> dict[int, str]:
    return {c.id: problem.reference(c.input_text) for c in cases}


def build_quality_report(
    problem: Problem, cases: list[TestCase], seed: int | None, check: dict | None = None
) -> dict:
    check = check or check_cases(problem, cases)
    valid_cases = [c for c in cases if numbered_name(c.id, "in") not in check["invalid"]]
    expected = expected_outputs(problem, valid_cases)
    results = [
        evaluate_candidate(cand, valid_cases, expected, problem.compare_mode)
        for cand in problem.wrong_solutions
    ]
    escaped = [r.candidate.id for r in results if r.escaped]
    warnings = list(problem.limitations)
    if escaped:
        warnings.append(f"Incorrect solutions escaped detection: {', '.join(escaped)}")
    if check["invalid"]:
        warnings.append("Some tests failed input validation and must not be exported")
    return {
        "problem": problem.id,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "comparator": problem.compare_mode.value,
        "tests_generated": len(cases),
        "tests_valid": len(valid_cases),
        "tests_invalid": len(check["invalid"]),
        "invalid_tests": check["invalid"],
        "duplicate_tests": len(check["duplicates"]),
        "duplicate_pairs": check["duplicates"],
        "incorrect_solutions": [
            {
                "id": r.candidate.id,
                "kind": r.candidate.kind,
                "simulated": r.candidate.kind == "simulated-python",
                "description": r.candidate.description,
                "detected": not r.escaped,
                "detected_by_tests": r.detected_by,
                "errored_on_tests": r.errored_on,
            }
            for r in results
        ],
        "escaped_incorrect_solutions": escaped,
        "warnings": warnings,
    }


def build_manifest(problem: Problem, cases: list[TestCase], seed: int | None) -> dict:
    expected = expected_outputs(problem, cases)
    return {
        "problem": problem.id,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "test_count": len(cases),
        "test_ids": [c.id for c in cases],
        "comparator": problem.compare_mode.value,
        "tests": [
            {
                "id": c.id,
                "category": c.category,
                "input_file": numbered_name(c.id, "in"),
                "output_file": numbered_name(c.id, "out"),
                "input_sha256": sha256(c.input_text),
                "output_sha256": sha256(expected[c.id]),
            }
            for c in cases
        ],
    }


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_zip(out_dir: Path, manifest: dict) -> Path:
    """Deterministic ZIP: test files + manifest.json, flat structure, fixed timestamps."""
    names = [t[k] for t in manifest["tests"] for k in ("input_file", "output_file")]
    names.append("manifest.json")
    zip_path = safe_join(out_dir, "tests.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(names):
            info = zipfile.ZipInfo(name, date_time=_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, safe_join(out_dir, name).read_bytes())
    return zip_path


def export_suite(
    problem: Problem,
    cases: list[TestCase],
    seed: int | None,
    out_dir: Path,
    *,
    make_zip: bool = True,
    write_report: bool = True,
) -> dict:
    """Validate, then write NN.in/NN.out, manifest, report and optional zip."""
    check = check_cases(problem, cases)
    if check["invalid"] or check["duplicates"]:
        raise SuiteError(f"refusing to export, bad tests: {check}")
    out = prepare_output_dir(out_dir)
    expected = expected_outputs(problem, cases)
    clean_managed_files(out)
    for c in cases:
        write_text(safe_join(out, numbered_name(c.id, "in")), c.input_text)
        write_text(safe_join(out, numbered_name(c.id, "out")), expected[c.id])
    manifest = build_manifest(problem, cases, seed)
    write_text(safe_join(out, "manifest.json"), _dump(manifest))
    report = build_quality_report(problem, cases, seed, check)
    if write_report:
        write_text(safe_join(out, "quality-report.json"), _dump(report))
    if make_zip:
        write_zip(out, manifest)
    return report


def write_report(out_dir: Path, report: dict) -> Path:
    path = safe_join(out_dir.resolve(), "quality-report.json")
    write_text(path, _dump(report))
    return path


def load_suite(in_dir: Path) -> tuple[list[TestCase], int | None]:
    """Load NN.in files (and categories/seed from manifest.json when present)."""
    directory = in_dir.resolve()
    if not directory.is_dir():
        raise SuiteError(f"input directory does not exist: {directory}")
    manifest: dict = {}
    mpath = directory / "manifest.json"
    if mpath.is_file():
        try:
            manifest = json.loads(read_text(mpath))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SuiteError(f"manifest.json is not valid JSON: {exc}") from exc
    categories = {t.get("id"): t.get("category", "unknown") for t in manifest.get("tests", [])}
    cases = []
    for path in sorted(directory.iterdir()):
        m = TEST_FILE_RE.match(path.name)
        if path.is_file() and m and m.group(2) == "in":
            try:
                text = read_text(path)
            except UnicodeDecodeError as exc:
                raise SuiteError(f"{path.name} is not valid UTF-8") from exc
            tid = int(m.group(1))
            cases.append(TestCase(tid, categories.get(tid, "unknown"), text))
    if not cases:
        raise SuiteError(f"no NN.in files found in {directory}")
    cases.sort(key=lambda c: c.id)
    return cases, manifest.get("seed")


def verify_dir(problem: Problem, in_dir: Path, cases: list[TestCase]) -> list[str]:
    """Extra consistency checks for an existing directory: .out files and manifest checksums."""
    directory = in_dir.resolve()
    errors: list[str] = []
    expected = {c.id: problem.reference(c.input_text) for c in cases if not problem.validate(c.input_text)}
    for c in cases:
        out_path = directory / numbered_name(c.id, "out")
        if not out_path.is_file():
            errors.append(f"{out_path.name}: missing")
        elif c.id in expected and read_text(out_path) != expected[c.id]:
            errors.append(f"{out_path.name}: differs from reference output")
    mpath = directory / "manifest.json"
    if mpath.is_file():
        manifest = json.loads(read_text(mpath))
        by_id = {c.id: c for c in cases}
        for t in manifest.get("tests", []):
            case = by_id.get(t.get("id"))
            if case is None:
                errors.append(f"manifest lists test {t.get('id')} but its .in file is missing")
            elif t.get("input_sha256") != sha256(case.input_text):
                errors.append(f"{t.get('input_file')}: checksum does not match manifest")
        if manifest.get("test_count") != len(cases):
            errors.append("manifest test_count does not match number of .in files")
    return errors
