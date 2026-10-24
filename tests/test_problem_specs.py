import json
from pathlib import Path

import pytest

from oj_test_lab.cli import main
from oj_test_lab.specs import (
    SpecExistsError,
    SpecNotFoundError,
    SpecStore,
    SpecStoreError,
    SpecValidationError,
    parse_spec,
)
from oj_test_lab.specs import store as store_mod
from oj_test_lab.specs.model import MAX_STATEMENT, SCHEMA_VERSION

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sum-two-integers.json"


def good() -> dict:
    return json.loads(EXAMPLE.read_text("utf-8"))


def errors_of(data) -> list[str]:
    with pytest.raises(SpecValidationError) as exc:
        parse_spec(data)
    return exc.value.errors


def run(*argv):
    return main(list(argv))


# ---- valid ----
def test_example_is_valid_and_vietnamese_preserved():
    spec = parse_spec(good())
    assert spec.id == "sum-two-integers" and spec.title == "Tổng hai số nguyên"
    assert spec.desired_test_count == 100 and spec.samples[0].input == "2 3"
    assert spec.constraints == ("-1000000000 <= a, b <= 1000000000",)


def test_optional_fields_default():
    d = good()
    for k in ("schema_version", "samples"):
        d.pop(k)
    spec = parse_spec(d)
    assert spec.schema_version == SCHEMA_VERSION and spec.samples == ()


# ---- invalid ----
@pytest.mark.parametrize("key", ["id", "title", "statement", "input_description", "output_description",
                                 "constraints", "desired_test_count"])
def test_missing_required(key):
    d = good()
    del d[key]
    assert any(e.startswith(f"{key}:") for e in errors_of(d))


@pytest.mark.parametrize("key,value", [
    ("title", 5), ("title", ""), ("title", "   "), ("title", "x" * 201), ("statement", "x" * (MAX_STATEMENT + 1)),
    ("statement", None), ("input_description", ["a"]), ("output_description", "a\x00b"),
    ("constraints", "a <= b"), ("constraints", []), ("constraints", [""]), ("constraints", [1]),
    ("constraints", ["x"] * 51), ("desired_test_count", 0), ("desired_test_count", 10001),
    ("desired_test_count", "100"), ("desired_test_count", 1.5), ("desired_test_count", True),
    ("samples", {}), ("samples", ["2 3"]), ("samples", [{"input": "1"}]),
    ("samples", [{"input": "1", "output": "2", "x": 3}]), ("samples", [{"input": "", "output": "2"}]),
    ("samples", [{"input": 1, "output": "2"}]), ("samples", [{"input": "1", "output": "2"}] * 21),
    ("schema_version", 2), ("schema_version", "1"), ("schema_version", True),
    ("created_at", "yesterday"), ("created_at", 5),
])
def test_invalid_values(key, value):
    d = good()
    d[key] = value
    assert errors_of(d)


@pytest.mark.parametrize("bad_id", ["", "Sum", "a b", "../x", "a/b", "a\\b", "-a", "a--b", "a-", "x" * 65,
                                    "con", "nul", "com1", 5, None, "tên"])
def test_invalid_ids(bad_id):
    d = good()
    d["id"] = bad_id
    assert any(e.startswith("id:") for e in errors_of(d))


def test_unknown_field_and_non_object():
    d = good()
    d["extra"] = 1
    assert "extra: unknown field" in errors_of(d)
    assert errors_of([1]) == ["top level must be a JSON object"]


def test_all_errors_reported_together():
    d = good()
    d["title"] = ""
    d["desired_test_count"] = 0
    assert len(errors_of(d)) == 2


# ---- store ----
def test_round_trip(tmp_path):
    store = SpecStore(tmp_path)
    saved = store.save(parse_spec(good()))
    loaded = store.load("sum-two-integers")
    assert loaded == saved and loaded.created_at and loaded.updated_at
    raw = store.path_for("sum-two-integers").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and "Tổng hai số nguyên".encode() in raw  # UTF-8, no escaping
    assert json.loads(raw)["schema_version"] == 1 and raw.endswith(b"\n")
    assert list(json.loads(raw)) == list(saved.to_dict())  # stable key order


