"""Offline tests. A fake prized.AsyncSandbox runs every command with the local bash and keeps files
in a temp dir that stands in for /home/prized, so the package's own shell helpers run for real."""

import os
import shutil
import subprocess

import prized
import pytest
from inspect_ai.util import (
    OutputLimitExceededError,
    SandboxUnavailableError,
    SandboxUserUnsupportedError,
)
from prized import CommandTimeoutError, ExecResult, PrizedError

import inspect_prized._environment as env_mod
from inspect_prized import PrizedSandboxConfig, PrizedSandboxEnvironment


class FakeFiles:
    def __init__(self, sb):
        self.sb = sb

    async def write(self, path, data):
        local = self.sb.local(path)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)

    async def read_bytes(self, path):
        with open(self.sb.local(path), "rb") as f:
            return f.read()

    async def remove(self, path, recursive=False):
        try:
            os.remove(self.sb.local(path))
        except FileNotFoundError:
            pass


def _result(reason=None, **fields):
    """An ExecResult; ``reason`` is prized 0.1.1's truncated_reason, set by hand on 0.1.0 (no such field)."""
    if reason is None:
        return ExecResult(**fields)
    try:
        return ExecResult(**fields, truncated_reason=reason)
    except TypeError:  # prized 0.1.0
        result = ExecResult(**fields)
        object.__setattr__(result, "truncated_reason", reason)
        return result


def _output_read_error(exit_code, reason):
    """prized 0.1.1's OutputReadError (the command ran, its output could not be read back); by hand on 0.1.0."""
    out, err = "/home/prized/.prized/exec/x/stdout", "/home/prized/.prized/exec/x/stderr"
    message = f"The command ran (exit {exit_code}), but its output could not be read back."
    cls = getattr(prized, "OutputReadError", None)
    if cls is not None:
        return cls(message, exit_code=exit_code, stdout_path=out, stderr_path=err, reason=reason)
    error = PrizedError("output_read_failed", message, reason=reason)
    error.exit_code, error.stdout_path, error.stderr_path = exit_code, out, err
    return error


class FakeSandbox:
    made: list = []
    reason = None  # truncated_reason of a cut result (prized 0.1.1)
    fork_error = None  # raised by fork()
    error = None  # raised by the next exec
    cut = 10**9  # what one blocking call keeps per stream (4 MiB on Prized)
    full_cut = 10**9  # what one with full_output=True keeps per stream (100 MiB on Prized)
    polled_cut = 10**9  # what a polled one (no timeout, or over 240 s) keeps (64 Mi characters on Prized)

    def __init__(self, root, options):
        self.id = f"sbx_fake{len(FakeSandbox.made)}"
        self.root = root
        self.options = options
        self.labels = options.get("labels", {})
        self.files = FakeFiles(self)
        self.deleted = self.paused = self.closed = False
        self.calls: list = []
        os.makedirs(os.path.join(root, "work"), exist_ok=True)
        FakeSandbox.made.append(self)

    def local(self, path):
        return path.replace("/home/prized", self.root)

    async def exec(self, cmd, cwd=None, env=None, timeout=None, **kw):
        self.calls.append((cmd, cwd, env, timeout, kw))
        if self.deleted:
            raise PrizedError("sandbox_deleted", "gone", status=410)
        if FakeSandbox.error is not None:
            raise FakeSandbox.error
        argv = [self.local(a) for a in cmd]
        sandbox_env = {k: self.local(v) for k, v in self.options.get("env_vars", {}).items()}
        full_env = {"PATH": os.environ["PATH"], "HOME": self.root, **sandbox_env, **(env or {})}
        try:
            p = subprocess.run(
                argv,
                cwd=self.local(cwd or "/home/prized/work"),
                env=full_env,
                capture_output=True,
                timeout=timeout or None,
            )
        except subprocess.TimeoutExpired:
            res = ExecResult(stdout="", stderr="", exit_code=-1, truncated=False, timed_out=True, duration_ms=0)
            raise CommandTimeoutError(res, timeout, self.id) from None
        out = p.stdout.decode(errors="replace").replace(self.root, "/home/prized")
        err = p.stderr.decode(errors="replace").replace(self.root, "/home/prized")
        streamed = "on_stdout" in kw or "on_stderr" in kw
        if streamed or not timeout or timeout > 240:
            cut = FakeSandbox.polled_cut
        else:
            cut = FakeSandbox.full_cut if kw.get("full_output") else FakeSandbox.cut
        return _result(
            FakeSandbox.reason if len(out) > cut else None,
            stdout=out[:cut],
            stderr=err,
            exit_code=p.returncode,
            truncated=len(out) > cut,
            timed_out=False,
            duration_ms=1,
        )

    async def fork(self, count=1, labels=None):
        if FakeSandbox.fork_error is not None:
            raise FakeSandbox.fork_error
        copies = []
        for _ in range(count):
            root = f"{self.root}-fork{len(FakeSandbox.made)}"
            shutil.copytree(self.root, root, symlinks=True)
            copy = FakeSandbox(root, {**self.options, "labels": dict(labels or self.labels)})
            copy.forked_from = self.id
            copies.append(copy)
        return copies

    async def delete(self, wait=False):
        self.deleted = True
        self.delete_waited = wait

    async def pause(self, wait=True):
        self.paused = True

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake(monkeypatch, tmp_path):
    FakeSandbox.made.clear()
    monkeypatch.setattr(FakeSandbox, "reason", None)
    monkeypatch.setattr(FakeSandbox, "error", None)
    monkeypatch.setattr(FakeSandbox, "fork_error", None)
    PrizedSandboxEnvironment._live.clear()
    n = {"i": 0}

    async def create(**options):
        n["i"] += 1
        return FakeSandbox(str(tmp_path / f"home{n['i']}"), options)

    async def connect(sandbox_id, **_):
        for sb in FakeSandbox.made:
            if sb.id == sandbox_id and not sb.deleted:
                return sb
        raise PrizedError("not_found", "nope", status=404)

    monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))
    monkeypatch.setattr(env_mod.AsyncSandbox, "connect", staticmethod(connect))


