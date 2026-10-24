"""Real C++ evaluation (Docker) of trusted candidates against an exported suite."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import docker_runner as dr
from .comparator import outputs_match
from .models import CompareMode, CppCandidate, Problem, TestCase
from .paths import numbered_name, read_text, safe_join, write_text

CPP_REPORT_NAME = "cpp-execution-report.json"
STATUSES = ("AC", "WA", "RE", "TLE", "CE")


@dataclass
class CppTestResult:
    test_id: int
    status: str  # AC/WA/RE/TLE/CE/INFRA
    time_ms: float = 0.0
    exit_code: int | None = None
    detail: str = ""


@dataclass
class CppCandidateResult:
    candidate: CppCandidate
    compile: dr.CompileResult
    tests: list[CppTestResult] = field(default_factory=list)

    def ids(self, status: str) -> list[int]:
        return [t.test_id for t in self.tests if t.status == status]

    @property
    def failed_ids(self) -> list[int]:
        return [t.test_id for t in self.tests if t.status not in ("AC", "INFRA")]

    @property
    def infra_errors(self) -> list[str]:
        errs = [f"compile: {self.compile.detail}"] if self.compile.status == "INFRA" else []
        return errs + [f"test {t.test_id}: {t.detail}" for t in self.tests if t.status == "INFRA"]


def load_expected(in_dir: Path, cases: list[TestCase]) -> dict[int, str]:
    expected = {}
    for c in cases:
        path = safe_join(in_dir.resolve(), numbered_name(c.id, "out"))
        if not path.is_file():
            raise FileNotFoundError(f"missing expected output {path.name}")
        expected[c.id] = read_text(path)
    return expected


def evaluate_cpp_candidate(
    runner: dr.DockerCppRunner,
    candidate: CppCandidate,
    cases: list[TestCase],
    expected: dict[int, str],
    mode: CompareMode,
) -> CppCandidateResult:
    source = candidate.source_path.read_bytes().decode("utf-8")
    with dr.TempWorkdir() as work:
        comp, build_dir = runner.compile(source, work)  # compiled once, reused for all tests
        result = CppCandidateResult(candidate, comp)
        if comp.status == "CE":
            result.tests = [CppTestResult(c.id, "CE", detail=comp.detail) for c in cases]
            return result
        if comp.status == "INFRA":
            result.tests = [CppTestResult(c.id, "INFRA", detail=comp.detail) for c in cases]
            return result
        for c in cases:
            run = runner.run_test(build_dir, c.input_text)
            if run.status == "OK":
                status = "AC" if outputs_match(expected[c.id], run.stdout, mode) else "WA"
            else:
                status = run.status  # TLE / RE / INFRA
            result.tests.append(CppTestResult(c.id, status, round(run.time_ms, 1), run.exit_code, run.detail))
    return result


def build_cpp_report(
    problem: Problem,
    results: list[CppCandidateResult],
    compiler: str,
    limits: dr.Limits,
    seed: int | None,
    test_count: int,
) -> dict:
    return {
        "problem": problem.id,
        "execution": "real",
        "simulated": False,
        "runner": "docker",
        "image": dr.IMAGE,
        "compiler": compiler,
        "cpp_standard": dr.CPP_STANDARD,
        "compile_flags": list(dr.COMPILE_FLAGS),
        "comparator": problem.compare_mode.value,
        "seed": seed,
        "test_count": test_count,
        "limits": {
            "run_timeout_seconds": limits.run_timeout,
            "compile_timeout_seconds": limits.compile_timeout,
            "run_memory": limits.run_memory,
            "run_pids": limits.run_pids,
            "cpus": limits.cpus,
        },
        "candidates": [
            {
                "id": r.candidate.id,
                "description": r.candidate.description,
                "execution": "real",
                "simulated": False,
                "compile_status": r.compile.status,
                "compile_output": {"stdout": r.compile.stdout, "stderr": r.compile.stderr},
                "counts": {s: len(r.ids(s)) for s in STATUSES} | {"INFRA": len(r.ids("INFRA"))},
                "failed_tests": r.failed_ids,
                "infrastructure_errors": r.infra_errors,
                "tests": [
                    {"id": t.test_id, "status": t.status, "time_ms": t.time_ms, "exit_code": t.exit_code,
                     **({"detail": t.detail} if t.detail else {})}
                    for t in r.tests
                ],
            }
            for r in results
        ],
        "warnings": [
            "Real execution of trusted, project-supplied C++ demonstration programs only.",
            "Docker isolation is not a strong security boundary against hostile code.",
        ],
    }


def write_cpp_report(path: Path, report: dict) -> None:
    write_text(path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
