import json
from dataclasses import replace

import pytest

from oj_test_lab.ai.code_model import CodeProposal, Question
from oj_test_lab.ai.code_store import CodeProposalStoreError
from oj_test_lab.ai.provider import ProviderReply
from oj_test_lab.ai.repair import RepairUnchangedError, repair_code
from oj_test_lab.code_review import MAX_REPAIR_ATTEMPTS, ValidationReport, validate_code
from oj_test_lab.ai.errors import AIResponseError
from oj_test_lab.sandbox import GeneratorOutcome, ReferenceOutcome, UntrustedSandbox
from oj_test_lab.specs import parse_spec


SPEC = parse_spec({
    "id": "review-problem", "title": "Review", "statement": "Add two numbers.",
    "input_description": "a b", "output_description": "sum", "constraints": ["-100 <= a, b <= 100"],
    "samples": [{"input": "2 3", "output": "5\n"}], "desired_test_count": 20,
})
GEN = "def generate(seed, count):\n    return [f'{i} 0\\n' for i in range(count)]\n"
CPP = '#include <iostream>\nint main(){std::cout << "5\\n";}\n'


def proposal(generator=GEN, reference=CPP, attempt=0):
    return CodeProposal(
        code_proposal_id=f"review-code-{attempt}", spec_id=SPEC.id, spec_schema_version=1,
        spec_sha256="a" * 64, plan_proposal_id="review-plan", plan_sha256="b" * 64,
        provider="fake", model="fake", model_reported="fake", prompt_version="test", created_at="now",
        generator_code=generator, reference_code=reference, algorithm_explanation="x", complexity="O(1)",
        test_strategy="x", numeric_range_analysis="x", edge_cases=("x",), adversarial_tests=("x",),
        assumptions=(), risks=(), unresolved_questions=(Question("q", False),), repair_attempt=attempt,
    )


class FakeSandbox:
    def __init__(self, mode="good"):
        self.mode, self.generator_calls = mode, 0

    def require_ready(self):
        return {"ready": True, "isolation": {"level": "fake"}}

    def status(self):
        return {"ready": True, "isolation": {"level": "fake"}}

    def run_generator(self, source, seed, count):
        self.generator_calls += 1
        if self.mode == "badcount":
            return GeneratorOutcome(True, ["0 0\n"] * max(0, count - 1))
        if self.mode == "nondeterministic" and self.generator_calls % 2 == 0:
            return GeneratorOutcome(True, [f"{i + 1} 0\n" for i in range(count)])
        return GeneratorOutcome(True, [f"{i} 0\n" for i in range(count)])

    def run_reference(self, source, inputs):
        output = "6\n" if self.mode == "sample-mismatch" else "5\n"
        return ReferenceOutcome(True, results=[{"status": "OK", "stdout": output} for _ in inputs], complete=True)


def test_static_syntax_and_unordered_iteration_are_reported():
    source = "def generate(seed, count):\n    return [x for x in {1, 2}]\n"
    report = validate_code(proposal(source), SPEC, UntrustedSandbox(False))
    assert not report.passed
    assert any("unordered" in item for item in report.errors)
    assert any("dynamic checks skipped" in item for item in report.warnings)


@pytest.mark.parametrize("mode,needle", [("badcount", "returned"), ("nondeterministic", "separate sandbox processes"), ("sample-mismatch", "sample input")])
def test_dynamic_validation_reports_contract_failures(mode, needle):
    report = validate_code(proposal(), SPEC, FakeSandbox(mode))
    assert not report.passed
    assert any(needle in item for item in report.errors)


def test_good_dynamic_validation_checks_small_count_and_multiple_seeds():
    report = validate_code(proposal(), SPEC, FakeSandbox())
    assert report.passed
    assert any("count_1" in check["name"] for check in report.checks)
    assert any("count_20" in check["name"] for check in report.checks)
    assert any("seed_42" in check["name"] for check in report.checks)


