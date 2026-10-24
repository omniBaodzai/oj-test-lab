"""Contract for the FUTURE AI analysis stage (no implementation, no AI calls here).

Flow: validated ProblemSpec --(SpecAnalyzer)--> GenerationPlanProposal.

A proposal is a separate artifact (workspace/proposals/), never merged into the
user's specification. Nothing proposed may be executed until a human reviews
and approves it; approval produces a different artifact (workspace/generators/).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .model import ProblemSpec


@dataclass(frozen=True)
class GenerationPlanProposal:
    """What an analyzer returns. Always unapproved when created.

    The first block of fields is the original contract. The rest were added with the
    OpenRouter analyzer (all defaulted, so older constructors keep working). Everything
    in the "AI analysis" block is untrusted, advisory TEXT: it is never executed.
    """

    spec_id: str
    spec_schema_version: int
    analyzer: str  # who produced it, e.g. "<provider>/<model>"; provenance is mandatory
    created_at: str
    summary: str
    test_categories: tuple[str, ...]
    generator_source: str | None = None  # proposed code: data only, never executed by this stage
    reference_source: str | None = None
    open_questions: tuple[str, ...] = ()  # questions for the human (clarification_questions)
    approved: bool = False

    # Identity / provenance (set by OJ Test Lab, never taken from the AI response)
    proposal_id: str = ""
    provider: str = ""
    model: str = ""  # model that was requested
    model_reported: str = ""  # model the provider says answered (may differ after routing)
    prompt_version: str = ""
    spec_sha256: str = ""
    proposal_schema_version: int = 1

    # AI analysis (structured, validated, untrusted)
    input_analysis: str = ""
    output_analysis: str = ""
    constraint_interpretation: str = ""
    suggested_algorithm: str = ""
    generator_strategy: str = ""
    reference_approach: str = ""
    edge_cases: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()
    risks_and_assumptions: tuple[str, ...] = ()


class SpecAnalyzer(Protocol):
    def analyze(self, spec: ProblemSpec) -> GenerationPlanProposal:
        """Read a validated spec and return a proposal. Must not modify the spec or run code."""
        ...
