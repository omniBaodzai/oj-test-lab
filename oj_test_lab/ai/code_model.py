"""CodeProposal: UNTRUSTED AI-written code stored for human review.

This module is data only. Nothing here (or anywhere in the code-proposal workflow) compiles,
imports, evaluates, or runs the code strings it holds.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..specs.ai_interface import GenerationPlanProposal

CODE_PROPOSAL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Question:
    question: str
    blocking: bool  # a blocking question must be resolved before a human may approve


@dataclass(frozen=True)
class CodeProposal:
    code_proposal_id: str
    spec_id: str
    spec_schema_version: int
    spec_sha256: str
    plan_proposal_id: str
    plan_sha256: str
    provider: str
    model: str
    model_reported: str
    prompt_version: str
    created_at: str
    generator_code: str  # Python, contract: generate(seed, count) -> list[str]
    reference_code: str  # C++17, stdin -> stdout
    algorithm_explanation: str
    complexity: str
    test_strategy: str
    numeric_range_analysis: str
    edge_cases: tuple[str, ...]
    adversarial_tests: tuple[str, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    unresolved_questions: tuple[Question, ...]
    schema_version: int = CODE_PROPOSAL_SCHEMA_VERSION
    approved: bool = False
    reviewer: str | None = None
    approved_at: str | None = None
    approved_content_sha256: str | None = None
    parent_code_proposal_id: str | None = None
    repair_attempt: int = 0

    def content_dict(self) -> dict:
        return {
            "generator_language": "python",
            "generator_code": self.generator_code,
            "reference_language": "cpp17",
            "reference_code": self.reference_code,
            "algorithm_explanation": self.algorithm_explanation,
            "complexity": self.complexity,
            "test_strategy": self.test_strategy,
            "numeric_range_analysis": self.numeric_range_analysis,
            "edge_cases": list(self.edge_cases),
            "adversarial_tests": list(self.adversarial_tests),
            "assumptions": list(self.assumptions),
            "risks": list(self.risks),
            "unresolved_questions": [{"question": q.question, "blocking": q.blocking} for q in self.unresolved_questions],
        }

    @property
    def content_sha256(self) -> str:
        return sha256_json(self.content_dict())

    @property
    def blocking_questions(self) -> tuple[Question, ...]:
        return tuple(q for q in self.unresolved_questions if q.blocking)


def sha256_json(obj: object) -> str:
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_fingerprint(plan: GenerationPlanProposal) -> str:
    """Hash of the stored plan content. `approved` is excluded so a future plan approval is not 'staleness'."""
    from .proposal_store import to_document

    doc = to_document(plan)
    doc.pop("approved", None)
    return sha256_json(doc)
