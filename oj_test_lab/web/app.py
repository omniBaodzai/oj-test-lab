"""Local-only FastAPI application for the OJ Test Lab workflow."""

from __future__ import annotations

import os
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..ai.analyzer import ProblemAnalyzer
from ..ai.code_proposer import CodeProposer
from ..ai.code_store import CodeProposalStore, CodeProposalStoreError
from ..ai.config import load_config
from ..ai.errors import AIError
from ..ai.factory import build_provider
from ..ai.proposal_store import ProposalStore, ProposalStoreError
from ..ai.repair import RepairUnchangedError, repair_code, repair_usage_estimate
from ..ai.manual import create_manual_revision
from ..code_review import load_report, report_from_dict, save_report, validate_code
from ..question_resolution import QuestionResolutionError, approval_questions_resolved, question_status, resolve_question
from ..generation import GenerationError, generate_tests, load_suite_info, read_suite_file, zip_path
from ..sandbox import SandboxError, UntrustedSandbox
from ..specs.model import ProblemSpec, SpecValidationError, parse_spec
from ..specs.store import SpecStore, SpecStoreError

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DEFAULT_WORKSPACE = Path("workspace")
CSRF_HEADER = "x-oj-test-lab"
CSRF_VALUE = "local-ui"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def _public_spec(spec: ProblemSpec) -> dict:
    return _jsonable(spec)


def _validation_summary(workspace: Path, code_id: str) -> dict:
    try:
        report = load_report(workspace, code_id)
    except CodeProposalStoreError:
        return {"status": "not-validated"}
    return {
        "status": "passed" if report.get("passed") else "failed",
        "checked_at": report.get("checked_at", ""),
        "content_sha256": report.get("content_sha256", ""),
        "errors": report.get("errors", [])[:5],
        "warnings": report.get("warnings", [])[:5],
    }


