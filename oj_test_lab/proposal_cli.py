"""`proposal` command group: read-only browsing of AI proposals. No approval or execution yet."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from .ai.prompts import spec_fingerprint
from .ai.proposal_store import ProposalStore
from .specs import SpecStore, SpecStoreError
from .specs.ai_interface import GenerationPlanProposal
from .specs.store import DEFAULT_WORKSPACE

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text: str) -> str:
    """Strip terminal escape sequences/control characters from untrusted AI text before printing."""
    return _CTRL.sub("", _ANSI.sub("", text))


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("proposal", help="browse AI-generated proposals (never executed, not approvable yet)")
    p.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE, help="default: ./workspace")
    ps = p.add_subparsers(dest="proposal_command", required=True)
    lst = ps.add_parser("list", help="list stored proposals")
    lst.add_argument("--spec-id", help="only proposals for this specification")
    show = ps.add_parser("show", help="show one proposal")
    show.add_argument("--id", required=True, help="proposal id")


def _bullets(title: str, items: tuple[str, ...]) -> None:
    print(f"\n{title}:")
    for item in items or ("(none reported)",):
        print(f"  - {clean(item)}")


def print_proposal(p: GenerationPlanProposal, path: Path | None = None) -> None:
    print(f"Proposal:   {p.proposal_id}")
    print(f"Spec:       {p.spec_id} (sha256 {p.spec_sha256[:12]}...)")
    print(f"Analyzer:   {clean(p.analyzer)} (reported model: {clean(p.model_reported) or 'n/a'}), prompt {p.prompt_version}")
    print(f"Generated:  {p.created_at}")
    print(f"Approved:   {str(p.approved).lower()}  (AI output is UNREVIEWED and not proven correct)")
    if path:
        print(f"File:       {path}")
    print(f"\nSummary:\n{clean(p.summary)}")
    for title, text in (
        ("Input analysis", p.input_analysis), ("Output analysis", p.output_analysis),
        ("Constraint interpretation", p.constraint_interpretation), ("Suggested algorithm", p.suggested_algorithm),
        ("Generator strategy", p.generator_strategy), ("Reference solution approach", p.reference_approach),
    ):
        print(f"\n{title}:\n{clean(text)}")
    _bullets("Test categories", p.test_categories)
    _bullets("Edge cases", p.edge_cases)
    _bullets("Ambiguities / contradictions", p.ambiguities)
    _bullets("Questions for a human", p.open_questions)
    _bullets("Risks and assumptions", p.risks_and_assumptions)


def _list(a: argparse.Namespace) -> int:
    store = ProposalStore(a.workspace)
    shown = 0
    for pid in store.list_ids():
        try:
            p = store.load(pid)
        except Exception as exc:  # one bad file must not hide the others
            print(f"{pid}\t[INVALID] {clean(str(exc))[:120]}")
            shown += 1
            continue
        if a.spec_id and p.spec_id != a.spec_id:
            continue
        print(f"{p.proposal_id}\tspec={p.spec_id}\tapproved={str(p.approved).lower()}\t{clean(p.analyzer)}\t{p.created_at}")
        shown += 1
    if not shown:
        print(f"No proposals in {store.dir}")
    return 0


def _show(a: argparse.Namespace) -> int:
    store = ProposalStore(a.workspace)
    p = store.load(a.id)
    print_proposal(p, store.path_for(a.id))
    try:
        current = spec_fingerprint(SpecStore(a.workspace).load(p.spec_id))
        if current != p.spec_sha256:
            print("\nWARNING: the specification changed after this proposal was generated (stale proposal).")
    except SpecStoreError:
        print("\nWARNING: the source specification could not be loaded; cannot check whether this proposal is stale.")
    return 0


def run(a: argparse.Namespace) -> int:
    return {"list": _list, "show": _show}[a.proposal_command](a)