class RepairProvider:
    name = "fake-repair"
    model = "fake-repair-1"

    def generate_json(self, **kwargs):
        return ProviderReply(json.dumps({
            "problem_id": SPEC.id, "algorithm_explanation": "fixed", "complexity": "O(1)",
            "test_strategy": "fixed", "numeric_range_analysis": "small", "generator_code": "# repaired\n" + GEN,
            "reference_code": CPP + "// repaired\n", "edge_cases": ["zero"], "adversarial_tests": ["bad count"],
            "assumptions": [], "risks": [], "unresolved_questions": [],
        }), self.model)


def test_repair_creates_new_unapproved_lineage_and_limits_attempts():
    parent = proposal()
    report = ValidationReport(parent.code_proposal_id, parent.content_sha256, SPEC.id, parent.spec_sha256,
                               errors=["generator.contract: wrong count"])
    repaired = repair_code(RepairProvider(), SPEC, parent, report)
    assert repaired.code_proposal_id != parent.code_proposal_id
    assert repaired.parent_code_proposal_id == parent.code_proposal_id
    assert repaired.repair_attempt == 1 and not repaired.approved
    with pytest.raises(CodeProposalStoreError, match="limit"):
        repair_code(RepairProvider(), SPEC, replace(parent, repair_attempt=MAX_REPAIR_ATTEMPTS), report)
    with pytest.raises(CodeProposalStoreError, match="approved"):
        repair_code(RepairProvider(), SPEC, replace(parent, approved=True), report)


def test_repair_schema_failure_is_reported_without_creating_a_proposal():
    class BrokenProvider(RepairProvider):
        def generate_json(self, **kwargs):
            return ProviderReply("{\"not_code\": true}", self.model)

    parent = proposal()
    report = ValidationReport(parent.code_proposal_id, parent.content_sha256, SPEC.id, parent.spec_sha256,
                               errors=["reference.cpp17.compile: failed"])
    with pytest.raises(AIResponseError):
        repair_code(BrokenProvider(), SPEC, parent, report)


def test_unchanged_repair_is_reported_and_not_mistaken_for_success():
    class SameProvider(RepairProvider):
        def generate_json(self, **kwargs):
            return ProviderReply(json.dumps({
                "problem_id": SPEC.id, "algorithm_explanation": "same", "complexity": "O(1)",
                "test_strategy": "same", "numeric_range_analysis": "same", "generator_code": GEN,
                "reference_code": CPP, "edge_cases": ["zero"], "adversarial_tests": ["count"],
                "assumptions": [], "risks": [], "unresolved_questions": [],
            }), self.model)

    parent = proposal()
    report = ValidationReport(parent.code_proposal_id, parent.content_sha256, SPEC.id, parent.spec_sha256,
                               errors=["generator.contract: returned 6 inputs for count=1"])
    with pytest.raises(RepairUnchangedError, match="did not change") as exc:
        repair_code(SameProvider(), SPEC, parent, report)
    assert "returned 6 inputs" in str(exc.value)


def test_each_repair_proposal_persists_its_own_source(tmp_path):
    from oj_test_lab.ai.code_store import CodeProposalStore

    store = CodeProposalStore(tmp_path)
    original = proposal()
    repaired = replace(original, code_proposal_id="review-code-repaired", generator_code="# repaired\n" + GEN,
                       parent_code_proposal_id=original.code_proposal_id, repair_attempt=1)
    store.save(original)
    store.save(repaired)
    assert store.load(original.code_proposal_id).generator_code == GEN
    assert store.load(repaired.code_proposal_id).generator_code.startswith("# repaired")
    assert store.load(repaired.code_proposal_id).parent_code_proposal_id == original.code_proposal_id


def test_validation_report_binds_to_the_active_proposal_content():
    original = proposal()
    repaired = replace(original, code_proposal_id="review-code-repaired", generator_code="# repaired\n" + GEN,
                       parent_code_proposal_id=original.code_proposal_id, repair_attempt=1)
    report = validate_code(repaired, SPEC, FakeSandbox("badcount"))
    assert report.proposal_id == repaired.code_proposal_id
    assert report.content_sha256 == repaired.content_sha256
    assert report.proposal_id != original.code_proposal_id
