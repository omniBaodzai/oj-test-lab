from dataclasses import replace

import pytest

from oj_test_lab.ai.code_model import CodeProposal, Question, plan_fingerprint
from oj_test_lab.ai.code_store import CodeProposalStore, CodeProposalStoreError
from oj_test_lab.ai.proposal_store import ProposalStore
from oj_test_lab.ai.prompts import spec_fingerprint
from oj_test_lab.question_resolution import (
    QuestionResolutionError,
    approval_questions_resolved,
    question_key,
    question_status,
    resolve_question,
)
from oj_test_lab.specs import SpecStore, parse_spec
from oj_test_lab.specs.model import now_iso


def make_workspace(tmp_path):
    spec = SpecStore(tmp_path).save(parse_spec({
        "id": "questions-problem", "title": "Questions", "statement": "A problem.",
        "input_description": "Input", "output_description": "Output", "constraints": ["x"],
        "samples": [], "desired_test_count": 3,
    }))
    from oj_test_lab.specs.ai_interface import GenerationPlanProposal
    plan = GenerationPlanProposal(
        spec_id=spec.id, spec_schema_version=1, analyzer="test", created_at=now_iso(),
        summary="summary", test_categories=("edge",), proposal_id="questions-plan",
        provider="test", model="test", model_reported="test", prompt_version="test",
        spec_sha256=spec_fingerprint(spec), input_analysis="input", output_analysis="output",
        constraint_interpretation="constraints", suggested_algorithm="algorithm",
        generator_strategy="generator", reference_approach="reference", edge_cases=("edge",),
        ambiguities=(), risks_and_assumptions=(), open_questions=(),
    )
    ProposalStore(tmp_path).save(plan)
    proposal = CodeProposal(
        code_proposal_id="questions-code", spec_id=spec.id, spec_schema_version=1,
        spec_sha256=spec_fingerprint(spec), plan_proposal_id=plan.proposal_id,
        plan_sha256=plan_fingerprint(plan), provider="test", model="test", model_reported="test",
        prompt_version="test", created_at=now_iso(),
        generator_code="def generate(seed, count):\n    return ['0\\n'] * count\n",
        reference_code="#include <iostream>\nint main(){return 0;}\n",
        algorithm_explanation="x", complexity="O(1)", test_strategy="x", numeric_range_analysis="x",
        edge_cases=("x",), adversarial_tests=("x",), assumptions=(), risks=(),
        unresolved_questions=(Question("What is the input behavior?", True), Question("Optional?", False)),
    )
    CodeProposalStore(tmp_path).save(proposal)
    return spec, proposal


def test_resolution_persists_reviewer_time_and_binding(tmp_path):
    spec, proposal = make_workspace(tmp_path)
    key = question_key("What is the input behavior?")
    before = question_status(tmp_path, proposal)
    assert before["all_blocking_resolved"] is False and before["questions"][0]["source"]["spec_id"] == spec.id
    after = resolve_question(tmp_path, proposal.code_proposal_id, key, "Reject malformed input.", "Reviewer")
    assert after["all_blocking_resolved"] is True
    resolved = after["questions"][0]["resolution"]
    assert resolved["reviewer"] == "Reviewer" and resolved["resolved_at"] and resolved["content_sha256"] == proposal.content_sha256
    assert approval_questions_resolved(tmp_path, CodeProposalStore(tmp_path).load(proposal.code_proposal_id))


def test_resolution_is_stale_after_specification_changes(tmp_path):
    spec, proposal = make_workspace(tmp_path)
    key = question_key("What is the input behavior?")
    resolve_question(tmp_path, proposal.code_proposal_id, key, "Answer", "Reviewer")
    changed = replace(spec, title="Changed")
    SpecStore(tmp_path).save(changed, overwrite=True)
    status = question_status(tmp_path, proposal)
    assert status["stale"] is True and status["all_blocking_resolved"] is False
    with pytest.raises(QuestionResolutionError, match="stale"):
        resolve_question(tmp_path, proposal.code_proposal_id, key, "New answer", "Reviewer")


def test_wrong_question_or_empty_answer_is_rejected(tmp_path):
    _, proposal = make_workspace(tmp_path)
    with pytest.raises(QuestionResolutionError, match="does not belong"):
        resolve_question(tmp_path, proposal.code_proposal_id, "0" * 64, "Answer", "Reviewer")
    with pytest.raises(QuestionResolutionError, match="must not be empty"):
        resolve_question(tmp_path, proposal.code_proposal_id, question_key("What is the input behavior?"), " ", "Reviewer")


def test_store_approval_still_requires_resolution_unless_explicitly_verified(tmp_path):
    _, proposal = make_workspace(tmp_path)
    store = CodeProposalStore(tmp_path)
    with pytest.raises(CodeProposalStoreError, match="unresolved blocking"):
        store.approve(proposal.code_proposal_id, "Reviewer")
    resolve_question(tmp_path, proposal.code_proposal_id, question_key("What is the input behavior?"), "Answer", "Reviewer")
    approved = store.approve(proposal.code_proposal_id, "Reviewer")
    assert approved.approved is True and approved.approved_content_sha256 == approved.content_sha256