async def _env(**cfg):
    config = PrizedSandboxConfig(**cfg)
    envs = await PrizedSandboxEnvironment.sample_init("unit task", config, {})
    return envs["default"].as_type(PrizedSandboxEnvironment), config, envs


async def test_create_options_and_labels():
    env, _, _ = await _env(template="prized-agent-v1", vcpu=2, memory_gb=4, env={"A": "1"}, labels={"team": "x"})
    o = FakeSandbox.made[0].options
    assert o["template"] == "prized-agent-v1" and o["vcpu"] == 2 and o["memory_gb"] == 4
    assert o["env_vars"]["A"] == "1" and o["auto_pause_min"] == 0 and o["max_runtime_min"] == 60
    assert o["env_vars"]["PATH"].startswith("/home/prized/.local/bin:")
    assert o["labels"] == {"team": "x", "created-by": "inspect-prized", "inspect-task": "unit-task"}
    assert "disk_gb" not in o


async def test_exec_wrapper_keeps_signal_exit_code_and_stdin():
    env, _, _ = await _env()
    r = await env.exec(["sh", "-c", "kill -TERM $$"])
    assert r.returncode == 143 and not r.success
    r = await env.exec(["cat"], input="a 'b' $HOME\n")
    assert r.stdout == "a 'b' $HOME\n"
    r = await env.exec(["cat"], input="")
    assert r.stdout == "" and r.success


async def test_large_argv_travels_as_a_file():
    env, _, _ = await _env()
    chunk = "y" * 70_000
    r = await env.exec(["printf", "%s", chunk, chunk, chunk, chunk])
    cmd = FakeSandbox.made[0].calls[-1][0]
    assert r.stdout[:10] == "y" * 10
    assert chunk not in cmd and cmd[-1].startswith("/home/prized/.inspect-prized/argv-")


async def test_output_past_one_call_cap_comes_back_whole(monkeypatch):
    monkeypatch.setattr(FakeSandbox, "cut", 64)
    env, _, _ = await _env()
    r = await env.exec(["bash", "-c", "printf '%0100d' 0"], timeout=10)
    assert len(r.stdout) == 100 and r.success
    assert FakeSandbox.made[0].calls[-1][4] == {"full_output": True}


class _Limits:
    MAX_EXEC_OUTPUT_SIZE = 50
    MAX_READ_FILE_SIZE = 100 * 1024 * 1024
    MAX_READ_FILE_SIZE_STR = "100 MiB"


