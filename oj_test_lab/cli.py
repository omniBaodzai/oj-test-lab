"""Command line interface: python -m oj_test_lab <command> ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import code_proposal_cli, proposal_cli, spec_cli
from .ai.errors import AIError
from .ai.proposal_store import ProposalStoreError
from .cpp_evaluator import (
    CPP_REPORT_NAME,
    build_cpp_report,
    evaluate_cpp_candidate,
    load_expected,
    write_cpp_report,
)
from .docker_runner import CPP_STANDARD, DockerCppRunner, DockerUnavailableError, InfrastructureError, Limits
from .models import GENERATOR_VERSION, Problem
from .paths import UnsafePathError
from .problems import REGISTRY, get_problem
from .specs import SpecStoreError
from .suite import (
    SuiteError,
    build_quality_report,
    build_suite,
    check_cases,
    export_suite,
    load_suite,
    verify_dir,
    write_report,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oj_test_lab", description="OJ Test Lab - test case generator and quality checker")
    p.add_argument("--version", action="version", version=f"oj_test_lab {GENERATOR_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, dir_help: str | None = None) -> None:
        sp.add_argument("--problem", required=True, help=f"problem id ({', '.join(sorted(REGISTRY))})")
        if dir_help:
            sp.add_argument("--input-dir", required=True, type=Path, help=dir_help)

    g = sub.add_parser("generate", help="generate, validate and export a test suite")
    common(g)
    g.add_argument("--count", type=int, help="number of tests (default: problem default)")
    g.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    g.add_argument("--output-dir", type=Path, help="default: output/<problem>")
    g.add_argument("--no-zip", action="store_true", help="do not create tests.zip")

    v = sub.add_parser("validate", help="validate .in files of an existing suite")
    common(v, "directory containing NN.in files")

    e = sub.add_parser("evaluate", help="run simulated wrong solutions and write quality-report.json")
    common(e, "directory containing NN.in files")
    e.add_argument("--no-write", action="store_true", help="do not write quality-report.json")
    e.add_argument("--strict", action="store_true", help="exit 1 if any wrong solution escapes detection")

    ec = sub.add_parser("evaluate-cpp", help="compile and run trusted C++ candidates in Docker (real execution)")
    common(ec, "directory containing NN.in and NN.out files")
    ec.add_argument("--candidate", action="append", metavar="ID",
                    help="trusted candidate id (repeatable; default: all registered for the problem)")
    ec.add_argument("--time-limit", type=float, default=5.0, help="per-test timeout in seconds (default 5)")
    ec.add_argument("--report", type=Path, help=f"default: <input-dir>/{CPP_REPORT_NAME}")

    x = sub.add_parser("export", help="re-export an existing suite (.out, manifest, report, zip)")
    common(x, "directory containing NN.in files")
    x.add_argument("--output-dir", type=Path, help="default: same as --input-dir")
    x.add_argument("--zip", action="store_true", help="create tests.zip")
    spec_cli.add_parser(sub)
    proposal_cli.add_parser(sub)
    code_proposal_cli.add_parser(sub)
    return p


def _summary(report: dict) -> None:
    print(f"Tests: {report['tests_generated']} generated, {report['tests_valid']} valid, "
          f"{report['duplicate_tests']} duplicate")
    for s in report["incorrect_solutions"]:
        status = f"detected by {len(s['detected_by_tests'])} test(s)" if s["detected"] else "ESCAPED"
        print(f"  [simulated] {s['id']}: {status}")
    for w in report["warnings"]:
        print(f"  warning: {w}")


def _load_checked(problem: Problem, in_dir: Path):
    cases, seed = load_suite(in_dir)
    check = check_cases(problem, cases)
    return cases, seed, check


def _cmd_generate(a: argparse.Namespace, problem: Problem) -> int:
    count = a.count if a.count is not None else problem.default_count
    out_dir = a.output_dir or Path("output") / problem.id
    cases = build_suite(problem, count, a.seed)
    report = export_suite(problem, cases, a.seed, out_dir, make_zip=not a.no_zip)
    print(f"Generated {len(cases)} tests for '{problem.id}' (seed={a.seed}) -> {out_dir.resolve()}")
    _summary(report)
    return 0


def _cmd_validate(a: argparse.Namespace, problem: Problem) -> int:
    cases, _, check = _load_checked(problem, a.input_dir)
    errors = [f"{name}: {'; '.join(msgs)}" for name, msgs in check["invalid"].items()]
    errors += [f"duplicate tests: {x} and {y}" for x, y in check["duplicates"]]
    errors += verify_dir(problem, a.input_dir, cases)
    if errors:
        for line in errors:
            print(f"error: {line}", file=sys.stderr)
        return 1
    print(f"OK: {len(cases)} tests are valid, unique and consistent")
    return 0


def _cmd_evaluate(a: argparse.Namespace, problem: Problem) -> int:
    cases, seed, check = _load_checked(problem, a.input_dir)
    if check["invalid"] or check["duplicates"]:
        print("error: suite is invalid; run the 'validate' command for details", file=sys.stderr)
        return 1
    report = build_quality_report(problem, cases, seed, check)
    _summary(report)
    if not a.no_write:
        print(f"Report written to {write_report(a.input_dir, report)}")
    return 1 if a.strict and report["escaped_incorrect_solutions"] else 0


def _cmd_export(a: argparse.Namespace, problem: Problem) -> int:
    cases, seed, check = _load_checked(problem, a.input_dir)
    if check["invalid"] or check["duplicates"]:
        print("error: refusing to export an invalid suite; run 'validate' for details", file=sys.stderr)
        return 1
    out_dir = a.output_dir or a.input_dir
    export_suite(problem, cases, seed, out_dir, make_zip=a.zip)
    print(f"Exported {len(cases)} tests to {out_dir.resolve()}" + (" (+ tests.zip)" if a.zip else ""))
    return 0


def _make_runner(limits: Limits) -> DockerCppRunner:
    return DockerCppRunner(limits)


def _cmd_evaluate_cpp(a: argparse.Namespace, problem: Problem) -> int:
    if a.time_limit <= 0:
        raise SuiteError("--time-limit must be positive")
    by_id = {c.id: c for c in problem.cpp_candidates}
    if not by_id:
        raise SuiteError(f"problem {problem.id!r} has no trusted C++ candidates")
    selected = []
    for cid in a.candidate or list(by_id):
        if cid not in by_id:
            raise SuiteError(f"unknown trusted candidate {cid!r}. Available: {', '.join(by_id)}")
        selected.append(by_id[cid])
    cases, seed, check = _load_checked(problem, a.input_dir)
    if check["invalid"] or check["duplicates"]:
        raise SuiteError("suite is invalid; run the 'validate' command for details")
    try:
        expected = load_expected(a.input_dir, cases)
    except FileNotFoundError as exc:
        raise SuiteError(str(exc)) from exc
    limits = Limits(run_timeout=a.time_limit)
    runner = _make_runner(limits)
    try:
        runner.check_available()
        compiler = runner.compiler_version()
        results = [evaluate_cpp_candidate(runner, c, cases, expected, problem.compare_mode) for c in selected]
    except (DockerUnavailableError, InfrastructureError) as exc:
        print(f"error: infrastructure failure (not a candidate verdict): {exc}", file=sys.stderr)
        return 1
    report = build_cpp_report(problem, results, compiler, limits, seed, len(cases))
    report_path = a.report or Path(a.input_dir) / CPP_REPORT_NAME
    write_cpp_report(report_path, report)
    print(f"Real C++ execution in Docker ({compiler}, {CPP_STANDARD}), {len(cases)} tests")
    infra = False
    for r in results:
        counts = {s: len(r.ids(s)) for s in ("AC", "WA", "RE", "TLE", "CE")}
        summary = " ".join(f"{k}={v}" for k, v in counts.items())
        print(f"  [real] {r.candidate.id}: {summary}")
        if r.failed_ids:
            print(f"         failed tests: {', '.join(map(str, r.failed_ids))}")
        if r.compile.status == "CE":
            print(f"         compile error: {r.compile.stderr.strip()[:300]}")
        for err in r.infra_errors:
            infra = True
            print(f"         INFRASTRUCTURE ERROR: {err}", file=sys.stderr)
    print(f"Report written to {report_path.resolve()} (simulated quality report untouched)")
    return 1 if infra else 0


_COMMANDS = {"generate": _cmd_generate, "validate": _cmd_validate, "evaluate": _cmd_evaluate,
             "export": _cmd_export, "evaluate-cpp": _cmd_evaluate_cpp}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "problem":
            return spec_cli.run(args)
        if args.command == "proposal":
            return proposal_cli.run(args)
        if args.command == "code-proposal":
            return code_proposal_cli.run(args)
        problem = get_problem(args.problem)
        return _COMMANDS[args.command](args, problem)
    except (SuiteError, SpecStoreError, AIError, ProposalStoreError, UnsafePathError, KeyError, OSError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
