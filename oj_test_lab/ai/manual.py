"""Create a new unapproved proposal from human-edited source text."""

from __future__ import annotations

import re

from ..specs.model import ProblemSpec, now_iso
from .code_model import CodeProposal
from .code_proposer import new_code_proposal_id
from .code_schema import MAX_CODE_CHARS
from .code_store import CodeProposalStoreError

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _source(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise CodeProposalStoreError(f"{name} must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise CodeProposalStoreError(f"{name} must not be empty")
    if len(normalized) > MAX_CODE_CHARS:
        raise CodeProposalStoreError(f"{name} is too long ({len(normalized)} > {MAX_CODE_CHARS} characters)")
    if _CONTROL.search(normalized):
        raise CodeProposalStoreError(f"{name} contains control characters")
    return normalized.strip("\n") + "\n"


def create_manual_revision(parent: CodeProposal, spec: ProblemSpec, generator_code: object, reference_code: object) -> CodeProposal:
    generator = _source("generator_code", generator_code)
    reference = _source("reference_code", reference_code)
    created_at = now_iso()
    return CodeProposal(
        code_proposal_id=new_code_proposal_id(spec.id, created_at), spec_id=spec.id,
        spec_schema_version=spec.schema_version, spec_sha256=parent.spec_sha256,
        plan_proposal_id=parent.plan_proposal_id, plan_sha256=parent.plan_sha256,
        provider="manual", model="human-edited", model_reported="human-edited",
        prompt_version="manual-v1", created_at=created_at,
        generator_code=generator, reference_code=reference,
        algorithm_explanation=parent.algorithm_explanation, complexity=parent.complexity,
        test_strategy=parent.test_strategy, numeric_range_analysis=parent.numeric_range_analysis,
        edge_cases=parent.edge_cases, adversarial_tests=parent.adversarial_tests,
        assumptions=parent.assumptions, risks=parent.risks,
        unresolved_questions=parent.unresolved_questions,
        parent_code_proposal_id=parent.code_proposal_id, repair_attempt=parent.repair_attempt,
    )