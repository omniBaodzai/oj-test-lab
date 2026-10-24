"""Storage for code proposals: workspace/code-proposals/<code_proposal_id>.json.

Stored layout (code_proposal_schema_version = 1):

    {
      "code_proposal_schema_version": 1,
      "code_proposal_id": "...",
      "source": {"spec_id", "spec_schema_version", "spec_sha256", "plan_proposal_id", "plan_sha256"},
      "provenance": {"provider", "model", "model_reported", "prompt_version", "generated_at"},
      "content": {generator_code, reference_code, algorithm_explanation, complexity, test_strategy,
                  numeric_range_analysis, edge_cases, adversarial_tests, assumptions, risks,
                  unresolved_questions: [{"question", "blocking"}]},
      "content_sha256": "...",
      "approval": {"approved", "reviewer", "approved_at", "approved_content_sha256"}
    }

New proposals are never overwritten. The only later change is approval (atomic replace), which is
bound to `content_sha256`. Nothing in this module executes the stored code.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from ..fsutil import replace_atomic, write_new_atomic
from ..paths import safe_join
from ..specs.model import check_spec_id, now_iso
from ..specs.store import SpecStore, SpecStoreError, read_json_file
from .code_model import CODE_PROPOSAL_SCHEMA_VERSION, CodeProposal, Question, plan_fingerprint
from .code_schema import validate_code_response
from .errors import AIResponseError
from .prompts import spec_fingerprint
from .proposal_store import ProposalStore, ProposalStoreError

CODE_PROPOSALS_DIRNAME = "code-proposals"
SUPPORTED_VERSIONS = (CODE_PROPOSAL_SCHEMA_VERSION,)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_REVIEWER_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")


class CodeProposalStoreError(ProposalStoreError):
    pass


class CodeProposalNotFoundError(CodeProposalStoreError):
    pass


def to_document(p: CodeProposal) -> dict:
    return {
        "code_proposal_schema_version": p.schema_version,
        "code_proposal_id": p.code_proposal_id,
        "source": {
            "spec_id": p.spec_id, "spec_schema_version": p.spec_schema_version, "spec_sha256": p.spec_sha256,
            "plan_proposal_id": p.plan_proposal_id, "plan_sha256": p.plan_sha256,
        },
        "provenance": {
            "provider": p.provider, "model": p.model, "model_reported": p.model_reported,
            "prompt_version": p.prompt_version, "generated_at": p.created_at,
        },
        "content": {"problem_id": p.spec_id, **{k: v for k, v in p.content_dict().items() if k not in ("generator_language", "reference_language")}},
        "content_sha256": p.content_sha256,
        "approval": {
            "approved": p.approved, "reviewer": p.reviewer, "approved_at": p.approved_at,
            "approved_content_sha256": p.approved_content_sha256,
        },
        "history": {
            "parent_code_proposal_id": p.parent_code_proposal_id,
            "repair_attempt": p.repair_attempt,
        },
    }


def _need(obj: object, key: str, typ: type, where: str):
    if not isinstance(obj, dict) or not isinstance(obj.get(key), typ) or (typ is int and isinstance(obj[key], bool)):
        raise CodeProposalStoreError(f"{where}{key}: missing or wrong type")
    return obj[key]


def _sha(obj: dict, key: str, where: str) -> str:
    value = _need(obj, key, str, where)
    if not _SHA_RE.match(value):
        raise CodeProposalStoreError(f"{where}{key}: not a SHA-256 hex digest")
    return value


def check_reviewer(name: object) -> str:
    if not isinstance(name, str) or not _REVIEWER_RE.match(name.strip()) or not name.strip():
        raise CodeProposalStoreError("reviewer must be 1-80 printable characters")
    return name.strip()


def from_document(doc: object) -> CodeProposal:
    version = _need(doc, "code_proposal_schema_version", int, "")
    if version not in SUPPORTED_VERSIONS:
        raise CodeProposalStoreError(f"unsupported code_proposal_schema_version {version}")
    cid = _need(doc, "code_proposal_id", str, "")
    if not _ID_RE.match(cid):
        raise CodeProposalStoreError("code_proposal_id: invalid format")
    src, prov, approval = _need(doc, "source", dict, ""), _need(doc, "provenance", dict, ""), _need(doc, "approval", dict, "")
    history = doc.get("history", {})
    if not isinstance(history, dict):
        raise CodeProposalStoreError("history: must be an object")
    parent_id = history.get("parent_code_proposal_id")
    attempt = history.get("repair_attempt", 0)
    if parent_id is not None and (not isinstance(parent_id, str) or not _ID_RE.match(parent_id)):
        raise CodeProposalStoreError("history.parent_code_proposal_id: invalid id")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise CodeProposalStoreError("history.repair_attempt: must be a non-negative integer")
    spec_id = _need(src, "spec_id", str, "source.")
    if check_spec_id(spec_id):
        raise CodeProposalStoreError("source.spec_id: invalid id")
    plan_id = _need(src, "plan_proposal_id", str, "source.")
    if not _ID_RE.match(plan_id):
        raise CodeProposalStoreError("source.plan_proposal_id: invalid id")
    content = dict(_need(doc, "content", dict, ""))
    try:
        data = validate_code_response(content, spec_id)
    except AIResponseError as exc:
        raise CodeProposalStoreError(str(exc)) from None
    approved = _need(approval, "approved", bool, "approval.")
    reviewer, approved_at, approved_sha = approval.get("reviewer"), approval.get("approved_at"), approval.get("approved_content_sha256")
    p = CodeProposal(
        code_proposal_id=cid, spec_id=spec_id,
        spec_schema_version=_need(src, "spec_schema_version", int, "source."),
        spec_sha256=_sha(src, "spec_sha256", "source."), plan_proposal_id=plan_id, plan_sha256=_sha(src, "plan_sha256", "source."),
        provider=_need(prov, "provider", str, "provenance."), model=_need(prov, "model", str, "provenance."),
        model_reported=_need(prov, "model_reported", str, "provenance."),
        prompt_version=_need(prov, "prompt_version", str, "provenance."), created_at=_need(prov, "generated_at", str, "provenance."),
        generator_code=data["generator_code"], reference_code=data["reference_code"],
        algorithm_explanation=data["algorithm_explanation"], complexity=data["complexity"],
        test_strategy=data["test_strategy"], numeric_range_analysis=data["numeric_range_analysis"],
        edge_cases=tuple(data["edge_cases"]), adversarial_tests=tuple(data["adversarial_tests"]),
        assumptions=tuple(data["assumptions"]), risks=tuple(data["risks"]),
        unresolved_questions=tuple(Question(q["question"], q["blocking"]) for q in data["unresolved_questions"]),
        schema_version=version, approved=approved,
        reviewer=reviewer if isinstance(reviewer, str) else None,
        approved_at=approved_at if isinstance(approved_at, str) else None,
        approved_content_sha256=approved_sha if isinstance(approved_sha, str) else None,
        parent_code_proposal_id=parent_id,
        repair_attempt=attempt,
    )
    if _sha(doc, "content_sha256", "") != p.content_sha256:
        raise CodeProposalStoreError("content_sha256 does not match the stored content (file was modified)")
    if approved:
        if not (p.reviewer and p.approved_at and p.approved_content_sha256 == p.content_sha256):
            raise CodeProposalStoreError("approval record is incomplete or does not match the current content")
        check_reviewer(p.reviewer)
    elif reviewer is not None or approved_at is not None or approved_sha is not None:
        raise CodeProposalStoreError("approval fields must be null while approved is false")
    return p


class CodeProposalStore:
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace or "workspace")
        self.dir = self.workspace / CODE_PROPOSALS_DIRNAME

    def path_for(self, code_proposal_id: str) -> Path:
        if not isinstance(code_proposal_id, str) or not _ID_RE.match(code_proposal_id):
            raise CodeProposalStoreError("invalid code proposal id")
        return safe_join(self.dir.resolve(), f"{code_proposal_id}.json")

    @staticmethod
    def _encode(p: CodeProposal) -> bytes:
        return (json.dumps(to_document(p), indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def save(self, proposal: CodeProposal) -> Path:
        if proposal.approved:
            raise CodeProposalStoreError("refusing to store a new code proposal that is already approved")
        from_document(to_document(proposal))  # never write something we could not read back
        target = self.path_for(proposal.code_proposal_id)
        try:
            return write_new_atomic(self.dir, target.name, self._encode(proposal))
        except FileExistsError:
            raise CodeProposalStoreError(f"code proposal {proposal.code_proposal_id!r} already exists; never overwritten") from None
        except OSError as exc:
            raise CodeProposalStoreError(f"cannot store code proposal: {exc.strerror or exc}") from exc

    def load(self, code_proposal_id: str) -> CodeProposal:
        path = self.path_for(code_proposal_id)
        if not path.is_file():
            raise CodeProposalNotFoundError(f"code proposal {code_proposal_id!r} not found in {self.dir}")
        try:
            proposal = from_document(read_json_file(path))
        except SpecStoreError as exc:
            raise CodeProposalStoreError(str(exc)) from None
        except CodeProposalStoreError as exc:
            raise CodeProposalStoreError(f"{path.name} is invalid: {exc}") from None
        if proposal.code_proposal_id != code_proposal_id:
            raise CodeProposalStoreError(f"{path.name}: code_proposal_id does not match the file name")
        return proposal

    def list_ids(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json") if p.is_file() and _ID_RE.match(p.stem))

    def staleness(self, p: CodeProposal) -> list[str]:
        """Reasons why `p` no longer matches its sources (empty list = up to date)."""
        reasons: list[str] = []
        try:
            if spec_fingerprint(SpecStore(self.workspace).load(p.spec_id)) != p.spec_sha256:
                reasons.append(f"specification {p.spec_id!r} changed after this code proposal was generated")
        except SpecStoreError as exc:
            reasons.append(f"specification {p.spec_id!r} cannot be loaded ({str(exc)[:100]})")
        try:
            if plan_fingerprint(ProposalStore(self.workspace).load(p.plan_proposal_id)) != p.plan_sha256:
                reasons.append(f"plan proposal {p.plan_proposal_id!r} changed after this code proposal was generated")
        except ProposalStoreError as exc:
            reasons.append(f"plan proposal {p.plan_proposal_id!r} cannot be loaded ({str(exc)[:100]})")
        return reasons

    def approve(self, code_proposal_id: str, reviewer: str) -> CodeProposal:
        """Record a human approval. Does NOT run anything. Caller is responsible for obtaining confirmation."""
        name = check_reviewer(reviewer)
        p = self.load(code_proposal_id)
        if p.approved:
            raise CodeProposalStoreError(f"code proposal {code_proposal_id!r} is already approved by {p.reviewer}")
        if stale := self.staleness(p):
            raise CodeProposalStoreError("cannot approve a stale code proposal: " + "; ".join(stale))
        blocking = p.blocking_questions
        if blocking:
            from ..question_resolution import approval_questions_resolved
            if approval_questions_resolved(self.workspace, p):
                blocking = ()
        if blocking:
            raise CodeProposalStoreError(
                f"cannot approve: {len(blocking)} unresolved blocking question(s): "
                + " | ".join(q.question[:120] for q in blocking)
            )
        approved = replace(p, approved=True, reviewer=name, approved_at=now_iso(), approved_content_sha256=p.content_sha256)
        from_document(to_document(approved))
        path = self.path_for(code_proposal_id)
        try:
            replace_atomic(self.dir, path.name, self._encode(approved))
        except OSError as exc:
            raise CodeProposalStoreError(f"cannot record approval: {exc.strerror or exc}") from exc
        return approved

    def export_sources(self, code_proposal_id: str, out_dir: Path) -> list[Path]:
        """Write the proposed sources as inert .txt files for manual inspection. Never executes them."""
        p = self.load(code_proposal_id)
        target_dir = Path(out_dir) / p.code_proposal_id
        status = f"APPROVED by {p.reviewer} at {p.approved_at}" if p.approved else "NOT APPROVED"
        stale = self.staleness(p)
        review = (
            f"Code proposal {p.code_proposal_id}\n"
            f"Status: {status}\n"
            f"Stale: {'YES - ' + '; '.join(stale) if stale else 'no'}\n"
            f"Source: spec {p.spec_id} (sha256 {p.spec_sha256}), plan {p.plan_proposal_id}\n"
            f"Generated by {p.provider}/{p.model} at {p.created_at}\n"
            f"content_sha256: {p.content_sha256}\n\n"
            "WARNING: this code was written by an AI and has NOT been executed or proven correct.\n"
            "The files have a .txt suffix on purpose. Review them before any use.\n"
        )
        files = {"generator.py.txt": p.generator_code, "reference.cpp.txt": p.reference_code, "REVIEW.txt": review}
        written: list[Path] = []
        try:
            for name, text in files.items():
                written.append(write_new_atomic(target_dir, name, text.encode("utf-8")))
        except FileExistsError:
            raise CodeProposalStoreError(f"{target_dir} already contains exported files; choose another --out-dir") from None
        except OSError as exc:
            raise CodeProposalStoreError(f"cannot export: {exc.strerror or exc}") from exc
        return written