def test_duplicate_id_requires_force(tmp_path):
    store = SpecStore(tmp_path)
    first = store.save(parse_spec(good()))
    changed = good()
    changed["title"] = "Khác"
    with pytest.raises(SpecExistsError):
        store.save(parse_spec(changed))
    assert store.load("sum-two-integers").title == "Tổng hai số nguyên"
    again = store.save(parse_spec(changed), overwrite=True)
    assert again.title == "Khác" and again.created_at == first.created_at


def test_path_traversal_blocked(tmp_path):
    store = SpecStore(tmp_path / "ws")
    for bad in ("../evil", "..\\evil", "a/b", "C:\\x", "", ".."):
        with pytest.raises(SpecStoreError):
            store.path_for(bad)
        with pytest.raises(SpecStoreError):
            store.load(bad)
    assert not (tmp_path / "evil.json").exists()


def test_load_missing_and_id_mismatch(tmp_path):
    store = SpecStore(tmp_path)
    with pytest.raises(SpecNotFoundError):
        store.load("nope")
    store.save(parse_spec(good()))
    p = store.path_for("sum-two-integers")
    p.rename(p.with_name("other.json"))
    with pytest.raises(SpecStoreError, match="does not match"):
        store.load("other")


def test_corrupt_file_reports_clearly(tmp_path):
    store = SpecStore(tmp_path)
    store.dir.mkdir(parents=True)
    (store.dir / "bad.json").write_text("{not json", "utf-8")
    with pytest.raises(SpecStoreError, match="invalid JSON"):
        store.load("bad")
    (store.dir / "bin.json").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(SpecStoreError, match="UTF-8"):
        store.load("bin")


def test_duplicate_json_keys_rejected(tmp_path):
    f = tmp_path / "d.json"
    f.write_text('{"id": "a", "id": "b"}', "utf-8")
    with pytest.raises(SpecStoreError, match="duplicate JSON key"):
        store_mod.read_json_file(f)


def test_atomic_write_failure_keeps_old_file_and_cleans_temp(tmp_path, monkeypatch):
    store = SpecStore(tmp_path)
    store.save(parse_spec(good()))
    target = store.path_for("sum-two-integers")
    before = target.read_bytes()
    changed = good()
    changed["title"] = "Mới"

    def boom(*a, **k):
        raise OSError("disk error")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        store.save(parse_spec(changed), overwrite=True)
    assert target.read_bytes() == before
    assert [p.name for p in store.dir.iterdir()] == ["sum-two-integers.json"]  # no temp leftovers


