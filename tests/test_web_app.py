import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oj_test_lab.ai.code_model import CodeProposal, Question
from oj_test_lab.ai.code_store import CodeProposalStore
from oj_test_lab.ai.code_schema import MAX_CODE_CHARS
from oj_test_lab.ai.prompts import spec_fingerprint
from oj_test_lab.ai.code_model import plan_fingerprint
from oj_test_lab.ai.proposal_store import ProposalStore
from oj_test_lab.specs.ai_interface import GenerationPlanProposal
from oj_test_lab.specs.model import now_iso
from oj_test_lab.code_review import ValidationReport, save_report
from oj_test_lab.sandbox import UntrustedSandbox
from oj_test_lab.specs import SpecStore, parse_spec
from oj_test_lab.web.app import create_app


TOKEN = {"X-OJ-Test-Lab": "local-ui"}


def problem_payload() -> dict:
    return {
        "id": "web-problem",
        "title": "Web problem",
        "statement": "Add two values.",
        "input_description": "Two integers.",
        "output_description": "Their sum.",
        "constraints": ["-10 <= a, b <= 10"],
        "samples": [{"input": "2 3", "output": "5"}],
        "desired_test_count": 3,
    }


@pytest.fixture
def client(tmp_path: Path):
    return TestClient(create_app(tmp_path))


def test_home_and_health_are_available(client):
    home = client.get("/")
    assert home.status_code == 200 and "OJ Test Lab" in home.text
    health = client.get("/api/health")
    assert health.status_code == 200 and health.json()["sandbox"]["ready"] is False


def test_mutating_api_requires_local_request_token(client):
    response = client.post("/api/problems", json=problem_payload())
    assert response.status_code == 403
    response = client.post("/api/problems", headers=TOKEN, json=problem_payload())
    assert response.status_code == 200
    assert response.json()["problem"]["id"] == "web-problem"


def test_invalid_problem_is_reported_without_writing(client, tmp_path):
    bad = problem_payload()
    bad["constraints"] = []
    response = client.post("/api/problems", headers=TOKEN, json=bad)
    assert response.status_code == 400
    assert not (tmp_path / "problems" / "web-problem.json").exists()


def test_generation_fails_closed_when_sandbox_is_disabled(tmp_path):
    client = TestClient(create_app(tmp_path, sandbox=UntrustedSandbox(enabled=False)))
    response = client.post("/api/generate", headers=TOKEN, json={"code_id": "missing", "count": 1, "seed": 1})
    assert response.status_code == 400
    assert "not approved" in response.json()["detail"] or "not found" in response.json()["detail"]


def _saved_proposal(workspace: Path, proposal_id: str, parent: str | None = None, blocking: bool = False) -> CodeProposal:
    spec = SpecStore(workspace).load("web-problem")
    plan_store = ProposalStore(workspace)
    if not plan_store.list_ids():
        plan_store.save(GenerationPlanProposal(
            spec_id=spec.id, spec_schema_version=1, analyzer="test", created_at=now_iso(),
            summary="summary", test_categories=("edge",), proposal_id="web-plan", provider="test",
            model="test", model_reported="test", prompt_version="test", spec_sha256=spec_fingerprint(spec),
            input_analysis="input", output_analysis="output", constraint_interpretation="constraints",
            suggested_algorithm="algorithm", generator_strategy="generator", reference_approach="reference",
            edge_cases=("edge",), ambiguities=(), risks_and_assumptions=(), open_questions=(),
        ))
    plan = plan_store.load("web-plan")
    proposal = CodeProposal(
        code_proposal_id=proposal_id, spec_id=spec.id, spec_schema_version=1,
        spec_sha256=spec_fingerprint(spec), plan_proposal_id=plan.proposal_id, plan_sha256=plan_fingerprint(plan),
        provider="fake", model="fake", model_reported="fake", prompt_version="test", created_at="now",
        generator_code=f"# {proposal_id}\n" + "def generate(seed, count):\n    return [f'{i} 0\\n' for i in range(count)]\n",
        reference_code="#include <iostream>\nint main(){std::cout << \"5\\n\";}\n",
        algorithm_explanation="x", complexity="O(1)", test_strategy="x", numeric_range_analysis="x",
        edge_cases=("x",), adversarial_tests=("x",), assumptions=(), risks=(),
        unresolved_questions=(Question("q", blocking),), parent_code_proposal_id=parent,
        repair_attempt=1 if parent else 0,
    )
    CodeProposalStore(workspace).save(proposal)
    return proposal


def test_saved_problem_and_proposals_can_resume_without_ai(tmp_path, monkeypatch):
    client = TestClient(create_app(tmp_path))
    assert client.post("/api/problems", headers=TOKEN, json=problem_payload()).status_code == 200
    first = _saved_proposal(tmp_path, "web-code-original")
    second = _saved_proposal(tmp_path, "web-code-repair", first.code_proposal_id)
    save_report(tmp_path, ValidationReport(second.code_proposal_id, second.content_sha256, second.spec_id, second.spec_sha256,
                                           passed=False, errors=["generator.contract: wrong count"]))
    def no_provider(*args, **kwargs):
        raise AssertionError("loading saved data must not call OpenRouter")
    monkeypatch.setattr("oj_test_lab.web.app.build_provider", no_provider)

    loaded = client.get("/api/problems/web-problem")
    assert loaded.status_code == 200 and loaded.json()["problem"]["id"] == "web-problem"
    listing = client.get("/api/problems/web-problem/code-proposals")
    assert listing.status_code == 200
    items = {item["code_proposal_id"]: item for item in listing.json()["proposals"]}
    assert set(items) == {first.code_proposal_id, second.code_proposal_id}
    assert items[second.code_proposal_id]["parent_code_proposal_id"] == first.code_proposal_id
    assert items[second.code_proposal_id]["validation"]["status"] == "failed"
    selected = client.get(f"/api/code/{second.code_proposal_id}")
    assert selected.status_code == 200
    assert selected.json()["code"]["generator_code"] == second.generator_code
    assert selected.json()["validation"]["status"] == "failed"


