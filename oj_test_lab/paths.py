"""Safe filesystem helpers."""

from __future__ import annotations

import re
from pathlib import Path

TEST_FILE_RE = re.compile(r"^(\d{2,4})\.(in|out)$")
MANAGED_FILES = ("manifest.json", "quality-report.json", "tests.zip", "cpp-execution-report.json")


class UnsafePathError(ValueError):
    pass


def numbered_name(test_id: int, ext: str) -> str:
    if not isinstance(test_id, int) or test_id < 1 or test_id > 9999:
        raise UnsafePathError(f"Invalid test id: {test_id!r}")
    if ext not in ("in", "out"):
        raise UnsafePathError(f"Invalid extension: {ext!r}")
    return f"{test_id:02d}.{ext}"


def safe_join(base: Path, name: str) -> Path:
    """Join `name` to `base`, refusing anything that escapes `base`."""
    if not name or name != Path(name).name or name in (".", ".."):
        raise UnsafePathError(f"Unsafe file name: {name!r}")
    target = (base / name).resolve()
    if target.parent != base.resolve():
        raise UnsafePathError(f"Path escapes output directory: {name!r}")
    return target


def prepare_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise UnsafePathError(f"Not a directory: {resolved}")
    if resolved.parent == resolved:
        raise UnsafePathError("Refusing to use a filesystem root as output directory")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def clean_managed_files(out_dir: Path) -> None:
    """Remove only files this tool creates (NN.in/NN.out and the 3 metadata files)."""
    for child in out_dir.iterdir():
        if child.is_file() and (TEST_FILE_RE.match(child.name) or child.name in MANAGED_FILES):
            child.unlink()


def write_text(path: Path, text: str) -> None:
    """UTF-8 write without newline translation (LF on every OS)."""
    path.write_bytes(text.encode("utf-8"))


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")
