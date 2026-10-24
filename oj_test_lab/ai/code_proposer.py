"""CodeProposer: (spec, plan) -> validated, UNAPPROVED CodeProposal. Never executes the returned code."""

from __future__ import annotations

import re
import secrets

from ..specs.ai_interface import GenerationPlanProposal
from ..specs.model import ProblemSpec, now_iso
from .code_model import CodeProposal, Question, plan_fingerprint
from .code_prompts import CODE_PROMPT_VERSION, build_code_messages
from .code_schema import JSON_SCHEMA, SCHEMA_NAME, validate_code_response
from .errors import AIError
from .prompts import spec_fingerprint
from .provider import AIProvider
from .schema import parse_json_text

DEFAULT_CODE_MAX_TOKENS = 8000


def new_code_proposal_id(spec_id: str, created_at: str) -> str:
    stamp = re.sub(r"[^0-9]", "", created_at)[:14]
    return f"{spec_id[:36].rstrip('-')}-code-{stamp}-{secrets.token_hex(3)}"


class CodeProposer:
    def __init__(self, provider: AIProvider, *, max_output_tokens: int = DEFAULT_CODE_MAX_TOKENS) -> None:
        self._provider = provider
        self._max_tokens = max_output_tokens

    def propose(self, spec: ProblemSpec, plan: GenerationPlanProposal) -> CodeProposal:
        if plan.spec_id != spec.id:
            raise AIError("the plan proposal belongs to a different specification")
        system, user = build_code_messages(spec, plan)
        reply = self._provider.generate_json(
            system=system, user=user, schema_name=SCHEMA_NAME, schema=JSON_SCHEMA, max_output_tokens=self._max_tokens,
        )
        data = validate_code_response(parse_json_text(reply.text), spec.id)
        created_at = now_iso()
        return CodeProposal(
            code_proposal_id=new_code_proposal_id(spec.id, created_at),
            spec_id=spec.id,
            spec_schema_version=spec.schema_version,
            spec_sha256=spec_fingerprint(spec),
            plan_proposal_id=plan.proposal_id,
            plan_sha256=plan_fingerprint(plan),
            provider=self._provider.name,
            model=self._provider.model,
            model_reported=reply.model_reported,
            prompt_version=CODE_PROMPT_VERSION,
            created_at=created_at,
            generator_code=data["generator_code"],
            reference_code=data["reference_code"],
            algorithm_explanation=data["algorithm_explanation"],
            complexity=data["complexity"],
            test_strategy=data["test_strategy"],
            numeric_range_analysis=data["numeric_range_analysis"],
            edge_cases=tuple(data["edge_cases"]),
            adversarial_tests=tuple(data["adversarial_tests"]),
            assumptions=tuple(data["assumptions"]),
            risks=tuple(data["risks"]),
            unresolved_questions=tuple(Question(q["question"], q["blocking"]) for q in data["unresolved_questions"]),
        )
