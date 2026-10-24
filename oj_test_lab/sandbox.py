"""Execution backend for UNTRUSTED (AI-written, human-approved) code. Docker only; fail-closed.

Design
- Nothing from the host is mounted: no project directory, no volumes, no Docker socket, no environment.
  Source code and test data go in through the container's stdin; results come back through its stdout.
- Each call starts a disposable container: no network, non-root, all capabilities dropped,
  no-new-privileges, read-only root filesystem, a small tmpfs, memory/CPU/PID/file-size limits, a host-side
  wall-clock timeout and `docker rm -f` cleanup. The image is never pulled automatically (`--pull never`).
- Execution is OFF unless the operator opts in explicitly, and it refuses to run unless the Docker daemon
  runs inside a separate Linux VM or uses a sandboxing runtime (gVisor/Kata). Otherwise it reports the blocker.

HONEST LIMITS: a container shares a kernel with every other container in the same Docker VM. This is NOT a
strong boundary against a determined attacker (kernel or runtime bugs). It is meant for a local, single-user
tool running code that a human has reviewed. Never expose it as a public service.

The in-container drivers below are trusted code written by this project. The AI's code runs inside them,
in the container only; it is never compiled, imported or executed in the host process.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .docker_runner import IMAGE, DockerUnavailableError, security_args

ENABLE_ENV_VAR = "OJ_TEST_LAB_ENABLE_UNTRUSTED_EXEC"
SANDBOX_RUNTIMES = ("runsc", "kata", "kata-runtime", "kata-qemu")
MAX_STDERR = 64 * 1024


class SandboxError(RuntimeError):
    """Sandbox infrastructure failure (not a verdict about the candidate code)."""


class SandboxDisabledError(SandboxError):
    """Untrusted execution is switched off or blocked; `blocker` explains why."""

    def __init__(self, blocker: str) -> None:
        super().__init__(blocker)
        self.blocker = blocker


@dataclass(frozen=True)
class SandboxLimits:
    memory: str = "768m"
    pids: int = 64
    cpus: str = "1"
    tmpfs_size: str = "128m"
    generator_wall_seconds: float = 30.0
    generator_cpu_seconds: int = 20
    compile_seconds: int = 60
    run_seconds: float = 2.0
    run_budget_seconds: float = 120.0
    max_generator_stdout: int = 32 * 1024 * 1024
    max_reference_stdout: int = 32 * 1024 * 1024
    max_case_output: int = 1024 * 1024
    run_memory_bytes: int = 256 * 1024 * 1024


@dataclass
class ContainerResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    stdout_overflow: bool = False


@dataclass
class Isolation:
    allowed: bool
    level: str  # "vm", "sandboxed-runtime" or "none"
    reason: str


@dataclass
class GeneratorOutcome:
    ok: bool
    inputs: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ReferenceOutcome:
    compiled: bool
    error: str = ""  # compiler diagnostics or infrastructure detail when not compiled / aborted
    results: list[dict] = field(default_factory=list)  # one dict per executed input (stops at first failure)
    complete: bool = False


GENERATOR_DRIVER = r'''
import json, os, resource, sys
real_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
sys.stdout = sys.stderr  # stray prints from the generated code must not corrupt the result channel
req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
resource.setrlimit(resource.RLIMIT_AS, (req["memory_bytes"], req["memory_bytes"]))
resource.setrlimit(resource.RLIMIT_CPU, (req["cpu_seconds"], req["cpu_seconds"]))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
ns = {"__name__": "ai_generator"}
try:
    exec(compile(req["source"], "generator.py", "exec"), ns)
    fn = ns.get("generate")
    if not callable(fn):
        raise TypeError("generate() is not defined")
    result = fn(req["seed"], req["count"])
    if not isinstance(result, list) or not all(isinstance(x, str) for x in result):
        raise TypeError("generate() must return a list of str")
    out = {"ok": True, "inputs": result}
except BaseException as exc:
    out = {"ok": False, "error": (type(exc).__name__ + ": " + str(exc))[:500]}
real_out.write(json.dumps(out))
real_out.flush()
'''

REFERENCE_DRIVER = r'''
import json, os, resource, subprocess, sys, time
req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
real_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
def emit(obj):
    real_out.write(json.dumps(obj))
    real_out.flush()
    sys.exit(0)
work = "/tmp/w"
os.mkdir(work)
with open(work + "/main.cpp", "wb") as fh:
    fh.write(req["source"].encode("utf-8"))
try:
    cc = subprocess.run(["g++", "-std=c++17", "-O2", "-pipe", "-o", work + "/main", work + "/main.cpp"],
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=req["compile_seconds"])
except subprocess.TimeoutExpired:
    emit({"compiled": False, "error": "compilation timed out"})
if cc.returncode != 0 or not os.path.isfile(work + "/main"):
    emit({"compiled": False, "error": cc.stderr[:4000].decode("utf-8", "replace")})
limit = req["max_case_output"]
def limits():
    resource.setrlimit(resource.RLIMIT_AS, (req["run_memory_bytes"], req["run_memory_bytes"]))
    resource.setrlimit(resource.RLIMIT_CPU, (int(req["run_seconds"]) + 1, int(req["run_seconds"]) + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit + 1, limit + 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
results = []
spent = 0.0
complete = True
for inp in req["inputs"]:
    if spent > req["budget_seconds"]:
        results.append({"status": "BUDGET", "time_ms": 0, "exit_code": None, "stdout": "", "stderr": ""})
        complete = False
        break
    with open(work + "/stdout", "wb") as fo, open(work + "/stderr", "wb") as fe:
        t0 = time.perf_counter()
        try:
            p = subprocess.run([work + "/main"], input=inp.encode("utf-8"), stdout=fo, stderr=fe,
                               timeout=req["run_seconds"], preexec_fn=limits)
            status, code = ("OK" if p.returncode == 0 else "RE"), p.returncode
        except subprocess.TimeoutExpired:
            status, code = "TLE", None
        ms = (time.perf_counter() - t0) * 1000
    spent += ms / 1000
    with open(work + "/stdout", "rb") as fh:
        raw = fh.read(limit + 1)
    with open(work + "/stderr", "rb") as fh:
        err = fh.read(2000).decode("utf-8", "replace")
    text = ""
    if len(raw) > limit:
        status = "OLE"
    elif status == "OK":
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            status = "BAD_OUTPUT"
    results.append({"status": status, "time_ms": round(ms, 2), "exit_code": code, "stdout": text, "stderr": err})
    if status != "OK":
        complete = False
        break
emit({"compiled": True, "results": results, "complete": complete})
'''


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


class UntrustedSandbox:
    def __init__(
        self,
        enabled: bool = False,
        limits: SandboxLimits | None = None,
        *,
        run_fn: Callable[..., "subprocess.CompletedProcess[bytes]"] | None = None,
        popen_fn: Callable[..., "subprocess.Popen[bytes]"] | None = None,
    ) -> None:
        self.enabled = enabled
        self.limits = limits or SandboxLimits()
        self._run = run_fn or subprocess.run
        self._popen = popen_fn or subprocess.Popen

    @classmethod
    def from_flag(cls, flag: bool, **kw) -> "UntrustedSandbox":
        """Enabled by an explicit command-line flag or by the OJ_TEST_LAB_ENABLE_UNTRUSTED_EXEC variable."""
        return cls(enabled=bool(flag) or _truthy(os.environ.get(ENABLE_ENV_VAR)), **kw)

    # ---- readiness ----------------------------------------------------------
    def _docker_json(self, args: list[str], timeout: float = 30) -> tuple[int, str]:
        try:
            proc = self._run(["docker", *args], capture_output=True, timeout=timeout, shell=False)
        except FileNotFoundError as exc:
            raise DockerUnavailableError("docker CLI not found in PATH") from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise DockerUnavailableError(f"docker is not responding: {exc}") from exc
        return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace")

    def assess_isolation(self) -> Isolation:
        try:
            code, out = self._docker_json(["info", "--format", "{{json .}}"])
        except DockerUnavailableError as exc:
            return Isolation(False, "none", str(exc))
        if code != 0:
            return Isolation(False, "none", "docker daemon is not reachable")
        try:
            info = json.loads(out)
        except ValueError:
            return Isolation(False, "none", "cannot read `docker info` output; failing closed")
        if not isinstance(info, dict) or info.get("OSType") != "linux":
            return Isolation(False, "none", "Docker does not run Linux containers")
        default_runtime = str(info.get("DefaultRuntime", ""))
        if any(default_runtime.startswith(r) for r in SANDBOX_RUNTIMES):
            return Isolation(True, "sandboxed-runtime", f"default runtime is {default_runtime!r}")
        if str(info.get("OperatingSystem", "")).startswith("Docker Desktop"):
            return Isolation(
                True, "vm",
                "Docker Desktop runs containers inside a separate Linux VM (kernel isolated from the Windows host, "
                "but shared with other containers in that VM)",
            )
        return Isolation(
            False, "none",
            "Docker daemon shares the host kernel (no VM or sandboxing runtime such as gVisor/Kata detected); "
            "ordinary Docker is not adequate isolation for untrusted code",
        )

    def image_present(self) -> bool:
        try:
            code, _ = self._docker_json(["image", "inspect", IMAGE, "--format", "{{.Id}}"])
        except DockerUnavailableError:
            return False
        return code == 0

    def status(self) -> dict:
        """Describes whether Generate Tests can run, and if not, why. Never raises."""
        iso = self.assess_isolation()
        blocker: str | None = None
        if not self.enabled:
            blocker = (
                "Untrusted code execution is disabled. Start the server with --enable-untrusted-exec "
                f"(or set {ENABLE_ENV_VAR}=1) to opt in."
            )
        elif not iso.allowed:
            blocker = "Isolation is not adequate: " + iso.reason
        elif not self.image_present():
            blocker = f"Sandbox image is not present locally (it is never pulled automatically). Run: docker pull {IMAGE}"
        return {
            "enabled": self.enabled,
            "ready": blocker is None,
            "blocker": blocker,
            "isolation": {"allowed": iso.allowed, "level": iso.level, "reason": iso.reason},
            "image": IMAGE,
            "limits": {
                "memory": self.limits.memory, "pids": self.limits.pids, "cpus": self.limits.cpus,
                "network": "none", "user": "65534:65534", "read_only_rootfs": True,
                "run_seconds_per_test": self.limits.run_seconds, "generator_wall_seconds": self.limits.generator_wall_seconds,
            },
            "limitations": [
                "A container shares a kernel with other containers in the same Docker VM; this is not a strong boundary "
                "against a determined attacker.",
                "Intended for local, single-user use with code a human has reviewed. Never expose as a public service.",
            ],
        }

    def require_ready(self) -> dict:
        st = self.status()
        if not st["ready"]:
            raise SandboxDisabledError(st["blocker"])
        return st

    # ---- command construction ------------------------------------------------
    def build_command(self, name: str, argv: list[str]) -> list[str]:
        """No --mount, no --volume, no --env, no docker.sock: the container sees nothing from the host."""
        lim = self.limits
        args = security_args(name, lim.memory, lim.pids, lim.cpus, lim.tmpfs_size, noexec_tmp=False)
        # Docker tmpfs is noexec by default; the compiled reference must run from /tmp (it is still inside the container)
        args = [a + ",exec" if a.startswith("/tmp:rw") else a for a in args]
        return [
            "docker", "run", "-i", "--pull", "never", "--label", "ojtl-sandbox=untrusted",
            *args,
            "--ipc", "none", "--ulimit", "nofile=256:256",
            IMAGE, *argv,
        ]

    @staticmethod
    def _new_name() -> str:
        return f"ojtl-untrusted-{uuid.uuid4().hex[:12]}"

    # ---- execution -------------------------------------------------------------
    def remove_container(self, name: str) -> None:
        try:
            self._run(["docker", "rm", "-f", name], capture_output=True, timeout=30, shell=False)
        except (subprocess.SubprocessError, OSError):
            pass

    def _execute(self, cmd: list[str], stdin: bytes, timeout: float, max_stdout: int) -> ContainerResult:
        name = cmd[cmd.index("--name") + 1]
        try:
            proc = self._popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        except FileNotFoundError as exc:
            raise DockerUnavailableError("docker CLI not found in PATH") from exc
        except OSError as exc:
            raise SandboxError(f"cannot execute docker: {exc}") from exc
        out, err = bytearray(), bytearray()
        overflow = threading.Event()

        def pump(stream, buf: bytearray, cap: int, flag: threading.Event | None) -> None:
            try:
                while chunk := stream.read(65536):
                    if len(buf) < cap:
                        buf.extend(chunk[: cap - len(buf)])
                    if flag is not None and len(buf) >= cap:
                        flag.set()
            except (OSError, ValueError):
                pass

        def feed() -> None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, out, max_stdout + 1, overflow), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, err, MAX_STDERR, None), daemon=True),
            threading.Thread(target=feed, daemon=True),
        ]
        timed_out = False
        try:
            for t in threads:
                t.start()
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if overflow.is_set():
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                proc.kill()
            self.remove_container(name)  # killing the CLI does not stop the container
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            for t in threads:
                t.join(timeout=5)
        too_much = len(out) > max_stdout
        return ContainerResult(proc.poll(), bytes(out[:max_stdout]), bytes(err), timed_out, too_much)

    def _run_driver(self, driver: str, request: dict, wall: float, max_stdout: int) -> dict:
        """Run a trusted driver in a fresh container; return its decoded JSON reply or raise SandboxError."""
        cmd = self.build_command(self._new_name(), ["python3", "-I", "-B", "-c", driver])
        res = self._execute(cmd, json.dumps(request).encode("utf-8"), wall, max_stdout)
        if res.stdout_overflow:
            raise SandboxError("sandboxed program produced more output than allowed")
        if res.timed_out:
            raise SandboxError(f"sandbox wall-clock limit of {wall:.0f}s exceeded")
        if res.exit_code in (125, 126, 127):
            raise SandboxError("docker failed to start the sandbox container: " + res.stderr.decode("utf-8", "replace").strip()[:300])
        try:
            reply = json.loads(res.stdout.decode("utf-8"))
        except ValueError:
            hint = {137: " (killed: likely out of memory)", -9: " (killed)"}.get(res.exit_code or 0, "")
            raise SandboxError(f"sandbox returned no valid result (exit code {res.exit_code}){hint}") from None
        if not isinstance(reply, dict):
            raise SandboxError("sandbox returned an unexpected result")
        return reply

    def run_generator(self, source: str, seed: int, count: int) -> GeneratorOutcome:
        lim = self.limits
        reply = self._run_driver(
            GENERATOR_DRIVER,
            {"source": source, "seed": seed, "count": count, "cpu_seconds": lim.generator_cpu_seconds, "memory_bytes": 512 * 1024 * 1024},
            lim.generator_wall_seconds, lim.max_generator_stdout,
        )
        if reply.get("ok") is True and isinstance(reply.get("inputs"), list) and all(isinstance(x, str) for x in reply["inputs"]):
            return GeneratorOutcome(True, list(reply["inputs"]))
        error = reply.get("error") if isinstance(reply.get("error"), str) else "generator returned an invalid result"
        return GeneratorOutcome(False, error=error)

    def run_reference(self, source: str, inputs: list[str]) -> ReferenceOutcome:
        lim = self.limits
        wall = lim.compile_seconds + lim.run_budget_seconds + 30
        reply = self._run_driver(
            REFERENCE_DRIVER,
            {
                "source": source, "inputs": inputs, "compile_seconds": lim.compile_seconds,
                "run_seconds": lim.run_seconds, "budget_seconds": lim.run_budget_seconds,
                "max_case_output": lim.max_case_output, "run_memory_bytes": lim.run_memory_bytes,
            },
            wall, lim.max_reference_stdout,
        )
        if reply.get("compiled") is not True:
            return ReferenceOutcome(False, error=str(reply.get("error", "compilation failed"))[:4000])
        results = reply.get("results")
        if not isinstance(results, list) or not all(isinstance(r, dict) and isinstance(r.get("stdout"), str) for r in results):
            raise SandboxError("sandbox returned malformed reference results")
        return ReferenceOutcome(True, results=results, complete=reply.get("complete") is True)