def test_existing_problem_is_not_overwritten_by_resume(client):
    assert client.post("/api/problems", headers=TOKEN, json=problem_payload()).status_code == 200
    changed = problem_payload()
    changed["title"] = "Changed"
    response = client.post("/api/problems", headers=TOKEN, json=changed)
    assert response.status_code == 400
    assert client.get("/api/problems/web-problem").json()["problem"]["title"] == "Web problem"


def test_home_contains_resume_controls(client):
    html = client.get("/").text
    assert "Load saved problem" in html and "Saved code proposals" in html
    assert 'id="active-code"' in html and 'id="proposal-meta"' in html


def test_manual_edit_saves_new_lineage_without_openrouter(tmp_path, monkeypatch):
    client = TestClient(create_app(tmp_path))
    assert client.post("/api/problems", headers=TOKEN, json=problem_payload()).status_code == 200
    original = _saved_proposal(tmp_path, "web-code-original")

    def no_provider(*args, **kwargs):
        raise AssertionError("manual editing must not call OpenRouter")
    monkeypatch.setattr("oj_test_lab.web.app.build_provider", no_provider)
    edited_generator = "def generate(seed, count):\n    return ['0 0\\n'] * count\n"
    edited_reference = "# edited\n#include <iostream>\nint main(){std::cout << \"5\\n\";}\n"
    response = client.post(f"/api/code/{original.code_proposal_id}/manual-edit", headers=TOKEN,
                           json={"generator_code": edited_generator, "reference_code": edited_reference})
    assert response.status_code == 200
    new_code = response.json()["code"]
    assert new_code["code_proposal_id"] != original.code_proposal_id
    assert new_code["parent_code_proposal_id"] == original.code_proposal_id
    assert new_code["approved"] is False and new_code["provider"] == "manual"
    assert new_code["generator_code"] == edited_generator
    assert new_code["reference_code"] == edited_reference
    assert CodeProposalStore(tmp_path).load(original.code_proposal_id).generator_code == original.generator_code
    assert CodeProposalStore(tmp_path).load(new_code["code_proposal_id"]).reference_code == edited_reference


@pytest.mark.parametrize("field,value", [("generator_code", ""), ("reference_code", ""),
                                          ("generator_code", "x" * (MAX_CODE_CHARS + 1))])
def test_manual_edit_validates_required_source_before_saving(client, tmp_path, field, value):
    assert client.post("/api/problems", headers=TOKEN, json=problem_payload()).status_code == 200
    original = _saved_proposal(tmp_path, "web-code-original")
    body = {"generator_code": original.generator_code, "reference_code": original.reference_code}
    body[field] = value
    response = client.post(f"/api/code/{original.code_proposal_id}/manual-edit", headers=TOKEN, json=body)
    assert response.status_code == 400
    assert CodeProposalStore(tmp_path).list_ids() == [original.code_proposal_id]


def test_manual_revision_cannot_be_approved_before_validation(client, tmp_path):
    assert client.post("/api/problems", headers=TOKEN, json=problem_payload()).status_code == 200
    original = _saved_proposal(tmp_path, "web-code-original")
    response = client.post(f"/api/code/{original.code_proposal_id}/manual-edit", headers=TOKEN,
                           json={"generator_code": original.generator_code, "reference_code": original.reference_code})
    new_id = response.json()["code"]["code_proposal_id"]
    approval = client.post(f"/api/code/{new_id}/approve", headers=TOKEN,
                           json={"reviewer": "Reviewer", "confirm_id": new_id})
    assert approval.status_code == 400
    assert CodeProposalStore(tmp_path).load(new_id).approved is False


def test_home_contains_manual_edit_controls(client):
    html = client.get("/").text
    assert 'id="edit"' in html and 'id="save-edit"' in html and 'id="cancel-edit"' in html


def test_question_resolution_api_persists_answer_for_active_proposal(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.post("/api/problems", headers=TOKEN, json=problem_payload()).status_code == 200
    proposal = _saved_proposal(tmp_path, "web-code-questions", blocking=True)
    questions = client.get(f"/api/code/{proposal.code_proposal_id}/questions")
    assert questions.status_code == 200
    item = questions.json()["questions"][0]
    assert item["blocking"] is True and item["resolved"] is False
    resolved = client.post(f"/api/code/{proposal.code_proposal_id}/questions/resolve", headers=TOKEN,
                           json={"question_sha256": item["question_sha256"], "answer": "Use strict input.", "reviewer": "Alice"})
    assert resolved.status_code == 200
    assert resolved.json()["all_blocking_resolved"] is True
    assert resolved.json()["questions"][0]["resolution"]["reviewer"] == "Alice"


def test_home_contains_open_questions_controls(client):
    html = client.get("/").text
    assert "Open Questions" in html and 'id="questions"' in html and "Resolve question" in client.get("/static/app.js").text


def test_web_assets_are_utf8_and_use_vietnamese_capable_fonts(client):
    static = Path(__file__).resolve().parent.parent / "oj_test_lab" / "web" / "static"
    for name in ("index.html", "style.css", "app.js"):
        raw = (static / name).read_bytes()
        text = raw.decode("utf-8")
        assert text.encode("utf-8") == raw
    html = client.get("/").text
    css = client.get("/static/style.css").text
    assert 'charset="utf-8"' in html.lower()
    assert '"Segoe UI"' in css and '"Noto Sans"' in css
    assert "var(--ui-font)" in css