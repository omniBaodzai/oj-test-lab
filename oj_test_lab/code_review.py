"""Static and sandboxed validation for unapproved AI code proposals."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .ai.code_model import CodeProposal
from .ai.code_schema import static_warnings
from .ai.code_store import CodeProposalStoreError
from .comparator import outputs_match
from .generation import _check_text, check_ranges
from .models import CompareMode
from .sandbox import SandboxError, UntrustedSandbox
from .specs.model import ProblemSpec
from .fsutil import replace_atomic, write_new_atomic

VALIDATION_DIRNAME = "code-validations"
VALIDATION_COUNTS = (1, 3, 20)
VALIDATION_SEEDS = (0, 42)
MAX_REPAIR_ATTEMPTS = 2


@dataclass
class ValidationReport:
    proposal_id: str
    content_sha256: str
    spec_id: str
    spec_sha256: str
    passed: bool = False
    checked_at: str = ""
    checks: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sandbox: dict = field(default_factory=dict)
    api_usage_estimate: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id, "content_sha256": self.content_sha256,
            "spec_id": self.spec_id, "spec_sha256": self.spec_sha256, "passed": self.passed,
            "checked_at": self.checked_at, "checks": self.checks, "errors": self.errors,
            "warnings": self.warnings, "sandbox": self.sandbox,
            "api_usage_estimate": self.api_usage_estimate,
        }


def _finding(report: ValidationReport, name: str, status: str, message: str = "") -> None:
    report.checks.append({"name": name, "status": status, "message": message})
    if status == "error":
        report.errors.append(f"{name}: {message}")
    elif status == "warning":
        report.warnings.append(f"{name}: {message}")


def _static_generator(report: ValidationReport, source: str) -> ast.FunctionDef | None:
    try:
        tree = ast.parse(source, filename="generator.py")
    except SyntaxError as exc:
        _finding(report, "generator.syntax", "error", f"line {exc.lineno}: {exc.msg}")
        return None
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "generate"]
    if len(functions) != 1 or isinstance(functions[0], ast.AsyncFunctionDef):
        _finding(report, "generator.interface", "error", "exactly one synchronous generate(seed, count) function is required")
        return None
    fn = functions[0]
    positional = [*fn.args.posonlyargs, *fn.args.args]
    if len(positional) < 2 or [arg.arg for arg in positional[:2]] != ["seed", "count"] or fn.args.vararg:
        _finding(report, "generator.interface", "error", "generate must accept seed and count as its first two positional arguments")
    else:
        _finding(report, "generator.interface", "pass", "generate(seed, count) found")
    for node in ast.walk(tree):
        if isinstance(node, ast.Set):
            _finding(report, "generator.determinism", "error", "set literal may produce unordered output")
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name) and node.iter.func.id == "set":
            _finding(report, "generator.determinism", "error", "iteration over set() has unordered output")
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Name) and node.iter.id in {"set", "unordered", "unordered_cases"}:
            _finding(report, "generator.determinism", "warning", f"iteration over {node.iter.id} may be unordered")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"urandom", "time", "time_ns"}:
            _finding(report, "generator.determinism", "error", f"nondeterministic call {node.func.attr}()")
    return fn


def _static_cpp(report: ValidationReport, source: str) -> None:
    if not re.search(r"\bmain\s*\(", source):
        _finding(report, "reference.interface", "error", "C++ reference must define main()")
    elif not re.search(r"#\s*include\s*<", source) and "iostream" not in source:
        _finding(report, "reference.interface", "warning", "no standard include was detected")
    else:
        _finding(report, "reference.interface", "pass", "main() and standard headers detected")


def _check_inputs(report: ValidationReport, spec: ProblemSpec, inputs: list[str], count: int, seed: int) -> None:
    errors: list[str] = []
    if len(inputs) != count:
        errors.append(f"returned {len(inputs)} inputs for count={count}")
    for index, text in enumerate(inputs, 1):
        _check_text("input", index, text, errors)
    if len(set(inputs)) != len(inputs):
        errors.append("duplicate input strings")
    range_status, range_errors = check_ranges(spec.constraints, inputs)
    if range_status.startswith("skipped:"):
        _finding(report, f"generator.constraints.count_{count}.seed_{seed}", "warning", range_status)
    errors.extend(range_errors)
    _finding(report, f"generator.contract.count_{count}.seed_{seed}", "error" if errors else "pass", "; ".join(errors) or "count, format, uniqueness and mechanical constraints passed")


def validate_code(proposal: CodeProposal, spec: ProblemSpec, sandbox: UntrustedSandbox) -> ValidationReport:
    report = ValidationReport(proposal.code_proposal_id, proposal.content_sha256, spec.id, proposal.spec_sha256,
                              checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    _static_generator(report, proposal.generator_code)
    _static_cpp(report, proposal.reference_code)
    for warning in static_warnings(proposal.generator_code, proposal.reference_code):
        _finding(report, "static.advisory", "warning", warning)
    report.api_usage_estimate = {"repair_attempts_used": 0, "repair_max_attempts": MAX_REPAIR_ATTEMPTS,
                                 "estimated_repair_input_tokens": 0, "estimated_repair_output_tokens": 0}
    try:
        report.sandbox = sandbox.require_ready()
    except SandboxError as exc:
        report.sandbox = sandbox.status()
        _finding(report, "sandbox.dynamic", "warning", f"dynamic checks skipped: {exc}")
        report.passed = not report.errors and False
        return report
    all_inputs: list[str] = []
    for seed in VALIDATION_SEEDS:
        for count in VALIDATION_COUNTS:
            first = sandbox.run_generator(proposal.generator_code, seed, count)
            second = sandbox.run_generator(proposal.generator_code, seed, count)
            if not first.ok:
                _finding(report, f"generator.execution.count_{count}.seed_{seed}", "error", first.error)
                continue
            if not second.ok or first.inputs != second.inputs:
                _finding(report, f"generator.process_determinism.count_{count}.seed_{seed}", "error", "separate sandbox processes returned different results")
            else:
                _finding(report, f"generator.process_determinism.count_{count}.seed_{seed}", "pass", "separate processes matched")
            _check_inputs(report, spec, first.inputs, count, seed)
            all_inputs.extend(first.inputs)
    if not all_inputs:
        report.passed = False
        return report
    samples = [sample.input if sample.input.endswith("\n") else sample.input + "\n" for sample in spec.samples]
    batch = all_inputs + samples
    reference = sandbox.run_reference(proposal.reference_code, batch)
    if not reference.compiled:
        _finding(report, "reference.cpp17.compile", "error", reference.error[:2000])
    elif not reference.complete or len(reference.results) != len(batch):
        _finding(report, "reference.execution", "error", "reference did not produce a successful result for every input")
    else:
        _finding(report, "reference.cpp17.compile", "pass", "compiled as C++17 in sandbox")
        for index, result in enumerate(reference.results, 1):
            errors: list[str] = []
            _check_text("output", index, result["stdout"], errors)
            if errors:
                _finding(report, f"reference.output.{index}", "error", "; ".join(errors))
        for sample, result in zip(spec.samples, reference.results[-len(samples):] if samples else []):
            if not outputs_match(sample.output, result["stdout"], CompareMode.TRIM):
                _finding(report, "reference.samples", "error", f"sample input {sample.input!r} output mismatch")
        repeat = sandbox.run_reference(proposal.reference_code, batch)
        if repeat.complete and [r.get("stdout") for r in repeat.results] == [r.get("stdout") for r in reference.results]:
            _finding(report, "reference.process_determinism", "pass", "separate sandbox process matched")
        else:
            _finding(report, "reference.process_determinism", "error", "separate sandbox processes differed")
    report.passed = not report.errors and any(c["name"] == "reference.cpp17.compile" and c["status"] == "pass" for c in report.checks)
    return report


def validation_path(workspace: Path, proposal_id: str) -> Path:
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,79}$", proposal_id):
        raise CodeProposalStoreError("invalid code proposal id")
    return (Path(workspace) / VALIDATION_DIRNAME / f"{proposal_id}.json").resolve()


def save_report(workspace: Path, report: ValidationReport) -> Path:
    path = validation_path(workspace, report.proposal_id)
    data = (json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return replace_atomic(path.parent, path.name, data)
    return write_new_atomic(path.parent, path.name, data)


def load_report(workspace: Path, proposal_id: str) -> dict:
    path = validation_path(workspace, proposal_id)
    if not path.is_file():
        raise CodeProposalStoreError("code proposal has not been validated")
    return json.loads(path.read_text(encoding="utf-8"))


def report_from_dict(data: dict) -> ValidationReport:
    return ValidationReport(
        proposal_id=str(data.get("proposal_id", "")), content_sha256=str(data.get("content_sha256", "")),
        spec_id=str(data.get("spec_id", "")), spec_sha256=str(data.get("spec_sha256", "")),
        passed=bool(data.get("passed")), checked_at=str(data.get("checked_at", "")),
        checks=list(data.get("checks", [])), errors=list(data.get("errors", [])),
        warnings=list(data.get("warnings", [])), sandbox=dict(data.get("sandbox", {})),
        api_usage_estimate=dict(data.get("api_usage_estimate", {})),
    )