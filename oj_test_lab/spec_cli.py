"""`problem` command group: manage user-authored Problem Specifications.

Everything here is structural data management. No AI call, no code execution.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ai.analyzer import ProblemAnalyzer
from .ai.config import load_config
from .ai.factory import build_provider
from .ai.prompts import PROMPT_VERSION, build_messages
from .ai.proposal_store import ProposalStore
from .problems import REGISTRY
from .proposal_cli import clean
from .specs import SpecStore, SpecStoreError, SpecValidationError, parse_spec
from .specs.store import DEFAULT_WORKSPACE, read_json_file

SEMANTIC_NOTE = (
    "Note: only the STRUCTURE was validated. The statement and constraints are not "
    "understood by the tool; their correctness is for a human to review."
)


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("problem", help="manage user-authored problem specifications (data only)")
    p.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                   help="workspace directory (default: ./workspace; specs live in <workspace>/problems)")
    ps = p.add_subparsers(dest="problem_command", required=True)

    c = ps.add_parser("create", help="import a specification from a JSON file")
    c.add_argument("--file", required=True, type=Path, help="JSON file to import")
    c.add_argument("--force", action="store_true", help="overwrite an existing specification with the same id")

    ps.add_parser("list", help="list stored specifications")

    s = ps.add_parser("show", help="show a stored specification")
    s.add_argument("--id", required=True)

    v = ps.add_parser("validate", help="structurally validate a stored specification (--id) or a JSON file (--file)")
    g = v.add_mutually_exclusive_group(required=True)
    g.add_argument("--id")
    g.add_argument("--file", type=Path)

    an = ps.add_parser("analyze", help="ask the AI (OpenRouter) for a test-generation PROPOSAL; sends the spec to a third party")
    an.add_argument("--id", required=True)
    an.add_argument("--model", help="override OPENROUTER_MODEL")
    an.add_argument("--max-tokens", type=int, help="max output tokens (default: OPENROUTER_MAX_OUTPUT_TOKENS or 4000)")
    an.add_argument("--env-file", type=Path, default=Path(".env"), help="env file with OPENROUTER_* settings (default: ./.env)")
    an.add_argument("--dry-run", action="store_true", help="show what would be sent (size/model) without calling the API")


def _create(a: argparse.Namespace, store: SpecStore) -> int:
    spec = parse_spec(read_json_file(a.file))
    if spec.id in REGISTRY:
        raise SpecStoreError(f"id {spec.id!r} is a built-in executable problem; choose another id")
    saved = store.save(spec, overwrite=a.force)
    print(f"Saved specification {saved.id!r} -> {store.path_for(saved.id)}")
    print(SEMANTIC_NOTE)
    return 0


def _list(a: argparse.Namespace, store: SpecStore) -> int:
    ids = store.list_ids()
    if not ids:
        print(f"No specifications in {store.dir}")
        return 0
    for spec_id in ids:
        try:
            spec = store.load(spec_id)
            print(f"{spec.id}\t{spec.desired_test_count} tests\t{spec.title}")
        except SpecStoreError as exc:
            print(f"{spec_id}\t[INVALID] {exc}")
    return 0


def _show(a: argparse.Namespace, store: SpecStore) -> int:
    spec = store.load(a.id)
    print(f"ID:            {spec.id}")
    print(f"Title:         {spec.title}")
    print(f"Schema:        v{spec.schema_version}")
    print(f"Created/Updated: {spec.created_at} / {spec.updated_at}")
    print(f"Desired tests: {spec.desired_test_count}")
    print(f"\nStatement:\n{spec.statement}")
    print(f"\nInput:\n{spec.input_description}")
    print(f"\nOutput:\n{spec.output_description}")
    print("\nConstraints:")
    for item in spec.constraints:
        print(f"  - {item}")
    for i, smp in enumerate(spec.samples, 1):
        print(f"\nSample {i} input:\n{smp.input}\nSample {i} output:\n{smp.output}")
    return 0


def _validate(a: argparse.Namespace, store: SpecStore) -> int:
    if a.id:
        spec = store.load(a.id)  # raises with details when invalid
        where = str(store.path_for(a.id))
    else:
        spec = parse_spec(read_json_file(a.file))
        where = str(a.file)
    print(f"OK: {spec.id!r} ({where}) has a valid structure")
    print(SEMANTIC_NOTE)
    return 0


def _analyze(a: argparse.Namespace, store: SpecStore) -> int:
    config = load_config(a.env_file, model=a.model, max_output_tokens=a.max_tokens, require_api_key=not a.dry_run)
    spec = store.load(a.id)  # structural validation; raises with details when invalid
    system, user = build_messages(spec)
    if a.dry_run:
        print(f"Dry run: nothing sent. Would call {config.base_url} with model {config.model!r}, "
              f"prompt {PROMPT_VERSION}, ~{len(system) + len(user)} characters, max {config.max_output_tokens} output tokens.")
        return 0
    print(f"Sending specification {spec.id!r} to {config.base_url} (model {config.model}); this is a paid API call...")
    analyzer = ProblemAnalyzer(build_provider(config), max_output_tokens=config.max_output_tokens)
    proposal = analyzer.analyze(spec)
    path = ProposalStore(a.workspace).save(proposal)
    print(f"\nProposal {proposal.proposal_id} saved -> {path}")
    print("approved = false. This is an UNREVIEWED AI proposal; no tests were generated and no code was executed.")
    print(f"\nSummary: {clean(proposal.summary)}")
    print(f"Test categories: {len(proposal.test_categories)}, edge cases: {len(proposal.edge_cases)}, "
          f"ambiguities: {len(proposal.ambiguities)}, questions for a human: {len(proposal.open_questions)}")
    print(f"View it with: python -m oj_test_lab proposal show --id {proposal.proposal_id}")
    return 0


_HANDLERS = {"create": _create, "list": _list, "show": _show, "validate": _validate, "analyze": _analyze}


def run(a: argparse.Namespace) -> int:
    try:
        return _HANDLERS[a.problem_command](a, SpecStore(a.workspace))
    except SpecValidationError as exc:
        print(f"error: specification is invalid ({len(exc.errors)} problem(s)):", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