async def test_output_over_inspect_limit_raises(monkeypatch):
    monkeypatch.setattr(env_mod, "SandboxEnvironmentLimits", _Limits)
    env, _, _ = await _env()
    with pytest.raises(OutputLimitExceededError) as e:
        await env.exec(["bash", "-c", "printf '%0100d' 0"], timeout=10)
    assert "50 bytes" in str(e.value) and e.value.truncated_output == "0" * 50
    with pytest.raises(OutputLimitExceededError):
        await env.exec(["bash", "-c", "printf '%0100d' 0 >&2"], timeout=10)
    assert (await env.exec(["bash", "-c", "printf '%050d' 0"], timeout=10)).stdout == "0" * 50


async def test_output_past_what_the_sdk_keeps_raises(monkeypatch):
    monkeypatch.setattr(FakeSandbox, "full_cut", 64)
    monkeypatch.setattr(FakeSandbox, "polled_cut", 64)
    env, _, _ = await _env()
    with pytest.raises(OutputLimitExceededError) as e:
        await env.exec(["bash", "-c", "printf '%0100d' 0"], timeout=10)
    assert "10 MiB" in str(e.value)
    monkeypatch.setattr(_Limits, "MAX_EXEC_OUTPUT_SIZE", 10**9)
    monkeypatch.setattr(env_mod, "SandboxEnvironmentLimits", _Limits)
    with pytest.raises(OutputLimitExceededError) as e:
        await env.exec(["bash", "-c", "printf '%0100d' 0"], timeout=10)
    assert "100 MiB" in str(e.value)
    for timeout in (None, 300):  # polled by the SDK, which keeps 64 Mi characters
        with pytest.raises(OutputLimitExceededError) as e:
            await env.exec(["bash", "-c", "printf '%0100d' 0"], timeout=timeout)
        assert "64 MiB" in str(e.value)


async def test_output_a_full_disk_cut_is_returned_with_a_note(monkeypatch):
    # prized 0.1.1: truncated_reason "write_failed" is a full disk, not an output limit.
    monkeypatch.setattr(FakeSandbox, "full_cut", 64)
    monkeypatch.setattr(FakeSandbox, "reason", "write_failed")
    env, _, _ = await _env()
    r = await env.exec(["bash", "-c", "printf '%0100d' 0; echo oops >&2; exit 3"], timeout=10)
    assert r.stdout == "0" * 64 and r.returncode == 3 and not r.success
    assert r.stderr.startswith("oops\n") and "its disk is full" in r.stderr
    # Over Inspect's limit it still raises.
    monkeypatch.setattr(env_mod, "SandboxEnvironmentLimits", _Limits)
    with pytest.raises(OutputLimitExceededError):
        await env.exec(["bash", "-c", "printf '%0100d' 0"], timeout=10)
    # A stream cut at the SDK's cap still raises, as before.
    monkeypatch.setattr(_Limits, "MAX_EXEC_OUTPUT_SIZE", 10**9)
    monkeypatch.setattr(FakeSandbox, "reason", "output_limit")
    with pytest.raises(OutputLimitExceededError):
        await env.exec(["bash", "-c", "printf '%0100d' 0"], timeout=10)


@pytest.mark.parametrize("reason", ["not_found", "network_error"])
async def test_output_that_could_not_be_read_back_says_the_command_ran(reason):
    env, _, _ = await _env()
    FakeSandbox.error = _output_read_error(3, reason)
    with pytest.raises(SandboxUnavailableError) as e:
        await env.exec(["make"], timeout=10)  # a blocking exec: what can raise OutputReadError
    message = str(e.value)
    assert f"the command ran (exit code 3), but its output could not be read back ({reason})" in message
    assert "/home/prized/.prized/exec/x/stdout and /home/prized/.prized/exec/x/stderr" in message
    assert "is not available" not in message
    assert len([c for c in FakeSandbox.made[0].calls if "make" in c[0]]) == 1


async def test_permission_error_and_timeout():
    env, _, _ = await _env()
    with pytest.raises(PermissionError):
        await env.exec(["/etc/passwd"])
    with pytest.raises(TimeoutError):
        await env.exec(["sleep", "5"], timeout=1)


async def test_users():
    env, _, _ = await _env()
    with pytest.raises(SandboxUserUnsupportedError):
        await env.exec(["whoami"], user="root")
    assert (await env.exec(["true"], user="prized")).success
    await env.write_file("u.txt", "x", user="prized")
    assert await env.read_file("u.txt", user="prized") == "x"
    with pytest.raises(SandboxUserUnsupportedError):
        await env.read_file("u.txt", user="root")
    with pytest.raises(SandboxUserUnsupportedError):
        await env.write_file("u.txt", "y", user="root")


