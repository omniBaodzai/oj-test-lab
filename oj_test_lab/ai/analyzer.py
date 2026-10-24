"""ProblemAnalyzer: spec -> validated, UNAPPROVED GenerationPlanProposal. Never executes anything."""

from __future__ import annotations

import re
import secrets

from ..specs.ai_interface import GenerationPlanProposal
from ..specs.model import ProblemSpec, now_iso
from .prompts import PROMPT_VERSION, build_messages, spec_fingerprint
from .provider import AIProvider
from .schema import JSON_SCHEMA, SCHEMA_NAME, parse_json_text, validate_analysis

PROPOSAL_SCHEMA_VERSION = 1


def new_proposal_id(spec_id: str, created_at: str) -> str:
    """`<spec id prefix>-<UTC timestamp>-<random>`: unique, lowercase, filename-safe, <= 64 chars."""
    stamp = re.sub(r"[^0-9]", "", created_at)[:14]
    return f"{spec_id[:40].rstrip('-')}-{stamp}-{secrets.token_hex(3)}"


class ProblemAnalyzer:
    """Implements the SpecAnalyzer protocol on top of any AIProvider."""

    def __init__(self, provider: AIProvider, *, max_output_tokens: int = 4000) -> None:
        self._provider = provider
        self._max_tokens = max_output_tokens

    def analyze(self, spec: ProblemSpec) -> GenerationPlanProposal:
        system, user = build_messages(spec)
        reply = self._provider.generate_json(
            system=system,
            user=user,
            schema_name=SCHEMA_NAME,
            schema=JSON_SCHEMA,
            max_output_tokens=self._max_tokens,
        )
        analysis = validate_analysis(parse_json_text(reply.text), spec.id)
        created_at = now_iso()
        return GenerationPlanProposal(
            spec_id=spec.id,
            spec_schema_version=spec.schema_version,
            analyzer=f"{self._provider.name}/{self._provider.model}",
            created_at=created_at,
            summary=analysis["summary"],
            test_categories=tuple(analysis["test_categories"]),
            open_questions=tuple(analysis["clarification_questions"]),
            approved=False,  # always: only a human may approve, in a later phase
            proposal_id=new_proposal_id(spec.id, created_at),
            provider=self._provider.name,
            model=self._provider.model,
            model_reported=reply.model_reported,
            prompt_version=PROMPT_VERSION,
            spec_sha256=spec_fingerprint(spec),
            proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
            input_analysis=analysis["input_analysis"],
            output_analysis=analysis["output_analysis"],
            constraint_interpretation=analysis["constraint_interpretation"],
            suggested_algorithm=analysis["suggested_algorithm"],
            generator_strategy=analysis["generator_strategy"],
            reference_approach=analysis["reference_approach"],
            edge_cases=tuple(analysis["edge_cases"]),
            ambiguities=tuple(analysis["ambiguities"]),
            risks_and_assumptions=tuple(analysis["risks_and_assumptions"]),
        )
