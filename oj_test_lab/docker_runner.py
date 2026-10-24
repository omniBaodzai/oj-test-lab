"""Docker-based runner for TRUSTED demonstration C++ solutions.

SECURITY SCOPE: this is a local educational prototype. Docker isolation is not
a strong boundary against hostile code; never expose this as a service and
never run untrusted submissions with it. Only candidates registered by the
project (see Problem.cpp_candidates) can be run from the CLI.

Compiled programs only ever run inside disposable containers, never on the host.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Pinned by tag and digest (gcc 13.3.0, Debian-based, linux/amd64 + others).
IMAGE = "gcc:13.3.0@sha256:1d71f0f3450214bef38fe09e6f610fb6cca90cf97b43f4ce845bfc32a4168818"
CPP_STANDARD = "c++17"
COMPILE_FLAGS = ("-std=c++17", "-O2", "-pipe")
CONTAINER_USER = "65534:65534"  # nobody:nogroup
MAX_CAPTURE = 64 * 1024  # bytes kept from compiler/program stdout and stderr

# Docker CLI itself uses these exit codes when it cannot start the container
# (125 = daemon error, 126/127 = command cannot be invoked / not found).
INFRA_EXIT_CODES = frozenset({125, 126, 127})

_SAFE_MOUNT_PATH = re.compile(r"^[^,\"'\n\r=]+$")

RunFn = Callable[..., "subprocess.CompletedProcess[bytes]"]


class DockerUnavailableError(RuntimeError):
    """Docker CLI or daemon cannot be used (infrastructure, not a candidate failure)."""


class InfrastructureError(RuntimeError):
    """Container/runtime failure unrelated to the candidate's behaviour."""


@dataclass(frozen=True)
class Limits:
    compile_timeout: float = 60.0
    run_timeout: float = 5.0
    compile_memory: str = "512m"
    run_memory: str = "128m"
    compile_pids: int = 128
    run_pids: int = 16
    cpus: str = "1"


@dataclass
class CompileResult:
    status: str  # "OK", "CE" (compiler error / timeout), "INFRA"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    detail: str = ""


@dataclass
class RunResult:
    status: str  # "OK", "TLE", "RE", "INFRA"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    time_ms: float = 0.0
    detail: str = ""


def _clip(data: bytes | None) -> str:
    text = (data or b"")[:MAX_CAPTURE].decode("utf-8", errors="replace")
    return text + ("\n[truncated]" if data and len(data) > MAX_CAPTURE else "")


def _check_mount_path(path: Path) -> str:
    text = str(path.resolve())
    if not _SAFE_MOUNT_PATH.match(text):
        raise InfrastructureError(f"unsafe path for bind mount: {text!r}")
    return text


def _mount(source: Path, target: str, readonly: bool) -> list[str]:
    spec = f"type=bind,source={_check_mount_path(source)},target={target}"
    return ["--mount", spec + (",readonly" if readonly else "")]


def security_args(name: str, memory: str, pids: int, cpus: str, tmpfs_size: str, noexec_tmp: bool) -> list[str]:
    """Isolation flags shared by compile and run containers."""
    tmp = f"/tmp:rw,nosuid,nodev,size={tmpfs_size}" + (",noexec" if noexec_tmp else "")
    return [
        "--rm",
        "--name", name,
        "--network", "none",
        "--user", CONTAINER_USER,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", tmp,
        "--memory", memory,
        "--memory-swap", memory,
        "--pids-limit", str(pids),
        "--cpus", cpus,
        "--ulimit", "fsize=67108864",
        "--ulimit", "core=0",
        "--workdir", "/tmp",
    ]


def build_compile_command(name: str, src_dir: Path, build_dir: Path, limits: Limits) -> list[str]:
    return [
        "docker", "run", *security_args(name, limits.compile_memory, limits.compile_pids, limits.cpus, "128m", False),
        *_mount(src_dir, "/src", True),
        *_mount(build_dir, "/build", False),
        IMAGE,
        "g++", *COMPILE_FLAGS, "-o", "/build/main", "/src/main.cpp",
    ]


def build_run_command(name: str, build_dir: Path, limits: Limits) -> list[str]:
    return [
        "docker", "run", "-i", *security_args(name, limits.run_memory, limits.run_pids, limits.cpus, "16m", True),
        *_mount(build_dir, "/build", True),
        IMAGE,
        "/build/main",
    ]


def build_version_command(name: str, limits: Limits) -> list[str]:
    return [
        "docker", "run", *security_args(name, "128m", 32, limits.cpus, "16m", True),
        IMAGE, "g++", "--version",
    ]


