import json
import zipfile
from pathlib import Path

import pytest

from oj_test_lab.cli import main
from oj_test_lab.comparator import outputs_match
from oj_test_lab.models import CompareMode
from oj_test_lab.paths import UnsafePathError, safe_join
from oj_test_lab.problems import get_problem
from oj_test_lab.problems.data_types_io.generator import generate
from oj_test_lab.problems.data_types_io.reference import solve
from oj_test_lab.problems.data_types_io.validator import validate_input
from oj_test_lab.suite import SuiteError, build_quality_report, build_suite

PROBLEM = get_problem("data-types-io")
GOOD = "5\n123\na\n3.25\n2.5\n"


def run(*argv: str) -> int:
    return main(list(argv))


@pytest.fixture
def suite_dir(tmp_path: Path) -> Path:
    out = tmp_path / "suite"
    assert run("generate", "--problem", "data-types-io", "--seed", "42", "--output-dir", str(out)) == 0
    return out


# ---- generation ----
def test_deterministic_same_seed():
    assert generate(28, 7) == generate(28, 7)


def test_different_seed_changes_random_part():
    assert generate(28, 1) != generate(28, 2)


def test_default_count_and_unique_valid():
    cases = build_suite(PROBLEM, 28, 42)
    assert len(cases) == 28
    assert len({c.input_text for c in cases}) == 28
    assert all(validate_input(c.input_text) == [] for c in cases)


def test_larger_count_unique():
    cases = build_suite(PROBLEM, 200, 3)
    assert len({c.input_text for c in cases}) == 200


def test_count_below_mandatory_rejected():
    with pytest.raises(SuiteError):
        build_suite(PROBLEM, PROBLEM.min_count - 1, 1)


def test_mandatory_boundaries_present_for_any_seed():
    for seed in (0, 42, 999):
        ys = {int(t.split("\n")[1]) for _, t in generate(28, seed)}
        for y in (2147483647, 2147483648, -2147483648, -2147483649, 10**18, -(10**18)):
            assert y in ys
        assert any(abs(y) > 2**31 for y in ys)


def test_categories_covered():
    cats = {c for c, _ in generate(28, 42)}
    assert {"normal", "zero", "negative", "integer-boundary", "character", "float-format", "random"} <= cats


# ---- validation ----
@pytest.mark.parametrize(
    "text",
    [
        "5\n123\na\n3.25\n",  # too few
        GOOD + "1\n",  # extra line
        GOOD + "\n",  # blank extra line
        GOOD[:-1],  # no final LF
        GOOD.replace("\n", "\r\n"),
        " 5\n123\na\n3.25\n2.5\n",
        "5 \n123\na\n3.25\n2.5\n",
        "1000000001\n1\na\n1\n1\n",
        "1\n1000000000000000001\na\n1\n1\n",
        "1\n-1000000000000000001\na\n1\n1\n",
        "1.0\n1\na\n1\n1\n",
        "abc\n1\na\n1\n1\n",
        "01\n1\na\n1\n1\n",
        "-0\n1\na\n1\n1\n",
        "1\n1\nab\n1\n1\n",
        "1\n1\n1\n1\n1\n",
        "1\n1\né\n1\n1\n",
        "1\n1\na\nnan\n1\n",
        "1\n1\na\n1\ninf\n",
        "1\n1\na\n1e2\n1\n",
        "1\n1\na\n1000000.25\n1\n",
        "1\n1\na\n0.001\n1\n",  # too many digits
        "1\n1\na\n0.1\n1\n",  # not exactly representable in binary32
        "1\n1\na\n1\n0.1\n",  # not exactly representable in binary64
        "1\n1\na\n-0.00\n1\n",
        "1\n1\na\n1\n1000000000.5\n",
    ],
)
def test_validator_rejects(text):
    assert validate_input(text) != []


def test_validator_accepts_good():
    assert validate_input(GOOD) == []
    assert validate_input("-1000000000\n-1000000000000000000\nZ\n-1000000\n-1000000000\n") == []


# ---- reference ----
def test_reference_output():
    assert solve(GOOD) == "5\n123\na\n3.25\n2.500000000\n"


def test_reference_preserves_large_y_and_formats():
    out = solve("-5\n1000000000000000000\nQ\n0\n-0.001953125\n")
    assert out == "-5\n1000000000000000000\nQ\n0.00\n-0.001953125\n"


def test_reference_five_lines_for_all_tests():
    for _, t in generate(28, 42):
        out = solve(t)
        assert out.endswith("\n") and len(out[:-1].split("\n")) == 5
        assert out == solve(t)


def test_reference_rejects_invalid():
    with pytest.raises(ValueError):
        solve("1\n2\n")


# ---- comparator ----
def test_exact_vs_trim():
    assert not outputs_match("a\n", "a", CompareMode.EXACT)
    assert outputs_match("a\n", "a", CompareMode.TRIM)
    assert not outputs_match("a\n", "a \n", CompareMode.EXACT)
    assert outputs_match("a\n", "a \n\n", CompareMode.TRIM)
    assert outputs_match("a\r\nb\n", "a\nb\n", CompareMode.EXACT)


def test_internal_whitespace_never_collapsed():
    assert not outputs_match("1 2\n", "1  2\n", CompareMode.TRIM)
    assert not outputs_match("1 2\n", "1  2\n", CompareMode.EXACT)


