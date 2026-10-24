import json
import subprocess
from pathlib import Path

import pytest

from oj_test_lab import cli
from oj_test_lab import docker_runner as dr
from oj_test_lab.cli import main
from oj_test_lab.cpp_evaluator import CPP_REPORT_NAME, evaluate_cpp_candidate, load_expected
from oj_test_lab.models import CompareMode, CppCandidate, TestCase
from oj_test_lab.problems import get_problem
from oj_test_lab.suite import build_suite, load_suite

PROBLEM = get_problem("data-types-io")


def cp(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeDocker:
    """Records docker invocations and answers like the docker CLI would."""

    def __init__(self, compile_rc=0, run_rc=0, run_stdout=None, timeout_on_run=False, daemon_ok=True,
                 compile_stderr=b"", produce_exe=True):
        self.calls: list[list[str]] = []
        self.compile_rc, self.run_rc = compile_rc, run_rc
        self.run_stdout, self.timeout_on_run = run_stdout, timeout_on_run
        self.daemon_ok, self.compile_stderr, self.produce_exe = daemon_ok, compile_stderr, produce_exe

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        assert kw.get("shell") is False and isinstance(args, list)
        if args[:2] == ["docker", "version"]:
            return cp(0 if self.daemon_ok else 1, b"29.0.0", b"" if self.daemon_ok else b"cannot connect")
        if args[:3] == ["docker", "rm", "-f"]:
            return cp()
        if "g++" in args and "--version" in args:
            return cp(0, b"g++ (GCC) 13.3.0\nmore\n")
        if "g++" in args:
            if self.compile_rc == 0 and self.produce_exe:
                build = next(a for a in args if a.endswith("target=/build")).split("source=")[1].split(",")[0]
                (Path(build) / "main").write_bytes(b"exe")
            return cp(self.compile_rc, b"", self.compile_stderr)
        if self.timeout_on_run:
            raise subprocess.TimeoutExpired(args, kw["timeout"])
        stdout = self.run_stdout(kw["input"].decode()) if self.run_stdout else b""
        return cp(self.run_rc, stdout)

    def containers_started(self):
        return [c for c in self.calls if c[:2] == ["docker", "run"]]


def correct_stdout(text: str) -> bytes:
    return PROBLEM.reference(text).encode()


@pytest.fixture(scope="module")
def cases():
    return build_suite(PROBLEM, 28, 42)


@pytest.fixture(scope="module")
def expected(cases):
    return {c.id: PROBLEM.reference(c.input_text) for c in cases}


def candidate(tmp_path, text="int main(){}"):
    p = tmp_path / "x.cpp"
    p.write_text(text)
    return CppCandidate("cpp-x", "test", p)


# ---- command construction ----
def test_run_command_has_isolation_flags(tmp_path):
    cmd = dr.build_run_command("ojtl-run-1", tmp_path, dr.Limits())
    joined = " ".join(cmd)
    assert cmd[:3] == ["docker", "run", "-i"]
    for flag in ("--rm", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--network none", "--user 65534:65534", "--memory 128m", "--pids-limit 16", "--cpus 1"):
        assert flag in joined
    assert "--privileged" not in cmd
    assert "docker.sock" not in joined and "-p" not in cmd and "--publish" not in cmd
    assert dr.IMAGE in cmd and "@sha256:" in dr.IMAGE
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]
    assert len(mounts) == 1 and mounts[0].endswith("target=/build,readonly")
    assert any(a.startswith("/tmp:rw,nosuid,nodev,size=") and a.endswith("noexec") for a in cmd)


def test_compile_command(tmp_path):
    cmd = dr.build_compile_command("n", tmp_path / "s", tmp_path / "b", dr.Limits())
    assert cmd[-7:] == ["g++", "-std=c++17", "-O2", "-pipe", "-o", "/build/main", "/src/main.cpp"]
    assert "-std=c++17" in cmd and "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]
    assert mounts[0].endswith("target=/src,readonly") and not mounts[1].endswith("readonly")
    assert not any(f in cmd for f in ("-Wl,", "-fplugin", "-B", "-specs"))


@pytest.mark.parametrize("bad", ["a,b", "a'b", 'a"b', "a=b"])
def test_unsafe_mount_path_rejected(tmp_path, bad):
    with pytest.raises(dr.InfrastructureError):
        dr.build_run_command("n", tmp_path / bad, dr.Limits())


# ---- compile / run through the runner (mocked) ----
def test_compile_success_and_compiler_version(tmp_path):
    fake = FakeDocker()
    runner = dr.DockerCppRunner(run_fn=fake)
    with dr.TempWorkdir() as work:
        res, build = runner.compile("int main(){}", work)
        assert res.status == "OK" and (build / "main").is_file()
        assert (work / "src" / "main.cpp").read_text() == "int main(){}"
    assert runner.compiler_version() == "g++ (GCC) 13.3.0"


def test_compile_error_classified_ce():
    runner = dr.DockerCppRunner(run_fn=FakeDocker(compile_rc=1, compile_stderr=b"error: boom"))
    with dr.TempWorkdir() as work:
        res, _ = runner.compile("garbage", work)
    assert res.status == "CE" and "boom" in res.stderr


def test_compile_timeout_is_ce_and_container_removed():
    class SlowCompile(FakeDocker):
        def __call__(self, args, **kw):
            if "g++" in args and "--version" not in args:
                self.calls.append(list(args))
                raise subprocess.TimeoutExpired(args, 1)
            return super().__call__(args, **kw)

    fake = SlowCompile()
    runner = dr.DockerCppRunner(run_fn=fake)
    with dr.TempWorkdir() as work:
        res, _ = runner.compile("x", work)
    assert res.status == "CE" and res.timed_out
    assert any(c[:3] == ["docker", "rm", "-f"] for c in fake.calls)


def test_docker_error_exit_code_is_infra_not_ce():
    runner = dr.DockerCppRunner(run_fn=FakeDocker(compile_rc=125))
    with dr.TempWorkdir() as work:
        res, _ = runner.compile("x", work)
    assert res.status == "INFRA"


def test_missing_executable_is_infra():
    runner = dr.DockerCppRunner(run_fn=FakeDocker(produce_exe=False))
    with dr.TempWorkdir() as work:
        assert runner.compile("x", work)[0].status == "INFRA"


def test_runtime_error_and_signal(tmp_path):
    runner = dr.DockerCppRunner(run_fn=FakeDocker(run_rc=139))
    r = runner.run_test(tmp_path, "in")
    assert r.status == "RE" and "signal 11" in r.detail
    assert dr.DockerCppRunner(run_fn=FakeDocker(run_rc=3)).run_test(tmp_path, "in").status == "RE"


def test_timeout_is_tle_and_cleanup(tmp_path):
    fake = FakeDocker(timeout_on_run=True)
    r = dr.DockerCppRunner(run_fn=fake).run_test(tmp_path, "in")
    assert r.status == "TLE"
    name = fake.containers_started()[0][fake.containers_started()[0].index("--name") + 1]
    assert ["docker", "rm", "-f", name] in fake.calls


def test_docker_unavailable():
    with pytest.raises(dr.DockerUnavailableError):
        dr.DockerCppRunner(run_fn=FakeDocker(daemon_ok=False)).check_available()

    def no_docker(*a, **k):
        raise FileNotFoundError("docker")

    with pytest.raises(dr.DockerUnavailableError):
        dr.DockerCppRunner(run_fn=no_docker).check_available()


def test_docker_error_is_infra_not_wa(tmp_path):
    r = dr.DockerCppRunner(run_fn=FakeDocker(run_rc=125)).run_test(tmp_path, "in")
    assert r.status == "INFRA"


def test_output_is_clipped(tmp_path):
    big = lambda _: b"x" * (dr.MAX_CAPTURE * 2)  # noqa: E731
    r = dr.DockerCppRunner(run_fn=FakeDocker(run_stdout=big)).run_test(tmp_path, "in")
    assert len(r.stdout) < dr.MAX_CAPTURE + 50 and r.stdout.endswith("[truncated]")


# ---- evaluator classification ----
def test_ac_and_wa_classification(tmp_path, cases, expected):
    runner = dr.DockerCppRunner(run_fn=FakeDocker(run_stdout=correct_stdout))
    res = evaluate_cpp_candidate(runner, candidate(tmp_path), cases, expected, CompareMode.EXACT)
    assert res.failed_ids == [] and len(res.ids("AC")) == 28

    def wrong_y(text: str) -> bytes:
        lines = PROBLEM.reference(text).split("\n")
        lines[1] = str(int(lines[1]) % 2**31)
        return "\n".join(lines).encode()

    runner = dr.DockerCppRunner(run_fn=FakeDocker(run_stdout=wrong_y))
    res = evaluate_cpp_candidate(runner, candidate(tmp_path), cases, expected, CompareMode.EXACT)
    assert res.failed_ids and set(res.ids("WA")) == set(res.failed_ids)


def test_exact_comparison_strict_about_trailing_newline(tmp_path, cases, expected):
    runner = dr.DockerCppRunner(run_fn=FakeDocker(run_stdout=lambda t: correct_stdout(t).rstrip(b"\n")))
    exact = evaluate_cpp_candidate(runner, candidate(tmp_path), cases, expected, CompareMode.EXACT)
    trim = evaluate_cpp_candidate(runner, candidate(tmp_path), cases, expected, CompareMode.TRIM)
    assert len(exact.ids("WA")) == 28 and len(trim.ids("AC")) == 28


def test_compile_failure_marks_all_ce_and_no_runs(tmp_path, cases, expected):
    fake = FakeDocker(compile_rc=1)
    res = evaluate_cpp_candidate(dr.DockerCppRunner(run_fn=fake), candidate(tmp_path), cases, expected, CompareMode.EXACT)
    assert len(res.ids("CE")) == 28
    assert all("-i" not in c for c in fake.containers_started())  # no test container started


def test_compiled_once_for_all_tests(tmp_path, cases, expected):
    fake = FakeDocker(run_stdout=correct_stdout)
    evaluate_cpp_candidate(dr.DockerCppRunner(run_fn=fake), candidate(tmp_path), cases, expected, CompareMode.EXACT)
    assert sum("g++" in c for c in fake.calls) == 1
    assert sum(c[:3] == ["docker", "run", "-i"] for c in fake.calls) == 28


def test_workdir_cleaned_up(tmp_path, cases, expected):
    seen = []
    fake = FakeDocker(run_stdout=correct_stdout)
    orig = dr.TempWorkdir.__enter__

    def spy(self):
        p = orig(self)
        seen.append(p)
        return p

    dr.TempWorkdir.__enter__ = spy
    try:
        evaluate_cpp_candidate(dr.DockerCppRunner(run_fn=fake), candidate(tmp_path), cases[:2], expected, CompareMode.EXACT)
    finally:
        dr.TempWorkdir.__enter__ = orig
    assert seen and not seen[0].exists()


def test_load_expected_rejects_missing(tmp_path, cases):
    with pytest.raises(FileNotFoundError):
        load_expected(tmp_path, cases[:1])


# ---- CLI ----
@pytest.fixture
def suite_dir(tmp_path):
    out = tmp_path / "suite"
    assert main(["generate", "--problem", "data-types-io", "--output-dir", str(out)]) == 0
    return out


def correct_or_wrong(text_in: str, source: str) -> bytes:
    if "int y" in source:
        lines = PROBLEM.reference(text_in).split("\n")
        lines[1] = str(((int(lines[1]) + 2**31) % 2**32) - 2**31)
        return "\n".join(lines).encode()
    return correct_stdout(text_in)


def test_cli_evaluate_cpp_report(suite_dir, monkeypatch, capsys):
    class Fake(FakeDocker):
        def __call__(self, args, **kw):
            if "g++" in args and "--version" not in args:
                self.src = next(a for a in args if a.endswith("target=/src,readonly")).split("source=")[1].split(",")[0]
                self.last_source = (Path(self.src) / "main.cpp").read_text()
            return super().__call__(args, **kw)

    fake = Fake()
    fake.run_stdout = lambda t: correct_or_wrong(t, fake.last_source)
    monkeypatch.setattr(cli, "_make_runner", lambda limits: dr.DockerCppRunner(limits, run_fn=fake))
    before = (suite_dir / "quality-report.json").read_bytes()
    assert main(["evaluate-cpp", "--problem", "data-types-io", "--input-dir", str(suite_dir)]) == 0
    assert (suite_dir / "quality-report.json").read_bytes() == before  # simulated report untouched
    rep = json.loads((suite_dir / CPP_REPORT_NAME).read_text("utf-8"))
    assert rep["execution"] == "real" and rep["simulated"] is False and rep["cpp_standard"] == "c++17"
    assert rep["compiler"] == "g++ (GCC) 13.3.0" and rep["test_count"] == 28
    by = {c["id"]: c for c in rep["candidates"]}
    assert by["cpp-correct-long-long"]["failed_tests"] == []
    assert by["cpp-correct-long-long"]["counts"]["AC"] == 28
    assert by["cpp-wrong-int-y"]["failed_tests"] and len(by["cpp-wrong-int-y"]["tests"]) == 28
    assert all("time_ms" in t for t in by["cpp-wrong-int-y"]["tests"])
    out = capsys.readouterr().out
    assert "[real]" in out and "123456789012" not in out


def test_cli_select_candidate_and_unknown(suite_dir, monkeypatch, capsys):
    fake = FakeDocker(run_stdout=correct_stdout)
    monkeypatch.setattr(cli, "_make_runner", lambda limits: dr.DockerCppRunner(limits, run_fn=fake))
    args = ["evaluate-cpp", "--problem", "data-types-io", "--input-dir", str(suite_dir)]
    assert main(args + ["--candidate", "cpp-correct-long-long"]) == 0
    rep = json.loads((suite_dir / CPP_REPORT_NAME).read_text("utf-8"))
    assert [c["id"] for c in rep["candidates"]] == ["cpp-correct-long-long"]
    assert main(args + ["--candidate", "../../evil.cpp"]) == 1
    assert "unknown trusted candidate" in capsys.readouterr().err


def test_cli_docker_unavailable_is_infra_error(suite_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_make_runner", lambda limits: dr.DockerCppRunner(limits, run_fn=FakeDocker(daemon_ok=False)))
    assert main(["evaluate-cpp", "--problem", "data-types-io", "--input-dir", str(suite_dir)]) == 1
    err = capsys.readouterr().err
    assert "infrastructure failure" in err and "Wrong" not in err
    assert not (suite_dir / CPP_REPORT_NAME).exists()


def test_cli_errors(tmp_path, suite_dir):
    assert main(["evaluate-cpp", "--problem", "data-types-io", "--input-dir", str(tmp_path / "none")]) == 1
    assert main(["evaluate-cpp", "--problem", "data-types-io", "--input-dir", str(suite_dir), "--time-limit", "0"]) == 1
    (suite_dir / "05.out").unlink()
    assert main(["evaluate-cpp", "--problem", "data-types-io", "--input-dir", str(suite_dir)]) == 1


def test_regenerate_removes_stale_cpp_report_only(suite_dir):
    (suite_dir / CPP_REPORT_NAME).write_text("{}")
    (suite_dir / "notes.txt").write_text("keep")
    assert main(["generate", "--problem", "data-types-io", "--output-dir", str(suite_dir)]) == 0
    assert not (suite_dir / CPP_REPORT_NAME).exists() and (suite_dir / "notes.txt").exists()


# ---- real Docker integration (skipped when Docker or the image is unavailable) ----
def _docker_ready() -> bool:
    try:
        r = dr.DockerCppRunner()
        r.check_available()
        r.compiler_version()
        return True
    except Exception:
        return False


docker = pytest.mark.skipif(not _docker_ready(), reason="Docker or pinned gcc image not available")


@docker
def test_real_correct_vs_incorrect(suite_dir):
    cases, _ = load_suite(suite_dir)
    expected = load_expected(suite_dir, cases)
    runner = dr.DockerCppRunner()
    by = {c.id: c for c in PROBLEM.cpp_candidates}
    good = evaluate_cpp_candidate(runner, by["cpp-correct-long-long"], cases, expected, CompareMode.EXACT)
    bad = evaluate_cpp_candidate(runner, by["cpp-wrong-int-y"], cases, expected, CompareMode.EXACT)
    assert len(good.ids("AC")) == len(cases) and not good.infra_errors
    assert bad.failed_ids and not bad.infra_errors
    outside = {c.id for c in cases if not -(2**31) <= int(c.input_text.split("\n")[1]) < 2**31}
    assert set(bad.failed_ids) <= outside  # every failure has Y outside int32


@docker
def test_real_compile_error_runtime_error_timeout(tmp_path):
    runner = dr.DockerCppRunner(dr.Limits(run_timeout=2))
    with dr.TempWorkdir() as w:
        res, _ = runner.compile("this is not C++", w)
        assert res.status == "CE" and res.stderr
    with dr.TempWorkdir() as w:
        res, build = runner.compile("int main(){ return 3; }", w)
        assert res.status == "OK" and runner.run_test(build, "").status == "RE"
    with dr.TempWorkdir() as w:
        res, build = runner.compile("int main(){ for(;;){} }", w)
        assert res.status == "OK" and runner.run_test(build, "").status == "TLE"
    with dr.TempWorkdir() as w:  # the build mount is read-only at run time
        src = '#include <fstream>\nint main(){ std::ofstream f("/build/x"); return f ? 7 : 0; }'
        res, build = runner.compile(src, w)
        assert runner.run_test(build, "").status == "OK"  # write failed, so the program exited 0
        assert not (build / "x").exists()
    with dr.TempWorkdir() as w:  # no network inside the container
        src = ('#include <sys/socket.h>\n#include <netinet/in.h>\n#include <arpa/inet.h>\n'
               'int main(){ int s=socket(AF_INET,SOCK_STREAM,0); sockaddr_in a{}; a.sin_family=AF_INET; a.sin_port=htons(80);'
               ' inet_pton(AF_INET,"1.1.1.1",&a.sin_addr); return connect(s,(sockaddr*)&a,sizeof a)==0 ? 7 : 0; }')
        res, build = runner.compile(src, w)
        assert res.status == "OK" and runner.run_test(build, "").status == "OK"