class DockerCppRunner:
    def __init__(self, limits: Limits | None = None, run_fn: RunFn | None = None) -> None:
        self.limits = limits or Limits()
        self._run: RunFn = run_fn or subprocess.run

    # -- low level -------------------------------------------------------
    def _docker(self, args: list[str], timeout: float, stdin: bytes | None = None):
        """Run a docker CLI command (argument list, no shell). Returns (proc, timed_out, ms)."""
        import time

        name = args[args.index("--name") + 1] if "--name" in args else None
        start = time.perf_counter()
        try:
            proc = self._run(args, input=stdin, capture_output=True, timeout=timeout, shell=False)
            return proc, False, (time.perf_counter() - start) * 1000
        except subprocess.TimeoutExpired:
            if name:
                self.remove_container(name)  # --rm does not fire when the CLI is killed
            return None, True, (time.perf_counter() - start) * 1000
        except FileNotFoundError as exc:
            raise DockerUnavailableError("docker CLI not found in PATH") from exc
        except OSError as exc:
            raise InfrastructureError(f"cannot execute docker: {exc}") from exc
        except BaseException:
            if name:
                self.remove_container(name)
            raise

    def remove_container(self, name: str) -> None:
        try:
            self._run(["docker", "rm", "-f", name], capture_output=True, timeout=30, shell=False)
        except (subprocess.SubprocessError, OSError):
            pass

    @staticmethod
    def _new_name(prefix: str) -> str:
        return f"ojtl-{prefix}-{uuid.uuid4().hex[:12]}"

    # -- public ----------------------------------------------------------
    def check_available(self) -> None:
        try:
            proc = self._run(["docker", "version", "--format", "{{.Server.Version}}"],
                             capture_output=True, timeout=30, shell=False)
        except FileNotFoundError as exc:
            raise DockerUnavailableError("docker CLI not found in PATH") from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise DockerUnavailableError(f"docker is not responding: {exc}") from exc
        if proc.returncode != 0:
            raise DockerUnavailableError("docker daemon is not reachable: " + _clip(proc.stderr).strip())

    def compiler_version(self) -> str:
        proc, timed_out, _ = self._docker(build_version_command(self._new_name("ver"), self.limits), 60)
        if timed_out or proc is None or proc.returncode != 0:
            detail = _clip(proc.stderr).strip() if proc else "timeout"
            raise InfrastructureError(f"cannot query compiler version (is image {IMAGE} pulled?): {detail}")
        return _clip(proc.stdout).splitlines()[0].strip()

    def compile(self, source: str, workdir: Path) -> tuple[CompileResult, Path]:
        """Compile `source` once. Returns the result and the build directory holding `main`."""
        src_dir = workdir / "src"
        build_dir = workdir / "build"
        src_dir.mkdir()
        build_dir.mkdir()
        (src_dir / "main.cpp").write_bytes(source.encode("utf-8"))
        cmd = build_compile_command(self._new_name("cc"), src_dir, build_dir, self.limits)
        proc, timed_out, _ = self._docker(cmd, self.limits.compile_timeout)
        if timed_out:
            return CompileResult("CE", timed_out=True, detail="compilation timed out"), build_dir
        assert proc is not None
        out, err = _clip(proc.stdout), _clip(proc.stderr)
        if proc.returncode in INFRA_EXIT_CODES:
            return CompileResult("INFRA", out, err, proc.returncode, detail="docker failed to run compiler"), build_dir
        if proc.returncode != 0:
            return CompileResult("CE", out, err, proc.returncode, detail="compiler returned nonzero exit code"), build_dir
        if not (build_dir / "main").is_file():
            return CompileResult("INFRA", out, err, 0, detail="compiler succeeded but produced no executable"), build_dir
        return CompileResult("OK", out, err, 0), build_dir

    def run_test(self, build_dir: Path, input_text: str) -> RunResult:
        cmd = build_run_command(self._new_name("run"), build_dir, self.limits)
        proc, timed_out, ms = self._docker(cmd, self.limits.run_timeout, input_text.encode("utf-8"))
        if timed_out:
            return RunResult("TLE", time_ms=ms, detail=f"exceeded {self.limits.run_timeout}s")
        assert proc is not None
        out, err = _clip(proc.stdout), _clip(proc.stderr)
        if proc.returncode in INFRA_EXIT_CODES:
            return RunResult("INFRA", out, err, proc.returncode, ms, "docker failed to start container")
        if proc.returncode != 0:
            return RunResult("RE", out, err, proc.returncode, ms, _describe_exit(proc.returncode))
        return RunResult("OK", out, err, 0, ms)


def _describe_exit(code: int) -> str:
    if code > 128:
        return f"killed by signal {code - 128}" + (" (possibly out of memory)" if code == 137 else "")
    return f"nonzero exit code {code}"


@dataclass
class TempWorkdir:
    """Disposable host directory holding the source and the build output."""

    path: Path = field(init=False)

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="ojtl-"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
