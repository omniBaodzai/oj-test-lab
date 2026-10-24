"""`code-proposal` command group: AI-written code proposals for HUMAN review.

Nothing here compiles, imports or runs the proposed code. Approval only records a decision.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ai.code_proposer import DEFAULT_CODE_MAX_TOKENS, CodeProposer
from .ai.code_prompts import CODE_PROMPT_VERSION, build_code_messages
from .ai.code_schema import static_warnings
from .ai.code_store import CodeProposalStore, CodeProposalStoreError
from .ai.config import load_config
from .ai.factory import build_provider
from .ai.proposal_store import ProposalStore
from .proposal_cli import clean
from .specs import SpecStore
from .specs.store import DEFAULT_WORKSPACE

NOT_EXECUTED = "No AI-generated code was compiled, imported or executed."


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("code-proposal", help="AI code proposals (generator + reference) for human review; never executed")
    p.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE, help="default: ./workspace")
    ps = p.add_subparsers(dest="code_command", required=True)

    g = ps.add_parser("generate", help="ask the AI for generator + reference code (paid call; sends spec and plan to a third party)")
    g.add_argument("--proposal-id", required=True, help="id of an existing plan proposal (see `proposal list`)")
    g.add_argument("--model", help="override OPENROUTER_MODEL")
    g.add_argument("--max-tokens", type=int, help=f"max output tokens (default: at least {DEFAULT_CODE_MAX_TOKENS})")
    g.add_argument("--env-file", type=Path, default=Path(".env"))
    g.add_argument("--dry-run", action="store_true", help="check inputs and show what would be sent; no API call")

    lst = ps.add_parser("list", help="list code proposals")
    lst.add_argument("--spec-id")
    lst.add_argument("--proposal-id", help="only code proposals made from this plan proposal")

    show = ps.add_parser("show", help="show one code proposal, including the code")
    show.add_argument("--id", required=True)
    show.add_argument("--no-code", action="store_true", help="omit the source code")

    ap = ps.add_parser("approve", help="record a human approval (does not run anything)")
    ap.add_argument("--id", required=True)
    ap.add_argument("--reviewer", required=True, help="name of the human reviewer")
    ap.add_argument("--confirm-id", help="type the code proposal id to confirm non-interactively")

    ex = ps.add_parser("export", help="write the sources as inert .txt files for manual inspection")
    ex.add_argument("--id", required=True)
    ex.add_argument("--out-dir", type=Path, help="default: <workspace>/code-exports")


def _is_interactive() -> bool:
    return sys.stdin.isatty()


def _generate(a: argparse.Namespace) -> int:
    config = load_config(a.env_file, model=a.model, max_output_tokens=a.max_tokens, require_api_key=not a.dry_run)
    tokens = a.max_tokens or max(config.max_output_tokens, DEFAULT_CODE_MAX_TOKENS)
    plan = ProposalStore(a.workspace).load(a.proposal_id)
    spec = SpecStore(a.workspace).load(plan.spec_id)
    from .ai.prompts import spec_fingerprint

    if spec_fingerprint(spec) != plan.spec_sha256:
        raise CodeProposalStoreError(
            f"plan proposal {plan.proposal_id!r} is stale: specification {spec.id!r} changed. Run `problem analyze` again."
        )
    system, user = build_code_messages(spec, plan)
    if a.dry_run:
        print(f"Dry run: nothing sent. Would call {config.base_url} with model {config.model!r}, prompt {CODE_PROMPT_VERSION}, "
              f"~{len(system) + len(user)} characters, max {tokens} output tokens.")
        return 0
    print(f"Sending specification {spec.id!r} and plan {plan.proposal_id!r} to {config.base_url} (model {config.model}); this is a paid API call...")
    proposal = CodeProposer(build_provider(config), max_output_tokens=tokens).propose(spec, plan)
    path = CodeProposalStore(a.workspace).save(proposal)
    print(f"\nCode proposal {proposal.code_proposal_id} saved -> {path}")
    print(f"approved = false. UNTRUSTED AI code for human review. {NOT_EXECUTED}")
    print(f"Blocking questions: {len(proposal.blocking_questions)} of {len(proposal.unresolved_questions)}; "
          f"static warnings: {len(static_warnings(proposal.generator_code, proposal.reference_code))}")
    print(f"Review with: python -m oj_test_lab code-proposal show --id {proposal.code_proposal_id}")
    return 0


def _list(a: argparse.Namespace) -> int:
    store = CodeProposalStore(a.workspace)
    shown = 0
    for cid in store.list_ids():
        try:
            p = store.load(cid)
        except Exception as exc:  # one bad file must not hide the others
            print(f"{cid}\t[INVALID] {clean(str(exc))[:120]}")
            shown += 1
            continue
        if (a.spec_id and p.spec_id != a.spec_id) or (a.proposal_id and p.plan_proposal_id != a.proposal_id):
            continue
        state = "approved" if p.approved else "unapproved"
        stale = "stale" if store.staleness(p) else "current"
        print(f"{p.code_proposal_id}\tspec={p.spec_id}\tplan={p.plan_proposal_id}\t{state}\t{stale}\t{p.created_at}")
        shown += 1
    if not shown:
        print(f"No code proposals in {store.dir}")
    return 0


def _bullets(title: str, items: list[str]) -> None:
    print(f"\n{title}:")
    for item in items or ["(none reported)"]:
        print(f"  - {clean(item)}")


def _show(a: argparse.Namespace) -> int:
    store = CodeProposalStore(a.workspace)
    p = store.load(a.id)
    print(f"Code proposal: {p.code_proposal_id}")
    print(f"Spec:          {p.spec_id} (sha256 {p.spec_sha256[:12]}...)   Plan: {p.plan_proposal_id}")
    print(f"Generated by:  {clean(p.provider)}/{clean(p.model)} (reported: {clean(p.model_reported) or 'n/a'}), prompt {p.prompt_version}, at {p.created_at}")
    print(f"content_sha256: {p.content_sha256}")
    if p.approved:
        print(f"Approved:      TRUE by {clean(p.reviewer or '')} at {p.approved_at}")
    else:
        print("Approved:      false  (UNTRUSTED AI code, not executed, not proven correct)")
    for reason in store.staleness(p):
        print(f"WARNING (stale): {clean(reason)}")
    for title, text in (("Algorithm", p.algorithm_explanation), ("Complexity", p.complexity),
                        ("Numeric range analysis", p.numeric_range_analysis), ("Test strategy", p.test_strategy)):
        print(f"\n{title}:\n{clean(text)}")
    _bullets("Edge cases", list(p.edge_cases))
    _bullets("Adversarial tests", list(p.adversarial_tests))
    _bullets("Assumptions", list(p.assumptions))
    _bullets("Risks", list(p.risks))
    _bullets("Unresolved questions", [("[BLOCKING] " if q.blocking else "[open] ") + q.question for q in p.unresolved_questions])
    warnings = static_warnings(p.generator_code, p.reference_code)
    _bullets("Static warnings (advisory regex only; NOT a safety check)", warnings)
    if not a.no_code:
        print(f"\n===== generator_code (Python, UNTRUSTED) =====\n{clean(p.generator_code)}")
        print(f"===== reference_code (C++17, UNTRUSTED) =====\n{clean(p.reference_code)}")
    print(f"\n{NOT_EXECUTED}")
    return 0


def _approve(a: argparse.Namespace) -> int:
    store = CodeProposalStore(a.workspace)
    p = store.load(a.id)
    print(f"About to approve code proposal {p.code_proposal_id} (content_sha256 {p.content_sha256[:16]}...).")
    print("Approval records YOUR review decision only. It does not run, compile or import anything.")
    for w in static_warnings(p.generator_code, p.reference_code):
        print(f"  static warning: {w}")
    if a.confirm_id is not None:
        confirmed = a.confirm_id == p.code_proposal_id
    elif _is_interactive():
        confirmed = input(f"Type the code proposal id ({p.code_proposal_id}) to confirm you reviewed the code: ").strip() == p.code_proposal_id
    else:
        raise CodeProposalStoreError("confirmation required: run interactively or pass --confirm-id with the exact proposal id")
    if not confirmed:
        raise CodeProposalStoreError("confirmation did not match the proposal id; nothing was approved")
    done = store.approve(a.id, a.reviewer)
    print(f"\nApproved by {done.reviewer} at {done.approved_at}. {NOT_EXECUTED}")
    return 0


def _export(a: argparse.Namespace) -> int:
    store = CodeProposalStore(a.workspace)
    out_dir = a.out_dir or (a.workspace / "code-exports")
    for path in store.export_sources(a.id, out_dir):
        print(f"wrote {path}")
    print(f"Files are inert .txt copies for manual inspection. {NOT_EXECUTED}")
    return 0


_HANDLERS = {"generate": _generate, "list": _list, "show": _show, "approve": _approve, "export": _export}


def run(a: argparse.Namespace) -> int:
    return _HANDLERS[a.code_command](a)