def test_no_partial_file_when_serialization_fails(tmp_path, monkeypatch):
    store = SpecStore(tmp_path)
    monkeypatch.setattr(store_mod.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        store.save(parse_spec(good()))
    assert list(store.dir.iterdir()) == []


def test_list_ids_ignores_junk(tmp_path):
    store = SpecStore(tmp_path)
    assert store.list_ids() == []
    store.save(parse_spec(good()))
    (store.dir / "README.txt").write_text("x")
    (store.dir / "Bad Name.json").write_text("{}")
    assert store.list_ids() == ["sum-two-integers"]


# ---- CLI ----
def test_cli_create_list_show_validate(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert run("problem", "--workspace", ws, "create", "--file", str(EXAMPLE)) == 0
    assert run("problem", "--workspace", ws, "list") == 0
    out = capsys.readouterr().out
    assert "sum-two-integers" in out and "100 tests" in out and "Tổng hai số nguyên" in out
    assert run("problem", "--workspace", ws, "show", "--id", "sum-two-integers") == 0
    assert "-1000000000 <= a, b <= 1000000000" in capsys.readouterr().out
    assert run("problem", "--workspace", ws, "validate", "--id", "sum-two-integers") == 0
    assert "STRUCTURE" in capsys.readouterr().out
    assert run("problem", "--workspace", ws, "validate", "--file", str(EXAMPLE)) == 0


def test_cli_duplicate_and_force(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert run("problem", "--workspace", ws, "create", "--file", str(EXAMPLE)) == 0
    assert run("problem", "--workspace", ws, "create", "--file", str(EXAMPLE)) == 1
    assert "already exists" in capsys.readouterr().err
    assert run("problem", "--workspace", ws, "create", "--file", str(EXAMPLE), "--force") == 0


def test_cli_invalid_spec_lists_all_errors(tmp_path, capsys):
    bad = good()
    bad["title"] = ""
    bad["desired_test_count"] = -1
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(bad), "utf-8")
    ws = tmp_path / "ws"
    assert run("problem", "--workspace", str(ws), "create", "--file", str(f)) == 1
    err = capsys.readouterr().err
    assert "title" in err and "desired_test_count" in err
    assert not (ws / "problems").exists() or not list((ws / "problems").iterdir())


def test_cli_errors(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert run("problem", "--workspace", ws, "create", "--file", str(tmp_path / "missing.json")) == 1
    assert run("problem", "--workspace", ws, "show", "--id", "nope") == 1
    assert run("problem", "--workspace", ws, "show", "--id", "../../etc") == 1
    assert run("problem", "--workspace", ws, "validate", "--id", "nope") == 1
    garbage = tmp_path / "g.json"
    garbage.write_text("[1,2", "utf-8")
    assert run("problem", "--workspace", ws, "create", "--file", str(garbage)) == 1
    assert "error:" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["problem"])
    assert exc.value.code == 2


def test_cli_rejects_builtin_id(tmp_path, capsys):
    d = good()
    d["id"] = "data-types-io"
    f = tmp_path / "b.json"
    f.write_text(json.dumps(d), "utf-8")
    assert run("problem", "--workspace", str(tmp_path / "ws"), "create", "--file", str(f)) == 1
    assert "built-in" in capsys.readouterr().err


def test_create_only_touches_workspace_problems(tmp_path):
    ws = tmp_path / "ws"
    other = tmp_path / "other.txt"
    other.write_text("keep")
    run("problem", "--workspace", str(ws), "create", "--file", str(EXAMPLE))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.txt", "ws"]
    assert [p.name for p in (ws).iterdir()] == ["problems"]
    assert other.read_text() == "keep"


# ---- backward compatibility ----
def test_builtin_registry_unchanged_and_old_commands_work(tmp_path):
    from oj_test_lab.problems import REGISTRY

    assert list(REGISTRY) == ["data-types-io"]
    out = tmp_path / "suite"
    assert run("generate", "--problem", "data-types-io", "--output-dir", str(out)) == 0
    assert run("validate", "--problem", "data-types-io", "--input-dir", str(out)) == 0
    assert run("evaluate", "--problem", "data-types-io", "--input-dir", str(out), "--strict") == 0
    assert run("export", "--problem", "data-types-io", "--input-dir", str(out), "--zip") == 0


def test_spec_is_not_the_engine_problem():
    from oj_test_lab.models import Problem
    from oj_test_lab.specs import ProblemSpec

    assert ProblemSpec is not Problem
    assert not hasattr(ProblemSpec, "generate") and not hasattr(ProblemSpec, "reference")


def test_ai_interface_is_contract_only():
    from oj_test_lab.specs import ai_interface

    assert hasattr(ai_interface, "SpecAnalyzer")
    proposal = ai_interface.GenerationPlanProposal("a", 1, "x/y", "t", "s", ("c",))
    assert proposal.approved is False
    assert not [n for n, v in vars(ai_interface).items() if isinstance(v, type) and n.endswith("Analyzer") and n != "SpecAnalyzer"]