def test_default_mode_is_exact():
    assert PROBLEM.compare_mode is CompareMode.EXACT


# ---- wrong solutions ----
def test_all_simulated_wrong_solutions_detected():
    cases = build_suite(PROBLEM, 28, 42)
    report = build_quality_report(PROBLEM, cases, 42)
    assert report["escaped_incorrect_solutions"] == []
    by_id = {s["id"]: s for s in report["incorrect_solutions"]}
    assert len(by_id) >= 4
    assert all(s["simulated"] and s["detected"] for s in by_id.values())
    y_big = {c.id for c in cases if not -(2**31) <= int(c.input_text.split("\n")[1]) < 2**31}
    assert set(by_id["wrong-y-int32-overflow"]["detected_by_tests"]) == y_big
    neg = by_id["wrong-negative-sign"]["detected_by_tests"]
    assert neg and all("-" in cases[i - 1].input_text for i in neg)


def test_escape_is_reported():
    tiny = [c for c in build_suite(PROBLEM, 28, 42) if c.input_text.startswith("0\n0\nA")]
    report = build_quality_report(PROBLEM, tiny, 42)
    assert "wrong-y-int32-overflow" in report["escaped_incorrect_solutions"]


# ---- export / CLI ----
def test_manifest_and_files(suite_dir):
    manifest = json.loads((suite_dir / "manifest.json").read_text("utf-8"))
    assert manifest["problem"] == "data-types-io"
    assert manifest["seed"] == 42 and manifest["test_count"] == 28
    assert manifest["test_ids"] == list(range(1, 29))
    assert manifest["comparator"] == "EXACT"
    for t in manifest["tests"]:
        assert (suite_dir / t["input_file"]).is_file() and (suite_dir / t["output_file"]).is_file()
        assert t["category"] and len(t["input_sha256"]) == 64
    assert (suite_dir / "01.in").read_bytes().count(b"\r") == 0
    report = json.loads((suite_dir / "quality-report.json").read_text("utf-8"))
    assert report["tests_generated"] == 28 and report["duplicate_tests"] == 0
    assert report["escaped_incorrect_solutions"] == []


def test_zip_structure(suite_dir):
    with zipfile.ZipFile(suite_dir / "tests.zip") as zf:
        names = zf.namelist()
        assert zf.testzip() is None
    assert len(names) == 28 * 2 + 1
    assert "01.in" in names and "28.out" in names and "manifest.json" in names
    assert all("/" not in n and ".." not in n for n in names)


def test_generated_output_is_reproducible(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        assert run("generate", "--problem", "data-types-io", "--seed", "5", "--output-dir", str(d)) == 0
    for name in ("01.in", "28.out", "manifest.json", "tests.zip"):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_validate_evaluate_export_cli(suite_dir, capsys):
    assert run("validate", "--problem", "data-types-io", "--input-dir", str(suite_dir)) == 0
    assert run("evaluate", "--problem", "data-types-io", "--input-dir", str(suite_dir), "--strict") == 0
    (suite_dir / "tests.zip").unlink()
    assert run("export", "--problem", "data-types-io", "--input-dir", str(suite_dir), "--zip") == 0
    assert (suite_dir / "tests.zip").is_file()
    out = capsys.readouterr().out
    assert "123456789012" not in out  # no hidden test contents on console


def test_validate_detects_tampering(suite_dir, capsys):
    (suite_dir / "03.in").write_bytes(b"1\n2\n")
    assert run("validate", "--problem", "data-types-io", "--input-dir", str(suite_dir)) == 1
    assert run("evaluate", "--problem", "data-types-io", "--input-dir", str(suite_dir)) == 1
    assert run("export", "--problem", "data-types-io", "--input-dir", str(suite_dir)) == 1


def test_validate_detects_wrong_out(suite_dir):
    (suite_dir / "02.out").write_bytes(b"wrong\n")
    assert run("validate", "--problem", "data-types-io", "--input-dir", str(suite_dir)) == 1


def test_cli_errors(tmp_path, capsys):
    assert run("validate", "--problem", "nope", "--input-dir", str(tmp_path)) == 1
    assert "Unknown problem" in capsys.readouterr().err
    assert run("validate", "--problem", "data-types-io", "--input-dir", str(tmp_path / "missing")) == 1
    assert run("validate", "--problem", "data-types-io", "--input-dir", str(tmp_path)) == 1
    assert run("generate", "--problem", "data-types-io", "--count", "3", "--output-dir", str(tmp_path / "o")) == 1
    assert not (tmp_path / "o").exists()
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_generate_output_dir_is_file(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    assert run("generate", "--problem", "data-types-io", "--output-dir", str(f)) == 1


# ---- safety ----
@pytest.mark.parametrize("name", ["../x", "a/b", "..", ".", "", "a\\b", "C:\\x"])
def test_safe_join_rejects(tmp_path, name):
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, name)


def test_safe_join_ok(tmp_path):
    assert safe_join(tmp_path, "01.in").parent == tmp_path.resolve()


def test_generate_only_touches_managed_files(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    keep = out / "notes.txt"
    keep.write_text("keep")
    stale = out / "99.in"
    stale.write_text("stale")
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("s")
    assert run("generate", "--problem", "data-types-io", "--output-dir", str(out)) == 0
    assert keep.read_text() == "keep" and sibling.read_text() == "s"
    assert not stale.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out", "sibling.txt"]
