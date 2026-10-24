"""Local JSON storage for Problem Specifications.

Workspace layout (only `problems/` is written by this phase). Each kind of
artifact lives in its own directory so user-authored data is never mixed with
machine-produced data:

    workspace/
      problems/    <id>.json   user-authored specifications (this module)
      proposals/   (future) AI-generated generation-plan proposals
      generators/  (future) human-approved generators
      suites/      (future) generated test suites
      reports/     (future) execution / quality reports
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from ..paths import safe_join
from .model import (
    ProblemSpec,
    SpecValidationError,
    check_spec_id,
    now_iso,
    parse_spec,
)

DEFAULT_WORKSPACE = Path("workspace")
PROBLEMS_DIRNAME = "problems"
MAX_FILE_BYTES = 1_000_000


class SpecStoreError(Exception):
    pass


class SpecExistsError(SpecStoreError):
    pass


class SpecNotFoundError(SpecStoreError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def read_json_file(path: Path) -> object:
    """Read a UTF-8 JSON file (a leading BOM is tolerated) with clear errors."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SpecStoreError(f"{path.name}: file is larger than {MAX_FILE_BYTES} bytes")
        raw = path.read_bytes()
    except FileNotFoundError:
        raise SpecNotFoundError(f"file not found: {path}") from None
    except OSError as exc:
        raise SpecStoreError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError:
        raise SpecStoreError(f"{path.name}: not valid UTF-8") from None
    except ValueError as exc:
        raise SpecStoreError(f"{path.name}: invalid JSON: {exc}") from exc


def _serialize(spec: ProblemSpec) -> bytes:
    return (json.dumps(spec.to_dict(), indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class SpecStore:
    def __init__(self, workspace: Path | None = None) -> None:
        self.root = Path(workspace or DEFAULT_WORKSPACE)
        self.dir = self.root / PROBLEMS_DIRNAME

    def path_for(self, spec_id: str) -> Path:
        errors = check_spec_id(spec_id)
        if errors:
            raise SpecStoreError(errors[0])
        return safe_join(self.dir.resolve(), f"{spec_id}.json")

    def exists(self, spec_id: str) -> bool:
        return self.path_for(spec_id).is_file()

    def save(self, spec: ProblemSpec, *, overwrite: bool = False) -> ProblemSpec:
        """Atomically write the spec. Without `overwrite`, an existing id is an error."""
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.path_for(spec.id)
        exists_msg = f"problem {spec.id!r} already exists (use --force to overwrite)"
        if not overwrite and target.exists():
            raise SpecExistsError(exists_msg)
        now = now_iso()
        created = spec.created_at or now
        if overwrite and target.is_file():
            try:
                created = self.load(spec.id).created_at or created
            except SpecStoreError:
                pass  # a corrupt old file is being replaced; keep the new timestamp
        spec = replace(spec, created_at=created, updated_at=now)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{spec.id}.", suffix=".tmp", dir=self.dir)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(_serialize(spec))
                fh.flush()
                os.fsync(fh.fileno())
            if overwrite:
                os.replace(tmp, target)
            else:
                try:
                    os.link(tmp, target)  # fails if target appeared meanwhile: never overwrites
                except FileExistsError:
                    raise SpecExistsError(exists_msg) from None
        finally:
            tmp.unlink(missing_ok=True)
        return spec

    def load(self, spec_id: str) -> ProblemSpec:
        path = self.path_for(spec_id)
        if not path.is_file():
            raise SpecNotFoundError(f"problem {spec_id!r} not found in {self.dir}")
        try:
            spec = parse_spec(read_json_file(path), require_timestamps=True)
        except SpecValidationError as exc:
            raise SpecStoreError(f"{path.name} is invalid: {exc}") from exc
        if spec.id != spec_id:
            raise SpecStoreError(f"{path.name}: id {spec.id!r} does not match the file name")
        return spec

    def list_ids(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(
            p.stem for p in self.dir.glob("*.json") if p.is_file() and not check_spec_id(p.stem)
        )