def create_app(workspace: Path | str = DEFAULT_WORKSPACE, *, sandbox: UntrustedSandbox | None = None) -> FastAPI:
    workspace = Path(workspace)
    app = FastAPI(title="OJ Test Lab", docs_url=None, redoc_url=None)
    app.state.workspace = workspace
    app.state.sandbox = sandbox or UntrustedSandbox.from_flag(False)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            return JSONResponse({"error": "local-only server"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return JSONResponse({"error": "missing local request token"}, status_code=403)
        return await call_next(request)

    def error(exc: Exception, status: int = 400) -> HTTPException:
        detail = str(exc)
        if isinstance(exc, GenerationError) and exc.details:
            detail = {"message": detail, "details": exc.details}
        return HTTPException(status_code=status, detail=detail)

    @app.get("/", response_class=FileResponse)
    async def index() -> Path:
        return STATIC / "index.html"

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "sandbox": app.state.sandbox.status()}

    @app.post("/api/problems")
    async def create_problem(request: Request) -> dict:
        try:
            data = dict(await request.json())
            data.pop("overwrite", None)
            spec = parse_spec(data)
            saved = SpecStore(workspace).save(spec)
            return {"problem": _public_spec(saved)}
        except (SpecValidationError, SpecStoreError, ValueError) as exc:
            raise error(exc) from exc

    @app.get("/api/problems/{spec_id}")
    async def get_problem(spec_id: str) -> dict:
        try:
            spec = SpecStore(workspace).load(spec_id)
        except SpecStoreError as exc:
            raise error(exc, 404) from exc
        return {"problem": _public_spec(spec)}

    @app.get("/api/problems/{spec_id}/code-proposals")
    async def list_code_proposals(spec_id: str) -> dict:
        try:
            SpecStore(workspace).load(spec_id)
            store = CodeProposalStore(workspace)
            proposals = []
            for code_id in store.list_ids():
                proposal = store.load(code_id)
                if proposal.spec_id != spec_id:
                    continue
                proposals.append({
                    "code_proposal_id": proposal.code_proposal_id,
                    "parent_code_proposal_id": proposal.parent_code_proposal_id,
                    "repair_attempt": proposal.repair_attempt,
                    "created_at": proposal.created_at,
                    "approved": proposal.approved,
                    "reviewer": proposal.reviewer,
                    "content_sha256": proposal.content_sha256,
                    "stale": store.staleness(proposal),
                    "validation": _validation_summary(workspace, proposal.code_proposal_id),
                })
            return {"problem_id": spec_id, "proposals": proposals}
        except (SpecStoreError, CodeProposalStoreError) as exc:
            raise error(exc, 404) from exc

    @app.post("/api/problems/{spec_id}/analyze")
    async def analyze(spec_id: str, request: Request) -> dict:
        try:
            spec = SpecStore(workspace).load(spec_id)
            body = await request.json()
            config = load_config(Path(body["env_file"]) if body.get("env_file") else Path(".env"), model=body.get("model"))
            plan = ProblemAnalyzer(build_provider(config), max_output_tokens=config.max_output_tokens).analyze(spec)
            ProposalStore(workspace).save(plan)
            return {"analysis": _jsonable(plan)}
        except (SpecStoreError, AIError, ProposalStoreError, KeyError, ValueError) as exc:
            raise error(exc) from exc

    @app.post("/api/problems/{spec_id}/code")
    async def propose_code(spec_id: str, request: Request) -> dict:
        try:
            body = await request.json()
            spec = SpecStore(workspace).load(spec_id)
            plan_id = body.get("proposal_id")
            if not isinstance(plan_id, str):
                raise ValueError("proposal_id is required")
            plan = ProposalStore(workspace).load(plan_id)
            if plan.spec_id != spec_id:
                raise ValueError("proposal belongs to a different problem")
            config = load_config(Path(body["env_file"]) if body.get("env_file") else Path(".env"), model=body.get("model"), max_output_tokens=body.get("max_tokens"))
            proposal = CodeProposer(build_provider(config), max_output_tokens=config.max_output_tokens).propose(spec, plan)
            CodeProposalStore(workspace).save(proposal)
            return {"code": _jsonable(proposal)}
        except (SpecStoreError, ProposalStoreError, CodeProposalStoreError, AIError, KeyError, ValueError) as exc:
            raise error(exc) from exc

    @app.get("/api/code/{code_id}")
    async def get_code(code_id: str) -> dict:
        try:
            proposal = CodeProposalStore(workspace).load(code_id)
        except CodeProposalStoreError as exc:
            raise error(exc, 404) from exc
        store = CodeProposalStore(workspace)
        return {"code": _jsonable(proposal), "stale": store.staleness(proposal),
            "validation": _validation_summary(workspace, code_id)}

    @app.get("/api/code/{code_id}/questions")
    async def questions(code_id: str) -> dict:
        try:
            proposal = CodeProposalStore(workspace).load(code_id)
            return question_status(workspace, proposal)
        except (CodeProposalStoreError, SpecStoreError, QuestionResolutionError) as exc:
            raise error(exc, 404) from exc

    @app.post("/api/code/{code_id}/questions/resolve")
    async def resolve(code_id: str, request: Request) -> dict:
        try:
            body = await request.json()
            result = resolve_question(workspace, code_id, body.get("question_sha256"), body.get("answer"), body.get("reviewer"))
            return result
        except (CodeProposalStoreError, SpecStoreError, QuestionResolutionError, KeyError, TypeError, ValueError) as exc:
            raise error(exc) from exc

    @app.post("/api/code/{code_id}/manual-edit")
    async def manual_edit(code_id: str, request: Request) -> dict:
        try:
            body = await request.json()
            store = CodeProposalStore(workspace)
            parent = store.load(code_id)
            spec = SpecStore(workspace).load(parent.spec_id)
            edited = create_manual_revision(parent, spec, body.get("generator_code"), body.get("reference_code"))
            store.save(edited)
            persisted = store.load(edited.code_proposal_id)
            return {"proposal_id": persisted.code_proposal_id, "code": _jsonable(persisted),
                    "validation": _validation_summary(workspace, persisted.code_proposal_id)}
        except (CodeProposalStoreError, SpecStoreError, KeyError, TypeError, ValueError) as exc:
            raise error(exc) from exc

    @app.post("/api/code/{code_id}/approve")
    async def approve(code_id: str, request: Request) -> dict:
        try:
            body = await request.json()
            if body.get("confirm_id") != code_id:
                raise ValueError("confirm_id must exactly match code proposal id")
            proposal_store = CodeProposalStore(workspace)
            proposal = proposal_store.load(code_id)
            if not approval_questions_resolved(workspace, proposal):
                raise ValueError("approval requires all blocking questions to be resolved for the current proposal and specification")
            validation = load_report(workspace, code_id)
            if (not validation.get("passed") or validation.get("content_sha256") != proposal.content_sha256
                    or validation.get("spec_id") != proposal.spec_id or validation.get("spec_sha256") != proposal.spec_sha256):
                raise ValueError("approval requires a passing validation report for the current proposal content")
            proposal = proposal_store.approve(code_id, body.get("reviewer"))
            return {"code": _jsonable(proposal)}
        except (CodeProposalStoreError, ValueError, json.JSONDecodeError) as exc:
            raise error(exc) from exc

    @app.post("/api/code/{code_id}/validate")
    async def validate(code_id: str) -> dict:
        try:
            store = CodeProposalStore(workspace)
            proposal = store.load(code_id)
            spec = SpecStore(workspace).load(proposal.spec_id)
            report = validate_code(proposal, spec, app.state.sandbox)
            save_report(workspace, report)
            return {"proposal_id": proposal.code_proposal_id, "code": _jsonable(proposal), "validation": report.to_dict()}
        except (CodeProposalStoreError, SpecStoreError, SandboxError, ValueError) as exc:
            raise error(exc, 503 if isinstance(exc, SandboxError) else 400) from exc

    @app.get("/api/code/{code_id}/validation")
    async def validation(code_id: str) -> dict:
        try:
            return {"validation": load_report(workspace, code_id)}
        except CodeProposalStoreError as exc:
            raise error(exc, 404) from exc

    @app.post("/api/code/{code_id}/repair")
    async def repair(code_id: str, request: Request) -> dict:
        try:
            body = await request.json()
            store = CodeProposalStore(workspace)
            parent = store.load(code_id)
            report = load_report(workspace, code_id)
            if report.get("passed") or report.get("content_sha256") != parent.content_sha256:
                raise ValueError("repair requires a failed validation report for the current content")
            spec = SpecStore(workspace).load(parent.spec_id)
            config = load_config(Path(body["env_file"]) if body.get("env_file") else Path(".env"), model=body.get("model"), max_output_tokens=8000)
            repaired = repair_code(build_provider(config), spec, parent, report_from_dict(report))
            store.save(repaired)
            persisted = store.load(repaired.code_proposal_id)
            unchanged_parts = [name for name, old, new in (("generator_code", parent.generator_code, persisted.generator_code), ("reference_code", parent.reference_code, persisted.reference_code)) if old == new]
            return {"proposal_id": persisted.code_proposal_id, "code": _jsonable(persisted), "unchanged_parts": unchanged_parts,
                    "repair_usage_estimate": repair_usage_estimate(parent, report_from_dict(report))}
        except (RepairUnchangedError, CodeProposalStoreError, SpecStoreError, AIError, KeyError, ValueError) as exc:
            raise error(exc) from exc

    @app.post("/api/generate")
    async def generate(request: Request) -> dict:
        try:
            body = await request.json()
            result = generate_tests(workspace, body.get("code_id"), body.get("count"), body.get("seed"), app.state.sandbox)
            return result
        except (GenerationError, SandboxError, CodeProposalStoreError, KeyError, TypeError, ValueError) as exc:
            raise error(exc, 503 if isinstance(exc, SandboxError) else 400) from exc

    @app.get("/api/suites/{suite_id}")
    async def suite(suite_id: str) -> dict:
        try:
            return load_suite_info(workspace, suite_id)
        except GenerationError as exc:
            raise error(exc, 404) from exc

    @app.get("/api/suites/{suite_id}/files/{name}")
    async def suite_file(suite_id: str, name: str) -> dict:
        try:
            return read_suite_file(workspace, suite_id, name)
        except (GenerationError, FileNotFoundError) as exc:
            raise error(exc, 404) from exc

    @app.get("/api/suites/{suite_id}/download")
    async def download(suite_id: str) -> FileResponse:
        try:
            return FileResponse(zip_path(workspace, suite_id), filename="tests.zip", media_type="application/zip")
        except GenerationError as exc:
            raise error(exc, 404) from exc

    return app


app = create_app(Path(os.environ.get("OJ_TEST_LAB_WORKSPACE", "workspace")))