async def test_cwd_relative_and_default():
    env, _, _ = await _env()
    await env.exec(["mkdir", "sub"])
    await env.write_file("sub/f.txt", "x")
    assert (await env.exec(["ls"], cwd="sub")).stdout == "f.txt\n"
    assert FakeSandbox.made[0].calls[-1][1] == "/home/prized/work/sub"


async def test_file_round_trip_and_errors(tmp_path):
    env, _, _ = await _env()
    await env.write_file("a/b/c.bin", b"\x00\xff")
    assert await env.read_file("a/b/c.bin", text=False) == b"\x00\xff"
    await env.write_file("t.txt", "one")
    await env.write_file("t.txt", "two")
    assert await env.read_file("t.txt") == "two"
    with pytest.raises(FileNotFoundError, match="nope"):
        await env.read_file("nope")
    with pytest.raises(IsADirectoryError):
        await env.read_file("a")
    with pytest.raises(IsADirectoryError):
        await env.write_file("a", "x")
    await env.exec(["chmod", "-w", "t.txt"])
    with pytest.raises(PermissionError, match="t.txt"):
        await env.write_file("t.txt", "three")
    await env.exec(["chmod", "-r", "t.txt"])
    with pytest.raises(PermissionError, match="t.txt"):
        await env.read_file("t.txt")
    leftovers = os.listdir(os.path.join(FakeSandbox.made[0].root, ".inspect-prized"))
    assert leftovers == []


async def test_read_limit():
    from inspect_ai.util._sandbox.limits import override_max_read_file_size

    env, _, _ = await _env()
    await env.write_file("big", "a" * 2048)
    with override_max_read_file_size(1024):
        with pytest.raises(OutputLimitExceededError):
            await env.read_file("big")


async def test_cleanup_deletes_or_pauses_and_is_per_task():
    env, cfg, envs = await _env()
    other = await PrizedSandboxEnvironment.sample_init("other task", cfg, {})
    await PrizedSandboxEnvironment.sample_cleanup("unit task", cfg, envs, False)
    assert FakeSandbox.made[0].deleted and FakeSandbox.made[0].closed
    assert FakeSandbox.made[0].delete_waited  # the slot is free before the next sample starts
    assert PrizedSandboxEnvironment._live["unit task"] == set()
    await PrizedSandboxEnvironment.task_cleanup("unit task", cfg, True)
    assert not FakeSandbox.made[1].deleted
    await PrizedSandboxEnvironment.task_cleanup("other task", cfg, True)  # sample cleanup never ran there
    assert FakeSandbox.made[1].deleted and PrizedSandboxEnvironment._live["other task"] == set()
    kept_cfg = PrizedSandboxConfig(keep=True)
    kept = await PrizedSandboxEnvironment.sample_init("k", kept_cfg, {})
    await PrizedSandboxEnvironment.sample_cleanup("k", kept_cfg, kept, True)
    assert FakeSandbox.made[2].paused and not FakeSandbox.made[2].deleted
    assert other["default"]


async def test_deleted_sandbox_raises_unavailable():
    env, _, _ = await _env()
    FakeSandbox.made[0].deleted = True
    with pytest.raises(SandboxUnavailableError):
        await env.exec(["true"])
    with pytest.raises(SandboxUnavailableError):
        await env.write_file("x.txt", "x")
    with pytest.raises(SandboxUnavailableError):
        await env.read_file("x.txt")


def test_config_hash_and_deserialize():
    a = PrizedSandboxConfig(env={"A": "1"}, labels={"x": "y"})
    assert hash(a) == hash(PrizedSandboxConfig(env={"A": "1"}, labels={"x": "y"}))
    b = PrizedSandboxConfig(env={"A": "1", "B": "2"})
    c = PrizedSandboxConfig(env={"B": "2", "A": "1"})
    assert b == c and hash(b) == hash(c) and {b: 1}[c] == 1
    assert PrizedSandboxEnvironment.config_deserialize({"vcpu": 2}) == PrizedSandboxConfig(vcpu=2)
    assert env_mod._label_value("a b/c" * 30).startswith("a-b-c") and len(env_mod._label_value("x" * 99)) == 63


