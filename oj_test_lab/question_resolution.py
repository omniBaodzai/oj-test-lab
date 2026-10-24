"""Audited answers for blocking CodeProposal questions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .ai.code_model import CodeProposal
from .ai.code_store import CodeProposalStore, CodeProposalStoreError, check_reviewer
from .ai.code_model import plan_fingerprint
from .ai.proposal_store import ProposalStore
from .fsutil import replace_atomic, write_new_atomic
from .specs.store import SpecStore

RESOLUTION_DIRNAME = "question-resolutions"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_ANSWER_CHARS = 4000


class QuestionResolutionError(CodeProposalStoreError):
    pass


def question_key(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _path(workspace: Path, proposal_id: str) -> Path:
    if not isinstance(proposal_id, str) or not _ID_RE.match(proposal_id):
        raise QuestionResolutionError("invalid code proposal id")
    return (Path(workspace) / RESOLUTION_DIRNAME / f"{proposal_id}.json").resolve()


def _binding(workspace: Path, proposal: CodeProposal) -> dict:
    spec = SpecStore(workspace).load(proposal.spec_id)
    from .ai.prompts import spec_fingerprint
    current_spec_sha = spec_fingerprint(spec)
    plan = ProposalStore(workspace).load(proposal.plan_proposal_id)
    current_plan_sha = plan_fingerprint(plan)
    return {
        "proposal_id": proposal.code_proposal_id,
        "spec_id": proposal.spec_id,
        "spec_sha256": proposal.spec_sha256,
        "current_spec_sha256": current_spec_sha,
        "content_sha256": proposal.content_sha256,
        "plan_proposal_id": proposal.plan_proposal_id, "plan_sha256": proposal.plan_sha256,
        "current_plan_sha256": current_plan_sha,
        "parent_code_proposal_id": proposal.parent_code_proposal_id,
    }


def load_events(workspace: Path, proposal_id: str) -> list[dict]:
    path = _path(workspace, proposal_id)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QuestionResolutionError(f"resolution file is invalid: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise QuestionResolutionError("resolution file has invalid shape")
    return [event for event in data["events"] if isinstance(event, dict)]


def _write_events(workspace: Path, proposal_id: str, events: list[dict]) -> None:
    path = _path(workspace, proposal_id)
    data = (json.dumps({"schema_version": 1, "proposal_id": proposal_id, "events": events}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        replace_atomic(path.parent, path.name, data)
    else:
        write_new_atomic(path.parent, path.name, data)


def question_status(workspace: Path, proposal: CodeProposal) -> dict:
    binding = _binding(workspace, proposal)
    stale = (binding["spec_sha256"] != binding["current_spec_sha256"]
             or binding["plan_sha256"] != binding["current_plan_sha256"])
    events = load_events(workspace, proposal.code_proposal_id)
    latest: dict[str, dict] = {}
    for event in events:
        latest[event.get("question_sha256", "")] = event
    questions = []
    for question in proposal.unresolved_questions:
        key = question_key(question.question)
        event = latest.get(key)
        valid = bool(event and not stale and event.get("spec_sha256") == binding["current_spec_sha256"]
                     and event.get("content_sha256") == proposal.content_sha256
                     and event.get("proposal_id") == proposal.code_proposal_id
                     and event.get("plan_proposal_id") == proposal.plan_proposal_id
                     and event.get("plan_sha256") == binding["current_plan_sha256"]
                     and event.get("parent_code_proposal_id") == proposal.parent_code_proposal_id
                     and isinstance(event.get("answer"), str) and event["answer"].strip())
        questions.append({
            "question": question.question, "blocking": question.blocking, "question_sha256": key,
            "source": {"proposal_id": proposal.code_proposal_id, "spec_id": proposal.spec_id,
                        "plan_proposal_id": proposal.plan_proposal_id, "parent_code_proposal_id": proposal.parent_code_proposal_id},
            "resolved": valid, "stale": bool(event and not valid),
            "resolution": event if valid else None,
        })
    blocking = [item for item in questions if item["blocking"]]
    return {"proposal_id": proposal.code_proposal_id, "binding": binding, "questions": questions,
            "all_blocking_resolved": bool(all(item["resolved"] for item in blocking)),
            "stale": stale or any(item["stale"] for item in questions)}


def resolve_question(workspace: Path, proposal_id: str, question_sha256: str, answer: object, reviewer: object) -> dict:
    store = CodeProposalStore(workspace)
    proposal = store.load(proposal_id)
    status = question_status(workspace, proposal)
    match = next((item for item in status["questions"] if item["question_sha256"] == question_sha256), None)
    if match is None:
        raise QuestionResolutionError("question does not belong to the selected proposal")
    if not match["blocking"]:
        raise QuestionResolutionError("only blocking questions require resolution")
    if status["stale"]:
        raise QuestionResolutionError("cannot resolve questions for a stale specification or proposal lineage")
    if not isinstance(answer, str) or not answer.strip():
        raise QuestionResolutionError("answer must not be empty")
    if len(answer) > MAX_ANSWER_CHARS or _CONTROL.search(answer):
        raise QuestionResolutionError("answer is too long or contains control characters")
    name = check_reviewer(reviewer)
    binding = status["binding"]
    events = load_events(workspace, proposal_id)
    event = {"proposal_id": proposal_id, "question_sha256": question_sha256, "question": match["question"],
             "answer": answer.strip(), "reviewer": name, "resolved_at": _now(),
             "spec_id": binding["spec_id"], "spec_sha256": binding["current_spec_sha256"],
             "content_sha256": binding["content_sha256"], "plan_proposal_id": binding["plan_proposal_id"],
             "plan_sha256": binding["current_plan_sha256"],
             "parent_code_proposal_id": binding["parent_code_proposal_id"]}
    events.append(event)
    _write_events(workspace, proposal_id, events)
    return question_status(workspace, store.load(proposal_id))


def approval_questions_resolved(workspace: Path, proposal: CodeProposal) -> bool:
    return question_status(workspace, proposal)["all_blocking_resolved"]