"""Atomic file creation shared by stores that must never overwrite."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_new_atomic(directory: Path, name: str, data: bytes) -> Path:
    """Write `data` to a temp file in `directory`, fsync, then hard-link it to `name`.

    The link fails with FileExistsError if `name` exists, so an existing file is never
    replaced and readers never see a partial file. The temp file is always removed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    fd, tmp_name = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=directory)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def replace_atomic(directory: Path, name: str, data: bytes) -> Path:
    """Atomically replace an EXISTING file (used only for audited state changes such as approval)."""
    target = directory / name
    if not target.is_file():
        raise FileNotFoundError(name)
    fd, tmp_name = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=directory)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target
