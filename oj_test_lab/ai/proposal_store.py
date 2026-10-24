"""Proposal storage: workspace/proposals/<proposal_id>.json (separate from workspace/problems/).

Proposals are never overwritten and the source specification is never modified.
Stored layout (proposal_schema_version = 1):

    {
      "proposal_schema_version": 1,
      "proposal_id": "...",
      "approved": false,
      "source": {"spec_id", "spec_schema_version", "spec_sha256"},
      "provenance": {"provider", "model", "model_reported", "analyzer", "prompt_version", "generated_at"},
      "analysis": {problem_id, summary, input_analysis, ..., risks_and_assumptions}
    }
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..fsutil import write_new_atomic
from ..paths import safe_join
from ..specs.ai_interface import GenerationPlanProposal
from ..specs.model import check_spec_id
from ..specs.store import SpecStoreError, read_json_file
from .errors import AIResponseError
from .schema import FIELD_ORDER, validate_analysis

PROPOSALS_DIRNAME = "proposals"
SUPPORTED_PROPOSAL_VERSIONS = (1,)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ProposalStoreError(Exception):
    pass


class ProposalNotFoundError(ProposalStoreError):
    pass


def to_document(p: GenerationPlanProposal) -> dict:
    return {
        "proposal_schema_version": p.proposal_schema_version,
        "proposal_id": p.proposal_id,
        "approved": p.approved,
        "source": {"spec_id": p.spec_id, "spec_schema_version": p.spec_schema_version, "spec_sha256": p.spec_sha256},
        "provenance": {
            "provider": p.provider, "model": p.model, "model_reported": p.model_reported,
            "analyzer": p.analyzer, "prompt_version": p.prompt_version, "generated_at": p.created_at,
        },
        "analysis": {
            "problem_id": p.spec_id,
            "summary": p.summary,
            "input_analysis": p.input_analysis,
            "output_analysis": p.output_analysis,
            "constraint_interpretation": p.constraint_interpretation,
            "suggested_algorithm": p.suggested_algorithm,
            "generator_strategy": p.generator_strategy,
            "reference_approach": p.reference_approach,
            "test_categories": list(p.test_categories),
            "edge_cases": list(p.edge_cases),
            "ambiguities": list(p.ambiguities),
            "clarification_questions": list(p.open_questions),
            "risks_and_assumptions": list(p.risks_and_assumptions),
        },
    }


def _need(obj: object, key: str, typ: type, where: str):
    if not isinstance(obj, dict) or not isinstance(obj.get(key), typ) or (typ is int and isinstance(obj[key], bool)):
        raise ProposalStoreError(f"{where}{key}: missing or wrong type")
    return obj[key]


def from_document(doc: object) -> GenerationPlanProposal:
    version = _need(doc, "proposal_schema_version", int, "")
    if version not in SUPPORTED_PROPOSAL_VERSIONS:
        raise ProposalStoreError(f"unsupported proposal_schema_version {version}")
    pid = _need(doc, "proposal_id", str, "")
    if not _ID_RE.match(pid):
        raise ProposalStoreError("proposal_id: invalid format")
    approved = _need(doc, "approved", bool, "")
    src, prov = _need(doc, "source", dict, ""), _need(doc, "provenance", dict, "")
    spec_id = _need(src, "spec_id", str, "source.")
    if check_spec_id(spec_id):
        raise ProposalStoreError("source.spec_id: invalid id")
    sha = _need(src, "spec_sha256", str, "source.")
    if not _SHA_RE.match(sha):
        raise ProposalStoreError("source.spec_sha256: not a SHA-256 hex digest")
    try:
        a = validate_analysis(_need(doc, "analysis", dict, ""), spec_id)
    except AIResponseError as exc:
        raise ProposalStoreError(str(exc)) from None
    assert set(a) == set(FIELD_ORDER)
    return GenerationPlanProposal(
        spec_id=spec_id,
        spec_schema_version=_need(src, "spec_schema_version", int, "source."),
        analyzer=_need(prov, "analyzer", str, "provenance."),
        created_at=_need(prov, "generated_at", str, "provenance."),
        summary=a["summary"],
        test_categories=tuple(a["test_categories"]),
        open_questions=tuple(a["clarification_questions"]),
        approved=approved,
        proposal_id=pid,
        provider=_need(prov, "provider", str, "provenance."),
        model=_need(prov, "model", str, "provenance."),
        model_reported=_need(prov, "model_reported", str, "provenance."),
        prompt_version=_need(prov, "prompt_version", str, "provenance."),
        spec_sha256=sha,
        proposal_schema_version=version,
        input_analysis=a["input_analysis"],
        output_analysis=a["output_analysis"],
        constraint_interpretation=a["constraint_interpretation"],
        suggested_algorithm=a["suggested_algorithm"],
        generator_strategy=a["generator_strategy"],
        reference_approach=a["reference_approach"],
        edge_cases=tuple(a["edge_cases"]),
        ambiguities=tuple(a["ambiguities"]),
        risks_and_assumptions=tuple(a["risks_and_assumptions"]),
    )


class ProposalStore:
    def __init__(self, workspace: Path | None = None) -> None:
        self.dir = Path(workspace or "workspace") / PROPOSALS_DIRNAME

    def path_for(self, proposal_id: str) -> Path:
        if not isinstance(proposal_id, str) or not _ID_RE.match(proposal_id):
            raise ProposalStoreError("invalid proposal id")
        return safe_join(self.dir.resolve(), f"{proposal_id}.json")

    def save(self, proposal: GenerationPlanProposal) -> Path:
        if proposal.approved:
            raise ProposalStoreError("refusing to store a new proposal that is already approved")
        document = to_document(proposal)
        from_document(document)  # never write something we could not read back
        data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        target = self.path_for(proposal.proposal_id)
        try:
            return write_new_atomic(self.dir, target.name, data)
        except FileExistsError:
            raise ProposalStoreError(f"proposal {proposal.proposal_id!r} already exists; proposals are never overwritten") from None
        except OSError as exc:
            raise ProposalStoreError(f"cannot store proposal: {exc.strerror or exc}") from exc

    def load(self, proposal_id: str) -> GenerationPlanProposal:
        path = self.path_for(proposal_id)
        if not path.is_file():
            raise ProposalNotFoundError(f"proposal {proposal_id!r} not found in {self.dir}")
        try:
            proposal = from_document(read_json_file(path))
        except SpecStoreError as exc:
            raise ProposalStoreError(str(exc)) from None
        except ProposalStoreError as exc:
            raise ProposalStoreError(f"{path.name} is invalid: {exc}") from None
        if proposal.proposal_id != proposal_id:
            raise ProposalStoreError(f"{path.name}: proposal_id does not match the file name")
        return proposal

    def list_ids(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json") if p.is_file() and _ID_RE.match(p.stem))