async def test_task_cleanup_at_shutdown_deletes_by_the_eval_label(monkeypatch):
    # In an eval Inspect calls task_init("startup") and task_cleanup("shutdown"), never with the
    # task name the samples ran under (inspect_ai/_eval/run.py).
    swept = []

    async def delete_by_labels(labels, **_):
        swept.append(labels)
        hit = [sb for sb in FakeSandbox.made if all(sb.labels.get(k) == v for k, v in labels.items())]
        for sb in hit:
            sb.deleted = True
        return [sb.id for sb in hit]

    monkeypatch.setattr(env_mod.AsyncSandbox, "delete_by_labels", staticmethod(delete_by_labels))
    token = env_mod._run.set(None)
    try:
        cfg = PrizedSandboxConfig()
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        run = env_mod._run.get()
        await PrizedSandboxEnvironment.sample_init("my task", cfg, {})  # its sample cleanup never runs
        kept_cfg = PrizedSandboxConfig(keep=True)
        await PrizedSandboxEnvironment.sample_init("my task", kept_cfg, {})
        assert FakeSandbox.made[0].labels["inspect-run"] == run
        assert "inspect-run" not in FakeSandbox.made[1].labels  # kept sandboxes outlive the eval
        await PrizedSandboxEnvironment.task_cleanup("shutdown", cfg, True)
        assert swept == [{"inspect-run": run}]
        assert FakeSandbox.made[0].deleted and not FakeSandbox.made[1].deleted
        assert PrizedSandboxEnvironment._live["my task"] == {FakeSandbox.made[1].id}
        await PrizedSandboxEnvironment.task_cleanup("shutdown", cfg, False)  # --no-sandbox-cleanup
        assert len(swept) == 1
    finally:
        env_mod._run.reset(token)


async def test_python_runs_python3():
    env, _, _ = await _env()
    link = os.path.join(FakeSandbox.made[0].root, ".local", "bin", "python")
    assert os.path.realpath(link) == os.path.realpath("/usr/bin/python3")
    await _env(python_alias=False, env={"PATH": "/usr/bin:/bin"})
    assert not os.path.exists(os.path.join(FakeSandbox.made[1].root, ".local", "bin", "python"))
    assert FakeSandbox.made[1].options["env_vars"] == {"PATH": "/usr/bin:/bin"}


async def test_setup_runs_once_and_samples_fork_the_paused_template():
    token = env_mod._run.set(None)
    try:
        cfg = PrizedSandboxConfig(setup=["echo installed > ~/marker", "touch ~/second"], labels={"t": "1"})
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        template = FakeSandbox.made[0]
        assert template.paused and template.labels["inspect-role"] == "template"
        assert [c[0][-1] for c in template.calls] == [env_mod._ALIAS, *cfg.setup]
        a = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        b = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        assert len(FakeSandbox.made) == 3 and template.calls[-1][0][-1] == "touch ~/second"  # setup ran once
        assert a.sandbox.forked_from == template.id and a.sandbox.labels["t"] == "1"
        assert a.sandbox.labels["inspect-task"] == "t" and "inspect-role" not in a.sandbox.labels
        assert (await a.exec(["cat", "/home/prized/marker"])).stdout == "installed\n"
        assert (await b.exec(["python", "-c", "print(6 * 7)"])).stdout == "42\n"
        # A fork that does not fit falls back to a fresh sandbox with the setup run in it.
        FakeSandbox.fork_error = PrizedError("fork_capacity", "full", status=409)
        c = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        assert not hasattr(c.sandbox, "forked_from") and (await c.exec(["cat", "/home/prized/marker"])).success
        await PrizedSandboxEnvironment.task_cleanup("shutdown", cfg, True)
        assert template.deleted and template.closed and env_mod._templates.get() == {}
    finally:
        env_mod._run.reset(token)


async def test_setup_failure_deletes_the_sandbox():
    cfg = PrizedSandboxConfig(setup=["exit 3"])
    with pytest.raises(RuntimeError, match="setup command failed"):
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
    assert all(sb.deleted or sb.closed for sb in FakeSandbox.made)


async def test_fork_copies_go_with_the_sample():
    env, cfg, envs = await _env()
    await env.write_file("state.txt", "step 1")
    [copy] = await env.fork()
    assert await copy.read_file("state.txt") == "step 1"
    await copy.write_file("state.txt", "branch")
    assert await env.read_file("state.txt") == "step 1"
    await PrizedSandboxEnvironment.sample_cleanup("unit task", cfg, envs, False)
    assert env.sandbox.deleted and copy.sandbox.deleted
