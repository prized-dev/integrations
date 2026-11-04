"""Offline tests. A fake prized.AsyncSandbox runs every command with the local bash and keeps files
in a temp dir that stands in for /home/prized, so the package's own shell helpers run for real."""

import os
import shutil
import subprocess
import types

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
    # Copies a parent's machine holds beside it (409 fork_capacity past that): None for no limit, or
    # "shape" for the API's 31, 15, 7 and 3 copies of a 1, 2, 4 and 8 vCPU sandbox.
    room = None
    room_refusals: list = []
    error = None  # raised by the next exec
    cut = 10**9  # what one blocking call keeps per stream (4 MiB on Prized)
    full_cut = 10**9  # what one with full_output=True keeps per stream (100 MiB on Prized)
    polled_cut = 10**9  # what a polled one (no timeout, or over 240 s) keeps (64 Mi characters on Prized)

    def __init__(self, root, options):
        self.id = f"sbx_fake{len(FakeSandbox.made)}"
        self.root = root
        self.options = options
        self.labels = options.get("labels", {})
        # As the create answers: the shape asked for, or the template's default (4 vCPU, 8 GB).
        memory_mb = round(options.get("memory_gb", 8) * 1024)
        self.resources = types.SimpleNamespace(vcpu=options.get("vcpu", 4), memory_mb=memory_mb)
        self.files = FakeFiles(self)
        self.deleted = self.paused = self.closed = self.errored = False
        self.refreshes = 0
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
        if FakeSandbox.room is not None:
            vcpu = self.resources.vcpu
            room = (32 - vcpu) // vcpu if FakeSandbox.room == "shape" else FakeSandbox.room
            alive = [sb for sb in FakeSandbox.made if getattr(sb, "forked_from", None) == self.id and not sb.deleted]
            if len(alive) + count > room:
                FakeSandbox.room_refusals.append(self.id)
                raise PrizedError("fork_capacity", "no room on the parent's machine", status=409, sandbox_id=self.id)
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

    @property
    def state(self):
        return "deleted" if self.deleted else "error" if self.errored else "paused" if self.paused else "running"

    async def refresh(self):
        self.refreshes += 1
        return self

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake(monkeypatch, tmp_path):
    FakeSandbox.made.clear()
    monkeypatch.setattr(FakeSandbox, "reason", None)
    monkeypatch.setattr(FakeSandbox, "error", None)
    monkeypatch.setattr(FakeSandbox, "fork_error", None)
    monkeypatch.setattr(FakeSandbox, "room", None)
    monkeypatch.setattr(FakeSandbox, "room_refusals", [])
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
    token = env_mod._fallback.set(None)
    try:
        cfg = PrizedSandboxConfig()
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        run = env_mod._fallback.get().run_id
        await PrizedSandboxEnvironment.sample_init("my task", cfg, {})  # its sample cleanup never runs
        kept_cfg = PrizedSandboxConfig(keep=True)
        await PrizedSandboxEnvironment.sample_init("my task", kept_cfg, {})
        assert FakeSandbox.made[0].labels["inspect-run"] == run
        assert "inspect-run" not in FakeSandbox.made[1].labels  # kept sandboxes outlive the eval
        await PrizedSandboxEnvironment.task_cleanup("shutdown", cfg, True)
        assert swept == [{"inspect-run": run}]
        assert FakeSandbox.made[0].deleted and not FakeSandbox.made[1].deleted
        assert PrizedSandboxEnvironment._live["my task"] == {FakeSandbox.made[1].id}
        # The next eval's task_init starts a fresh state (a new run label).
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        assert env_mod._fallback.get().run_id != run
    finally:
        env_mod._fallback.reset(token)


async def test_no_sandbox_cleanup_keeps_samples(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    swept = _sweeper(monkeypatch)
    with sandbox_lifecycle_scope():
        cfg = PrizedSandboxConfig()
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        await PrizedSandboxEnvironment.task_cleanup("shutdown", cfg, False)  # --no-sandbox-cleanup
        assert swept == [] and not FakeSandbox.made[0].deleted


async def test_python_runs_python3():
    env, _, _ = await _env()
    link = os.path.join(FakeSandbox.made[0].root, ".local", "bin", "python")
    assert os.path.realpath(link) == os.path.realpath("/usr/bin/python3")
    await _env(python_alias=False, env={"PATH": "/usr/bin:/bin"})
    assert not os.path.exists(os.path.join(FakeSandbox.made[1].root, ".local", "bin", "python"))
    assert FakeSandbox.made[1].options["env_vars"] == {"PATH": "/usr/bin:/bin"}


async def test_setup_runs_once_and_samples_fork_the_paused_template(monkeypatch):
    _sweeper(monkeypatch)
    token = env_mod._fallback.set(None)
    try:
        cfg = PrizedSandboxConfig(setup=["echo installed > ~/marker", "touch ~/second"], labels={"t": "1"})
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        assert FakeSandbox.made == []  # no template until a second sample needs one
        first = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        assert [c[0][-1] for c in first.sandbox.calls] == [env_mod._ALIAS, *cfg.setup]  # its own setup
        a = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        template = FakeSandbox.made[1]
        assert template.paused and template.labels["inspect-role"] == "template"
        assert [c[0][-1] for c in template.calls] == [env_mod._ALIAS, *cfg.setup]
        b = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        assert len(FakeSandbox.made) == 4 and template.calls[-1][0][-1] == "touch ~/second"  # setup ran once more
        assert a.sandbox.forked_from == template.id and a.sandbox.labels["t"] == "1"
        assert a.sandbox.labels["inspect-task"] == "t" and "inspect-role" not in a.sandbox.labels
        assert (await a.exec(["cat", "/home/prized/marker"])).stdout == "installed\n"
        assert (await b.exec(["python", "-c", "print(6 * 7)"])).stdout == "42\n"
        # A fork that does not fit falls back to a fresh sandbox with the setup run in it.
        FakeSandbox.fork_error = PrizedError("fork_capacity", "full", status=409)
        c = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        assert not hasattr(c.sandbox, "forked_from") and (await c.exec(["cat", "/home/prized/marker"])).success
        FakeSandbox.fork_error = None  # a full machine is no reason to give the template up
        d = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        assert d.sandbox.forked_from == template.id
        # A copy that ended up deleted says nothing about the template: keep forking it.
        FakeSandbox.fork_error = PrizedError("sandbox_deleted", "copy gone", status=410, sandbox_id="sbx_copy")
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        FakeSandbox.fork_error = None
        d2 = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        assert d2.sandbox.forked_from == template.id
        # A template that is gone (deleted from outside) is not tried again.
        FakeSandbox.fork_error = PrizedError("not_found", "gone", status=404)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        FakeSandbox.fork_error = None
        e = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"].as_type(PrizedSandboxEnvironment)
        assert not hasattr(e.sandbox, "forked_from")
        await PrizedSandboxEnvironment.task_cleanup("shutdown", cfg, True)
        assert template.deleted and template.closed and env_mod._fallback.get().templates == {}
    finally:
        env_mod._fallback.reset(token)


async def test_setup_failure_deletes_the_sandbox(monkeypatch):
    import asyncio

    cfg = PrizedSandboxConfig(setup=["exit 3"])
    with pytest.raises(RuntimeError, match="setup command failed"):
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
    [sb] = FakeSandbox.made
    assert sb.deleted and sb.closed and sb.delete_waited  # gone before the next sample's create
    # Cancelled mid-setup: deleted without waiting, so the shutdown isn't held up.
    monkeypatch.setattr(FakeSandbox, "error", asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
    assert FakeSandbox.made[1].deleted and not FakeSandbox.made[1].delete_waited


async def test_fork_copies_go_with_the_sample():
    env, cfg, envs = await _env()
    await env.write_file("state.txt", "step 1")
    [copy] = await env.fork()
    assert await copy.read_file("state.txt") == "step 1"
    await copy.write_file("state.txt", "branch")
    assert await env.read_file("state.txt") == "step 1"
    await PrizedSandboxEnvironment.sample_cleanup("unit task", cfg, envs, False)
    assert env.sandbox.deleted and copy.sandbox.deleted


async def test_state_lives_on_inspects_lifecycle_scope(monkeypatch):
    # task_init may run in another task than the samples (a SampleSource feeder): the run label and
    # the templates must still reach the samples and task_cleanup, through Inspect's lifecycle scope.
    import asyncio

    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    async def delete_by_labels(labels, **_):
        hit = [sb for sb in FakeSandbox.made if all(sb.labels.get(k) == v for k, v in labels.items())]
        for sb in hit:
            sb.deleted = True
        return [sb.id for sb in hit]

    monkeypatch.setattr(env_mod.AsyncSandbox, "delete_by_labels", staticmethod(delete_by_labels))
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await asyncio.create_task(PrizedSandboxEnvironment.task_init("startup", cfg))  # its own context
        run = env_mod._state().run_id
        await asyncio.create_task(PrizedSandboxEnvironment.sample_init("t", cfg, {}))  # first: own setup
        envs = await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        template = FakeSandbox.made[1]
        sample = envs["default"].as_type(PrizedSandboxEnvironment).sandbox
        assert sample.forked_from == template.id and sample.labels["inspect-run"] == run
        await PrizedSandboxEnvironment.task_cleanup("shutdown", cfg, False)  # --no-sandbox-cleanup
        assert template.deleted and not sample.deleted  # templates hold no sample state
    assert env_mod._fallback.get() is None


async def test_setup_without_task_init_runs_in_the_sample():
    envs = await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(setup=["touch ~/ready"]), {})
    sb = envs["default"].as_type(PrizedSandboxEnvironment).sandbox
    assert len(FakeSandbox.made) == 1 and not hasattr(sb, "forked_from")  # no untracked template
    assert os.path.exists(os.path.join(sb.root, "ready"))


def _sweeper(monkeypatch):
    swept = []

    async def delete_by_labels(labels, **_):
        swept.append(labels)
        hit = [sb for sb in FakeSandbox.made if all(sb.labels.get(k) == v for k, v in labels.items())]
        for sb in hit:
            sb.deleted = True
        return [sb.id for sb in hit]

    monkeypatch.setattr(env_mod.AsyncSandbox, "delete_by_labels", staticmethod(delete_by_labels))
    return swept


async def test_templates_are_shared_by_content_capped_and_expire(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    swept = _sweeper(monkeypatch)

    async def two(cfg):
        for _ in range(2):
            envs = await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        return envs["default"].as_type(PrizedSandboxEnvironment).sandbox

    with sandbox_lifecycle_scope():
        # Labels and keep don't split a template; it always carries the run label and a paused TTL.
        a = PrizedSandboxConfig(setup=["touch ~/a"], labels={"team": "x"}, keep=True)
        await PrizedSandboxEnvironment.task_init("startup", a)
        kept = await two(a)  # the template is built for a keep config, yet carries the run label
        forked = await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(setup=["touch ~/a"]), {})
        forked = forked["default"].as_type(PrizedSandboxEnvironment).sandbox
        templates = [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]
        assert len(templates) == 1 and forked.forked_from == kept.forked_from == templates[0].id
        assert "inspect-run" not in kept.labels and "inspect-run" in forked.labels
        assert templates[0].options["auto_delete_min"] == 7 * 24 * 60 and "inspect-run" in templates[0].labels
        # A setup only one sample uses never builds a template; past MAX_TEMPLATES configs, none either.
        for i in range(env_mod.MAX_TEMPLATES + 1):
            await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(setup=[f"touch ~/once{i}"]), {})
        for i in range(env_mod.MAX_TEMPLATES):
            last = await two(PrizedSandboxConfig(setup=[f"touch ~/s{i}"]))
        templates = [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]
        assert len(templates) == env_mod.MAX_TEMPLATES
        assert not hasattr(last, "forked_from") and os.path.exists(os.path.join(last.root, f"s{i}"))
        # One run-label sweep however many configs Inspect cleans up, and templates go first.
        await PrizedSandboxEnvironment.task_cleanup("shutdown", a, True)  # keep: no sweep of samples
        await PrizedSandboxEnvironment.task_cleanup("shutdown", PrizedSandboxConfig(), True)
        await PrizedSandboxEnvironment.task_cleanup("shutdown", PrizedSandboxConfig(env={"X": "1"}), True)
        runs = [lb for lb in swept if "inspect-role" not in lb]
        assert len(runs) == 1 and last.deleted and all(t.deleted for t in templates)
        assert "inspect-role" in swept[0]  # templates are swept first, even for the keep config


async def test_at_a_disk_quota_the_template_makes_room(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)

    def templates():
        return [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]

    with sandbox_lifecycle_scope():
        cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        [template] = templates()
        # A paused template doesn't count toward the number of sandboxes: keep it.
        FakeSandbox.fork_error = PrizedError("sandbox_quota_exceeded", "n", status=429, reason="sandbox_limit")
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        assert not template.deleted
        # It counts toward disk: deleted (waiting until it is gone) to make room.
        FakeSandbox.fork_error = PrizedError("sandbox_quota_exceeded", "disk", status=429, reason="retained_disk")
        envs = await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        sb = envs["default"].as_type(PrizedSandboxEnvironment).sandbox
        assert template.deleted and template.delete_waited
        assert not hasattr(sb, "forked_from") and os.path.exists(os.path.join(sb.root, "ready"))
        FakeSandbox.fork_error = None
        envs = await PrizedSandboxEnvironment.sample_init("t", cfg, {})  # no template from here on
        assert not hasattr(envs["default"].as_type(PrizedSandboxEnvironment).sandbox, "forked_from")
        assert len(templates()) == 1


async def test_a_create_at_a_disk_quota_deletes_a_template_and_retries(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    real_create = env_mod.AsyncSandbox.create
    refusals = []

    async def create(**options):
        live = [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template" and not sb.deleted]
        if "inspect-role" not in options["labels"] and live and not refusals:
            refusals.append(options)
            raise PrizedError("sandbox_quota_exceeded", "disk", status=429, reason="retained_disk")
        return await real_create(**options)

    with sandbox_lifecycle_scope():
        cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        [template] = [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]
        monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))
        envs = await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(), {})  # no setup, no room
        assert len(refusals) == 1 and template.deleted and template.delete_waited
        assert envs["default"].as_type(PrizedSandboxEnvironment).sandbox.id
        # With no template left to give up, a quota refusal raises.

        async def always(**options):
            raise PrizedError("sandbox_quota_exceeded", "disk", status=429, reason="retained_disk")

        monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(always))
        with pytest.raises(PrizedError):
            await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(), {})


def _templates():
    return [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]


def _forks():
    return [sb for sb in FakeSandbox.made if getattr(sb, "forked_from", None)]


async def _wave(cfg, n):
    """n samples of cfg that start together (an eval's first samples): their environments."""
    import anyio

    envs = []

    async def sample():
        envs.append((await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"])

    async with anyio.create_task_group() as tg:
        for _ in range(n):
            tg.start_soon(sample)
    return envs


def _slow_creates(monkeypatch, cap=None):
    """Creates and setup commands that take a moment, so the samples of a wave run while a template is
    made and set up. With cap, a create that asks for no shape gets the template's default capped at cap
    (vCPU, GB), as on the free tier."""
    import anyio

    real_create = env_mod.AsyncSandbox.create
    real_exec = FakeSandbox.exec

    async def slow_create(**options):
        await anyio.sleep(0.01)
        sb = await real_create(**options)
        if cap is not None and "vcpu" not in options and "memory_gb" not in options:
            sb.resources = types.SimpleNamespace(vcpu=cap[0], memory_mb=cap[1] * 1024)
        return sb

    async def slow_exec(self, cmd, *args, **kw):
        if cmd[-1] == "touch ~/ready":
            await anyio.sleep(0.05)
        return await real_exec(self, cmd, *args, **kw)

    monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(slow_create))
    monkeypatch.setattr(FakeSandbox, "exec", slow_exec)


async def test_concurrent_samples_build_one_template(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    _slow_creates(monkeypatch)
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        envs = await _wave(cfg, 10)
        # The first sets up its own; the second builds the template and forks it. Of the 8 that start
        # while it is set up, 6 wait and fork it (with the second's copy, the 7 a 4 vCPU sandbox's machine
        # holds); the other 2 set up their own rather than wait for a fork with no room.
        [template] = _templates()
        assert len(_forks()) == 7 and len(FakeSandbox.made) == 11
        assert all(sb.forked_from == template.id for sb in _forks())
        # Once it is paused, every sample forks it, however many start together.
        later = await _wave(cfg, 10)
        assert len(_forks()) == 17 and len(FakeSandbox.made) == 21
        for env in [*envs, *later]:
            assert (await env.exec(["test", "-f", "/home/prized/ready"])).success


async def test_samples_that_find_the_template_set_up_once_its_create_answers_fork_it(monkeypatch):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    real_create = env_mod.AsyncSandbox.create

    async def slow_create(**options):  # the setup itself takes no time here
        await anyio.sleep(0.01)
        return await real_create(**options)

    monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(slow_create))
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await _wave(cfg, 10)
        [template] = _templates()
        assert len(_forks()) == 9 and len(FakeSandbox.made) == 11


@pytest.mark.parametrize(
    "shape,cap,forks",
    [
        ({}, None, 7),  # the default 4 vCPU: 7 copies fit, the second's and 6 more
        ({"memory_gb": 2}, None, 7),  # memory doesn't change who waits
        ({}, (2, 4), 9),  # the free tier's 2 vCPU: 14 may wait, more than its 10 sandboxes at once
        ({"vcpu": 2, "memory_gb": 2}, None, 9),
        ({"vcpu": 8, "memory_gb": 3.5}, None, 3),  # 3 copies fit: the second's and 2 more
        ({"memory_gb": 4, "vcpu": 16}, None, 1),  # 1 fits: the second's own
    ],
)
async def test_how_many_samples_wait_for_a_template_in_setup(monkeypatch, shape, cap, forks):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    _slow_creates(monkeypatch, cap)
    monkeypatch.setattr(FakeSandbox, "room", "shape")
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"], **shape)
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        envs = await _wave(cfg, 10)
        [template] = _templates()
        assert len(_forks()) == forks and len(FakeSandbox.made) == 1 + 10
        assert FakeSandbox.room_refusals == []  # no sample waited for a fork that had no room
        for env in envs:
            assert (await env.exec(["test", "-f", "/home/prized/ready"])).success


@pytest.mark.parametrize("shape", [{}, {"memory_gb": 2}])
async def test_a_sample_past_the_room_never_waits_for_a_template_still_in_setup(monkeypatch, shape):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    gate = anyio.Event()
    real_exec = FakeSandbox.exec

    async def exec_(self, cmd, *args, **kw):
        if self.labels.get("inspect-role") == "template" and cmd[-1] == "touch ~/ready":
            await gate.wait()  # the template's setup takes as long as the test says
        return await real_exec(self, cmd, *args, **kw)

    monkeypatch.setattr(FakeSandbox, "exec", exec_)
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"], **shape)
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})  # the first: its own setup
        async with anyio.create_task_group() as tg:
            tg.start_soon(PrizedSandboxEnvironment.sample_init, "t", cfg, {})  # builds the template
            for _ in range(6):  # with the builder's copy, the 7 its machine holds
                tg.start_soon(PrizedSandboxEnvironment.sample_init, "t", cfg, {})
            await anyio.sleep(0.05)
            [template] = _templates()
            assert len(FakeSandbox.made) == 2 and not template.paused  # those 6 wait, holding no sandbox
            with anyio.fail_after(5):  # the 7th sets up its own, while the template's setup still runs
                own = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"]
            own = own.as_type(PrizedSandboxEnvironment).sandbox
            assert not hasattr(own, "forked_from") and own.labels.get("inspect-role") is None
            assert not template.paused and _forks() == []
            gate.set()
        assert template.paused and len(_forks()) == 7
        copy = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"]
        assert copy.as_type(PrizedSandboxEnvironment).sandbox.forked_from == template.id


def _own():
    return [
        sb
        for sb in FakeSandbox.made
        if not getattr(sb, "forked_from", None) and sb.labels.get("inspect-role") != "template"
    ]


def _gated_template_setup(monkeypatch):
    """Each template's setup waits for its own gate (by sandbox id), so a test can hold it."""
    import anyio

    gates = {}
    real_exec = FakeSandbox.exec

    async def exec_(self, cmd, *args, **kw):
        if self.labels.get("inspect-role") == "template" and cmd[-1] == "touch ~/ready":
            await gates.setdefault(self.id, anyio.Event()).wait()
        return await real_exec(self, cmd, *args, **kw)

    monkeypatch.setattr(FakeSandbox, "exec", exec_)
    return gates


async def test_a_build_cancelled_mid_setup_is_taken_over_and_still_caps_who_waits(monkeypatch):
    # The builder is cancelled after its create answered, with 3 samples waiting: one of those takes the
    # build over (no longer counted as waiting), and of 8 that start during its setup, 4 more wait (6 in
    # all, beside the new builder's copy: the 7 a 4 vCPU sandbox's machine holds) and 4 set up their own.
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    gates = _gated_template_setup(monkeypatch)
    monkeypatch.setattr(FakeSandbox, "room", "shape")
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})  # the first: its own
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                scope = anyio.CancelScope()

                async def builder():
                    with scope:
                        await PrizedSandboxEnvironment.sample_init("t", cfg, {})

                tg.start_soon(builder)
                await anyio.sleep(0.05)
                for _ in range(3):
                    tg.start_soon(PrizedSandboxEnvironment.sample_init, "t", cfg, {})
                await anyio.sleep(0.05)
                scope.cancel()
                await anyio.sleep(0.05)
                first, second = _templates()
                assert first.deleted and not second.deleted  # a waiter is setting up the second
                for _ in range(8):
                    tg.start_soon(PrizedSandboxEnvironment.sample_init, "t", cfg, {})
                await anyio.sleep(0.2)
                assert len(_own()) == 1 + 4 and _forks() == []
                gates[second.id].set()
        assert second.paused and len(_forks()) == 7 and FakeSandbox.room_refusals == []
        assert all(sb.forked_from == second.id for sb in _forks())


@pytest.mark.parametrize("cap,forks", [(None, 7), ((2, 4), 8)])
async def test_a_build_cancelled_before_its_create_answers_is_taken_over_at_its_real_shape(monkeypatch, cap, forks):
    # The builder is cancelled while its create has not answered, with 8 samples waiting for that answer:
    # one takes the build over, and the other 7 decide on the shape its own create answers with (on the
    # free tier, 2 vCPU: all 7 wait) and never count the new builder as one of them.
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    real_create = env_mod.AsyncSandbox.create
    stalled = []

    async def create(**options):
        if options["labels"].get("inspect-role") == "template" and not stalled:
            stalled.append(options)
            await anyio.sleep_forever()
        await anyio.sleep(0.01)
        sb = await real_create(**options)
        if cap is not None:
            sb.resources = types.SimpleNamespace(vcpu=cap[0], memory_mb=cap[1] * 1024)
        return sb

    monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))
    gates = _gated_template_setup(monkeypatch)
    monkeypatch.setattr(FakeSandbox, "room", "shape")
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                scope = anyio.CancelScope()

                async def builder():
                    with scope:
                        await PrizedSandboxEnvironment.sample_init("t", cfg, {})

                tg.start_soon(builder)
                await anyio.sleep(0.02)
                for _ in range(8):
                    tg.start_soon(PrizedSandboxEnvironment.sample_init, "t", cfg, {})
                await anyio.sleep(0.02)
                scope.cancel()
                await anyio.sleep(0.2)
                [template] = _templates()
                gates[template.id].set()
        assert len(_forks()) == forks and len(_own()) == 1 + 8 - forks
        assert FakeSandbox.room_refusals == []


async def test_a_waiting_sample_that_is_cancelled_gives_its_place_back(monkeypatch):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    gates = _gated_template_setup(monkeypatch)
    monkeypatch.setattr(FakeSandbox, "room", "shape")
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                scopes = [anyio.CancelScope() for _ in range(6)]

                async def run(scope):
                    with scope:
                        await PrizedSandboxEnvironment.sample_init("t", cfg, {})

                tg.start_soon(PrizedSandboxEnvironment.sample_init, "t", cfg, {})  # builds the template
                await anyio.sleep(0.05)
                for scope in scopes:  # the 6 that fit beside the builder's copy wait
                    tg.start_soon(run, scope)
                await anyio.sleep(0.05)
                scopes[0].cancel()
                scopes[1].cancel()
                await anyio.sleep(0.05)
                for _ in range(2):  # their places are free again: these wait too
                    tg.start_soon(PrizedSandboxEnvironment.sample_init, "t", cfg, {})
                await anyio.sleep(0.05)
                assert len(_own()) == 1
                gates[_templates()[0].id].set()
        assert len(_forks()) == 7 and FakeSandbox.room_refusals == []


async def test_samples_waiting_for_a_create_whose_builder_is_cancelled_are_released(monkeypatch):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    real_create = env_mod.AsyncSandbox.create
    stalled = []

    async def create(**options):
        if options["labels"].get("inspect-role") == "template" and not stalled:
            stalled.append(options)
            await anyio.sleep_forever()
        return await real_create(**options)

    monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                scope = anyio.CancelScope()

                async def builder():
                    with scope:
                        await PrizedSandboxEnvironment.sample_init("t", cfg, {})

                tg.start_soon(builder)
                await anyio.sleep(0.02)
                for _ in range(3):
                    tg.start_soon(PrizedSandboxEnvironment.sample_init, "t", cfg, {})
                await anyio.sleep(0.02)
                scope.cancel()
        assert len(_forks()) == 3 and len(_templates()) == 1


async def test_a_queued_claim_cancelled_during_a_take_over_leaves_it_owned(monkeypatch):
    # After a cancelled build, a sample that starts before a waiter takes it over claims it and queues;
    # cancelling that queued sample must not clear the claim of the waiter now running the setup.
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    gates = _gated_template_setup(monkeypatch)
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        entry = env_mod._state().templates
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                first, queued = anyio.CancelScope(), anyio.CancelScope()

                async def run(scope):
                    with scope:
                        await PrizedSandboxEnvironment.sample_init("t", cfg, {})

                tg.start_soon(run, first)
                await anyio.sleep(0.05)
                tg.start_soon(PrizedSandboxEnvironment.sample_init, "t", cfg, {})  # waits
                await anyio.sleep(0.05)
                first.cancel()
                tg.start_soon(run, queued)  # runs before the waiter has the lock: claims, then queues
                await anyio.sleep(0.05)
                [entry] = entry.values()
                owner = entry.builder
                assert owner is not None and len(_templates()) == 2  # the waiter sets up the second
                queued.cancel()
                await anyio.sleep(0.05)
                assert entry.builder is owner  # the queued sample's cancel leaves the waiter's claim
                gates[_templates()[1].id].set()
        assert entry.builder is None and _templates()[1].paused and len(_forks()) == 1


async def test_samples_waiting_for_a_template_whose_create_fails_set_up_their_own(monkeypatch):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    real_create = env_mod.AsyncSandbox.create

    async def create(**options):
        await anyio.sleep(0.05)
        if options["labels"].get("inspect-role") == "template":
            raise PrizedError("sandbox_capacity", "none free", status=503)
        return await real_create(**options)

    monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        with anyio.fail_after(5):  # those waiting for the template's shape are told it has none
            envs = await _wave(cfg, 5)
        assert _templates() == [] and _forks() == [] and len(FakeSandbox.made) == 5
        for env in envs:
            assert (await env.exec(["test", "-f", "/home/prized/ready"])).success


async def test_a_cancelled_template_build_lets_the_next_sample_build_it(monkeypatch):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    real_exec = FakeSandbox.exec
    stall = {"on": True}

    async def exec_(self, cmd, *args, **kw):
        if stall["on"] and self.labels.get("inspect-role") == "template" and cmd[-1] == "touch ~/ready":
            await anyio.sleep_forever()
        return await real_exec(self, cmd, *args, **kw)

    monkeypatch.setattr(FakeSandbox, "exec", exec_)
    cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
    with sandbox_lifecycle_scope():
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        with anyio.move_on_after(0.2):
            await PrizedSandboxEnvironment.sample_init("t", cfg, {})  # cancelled mid-setup
        [first] = [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]
        assert first.deleted
        stall["on"] = False
        copy = (await PrizedSandboxEnvironment.sample_init("t", cfg, {}))["default"]
        [_, template] = [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]
        assert template.paused and copy.as_type(PrizedSandboxEnvironment).sandbox.forked_from == template.id


async def test_a_failed_template_build_leaves_setup_to_the_samples(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    with sandbox_lifecycle_scope():
        cfg = PrizedSandboxConfig(setup=["exit 3"])
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="setup command failed"):
                await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        assert len([s for s in FakeSandbox.made if s.labels.get("inspect-role") == "template"]) == 1
        assert len(FakeSandbox.made) == 3  # the builder didn't run the failing setup a second time
        assert all(s.deleted for s in FakeSandbox.made)


async def _two_templates(monkeypatch):
    _sweeper(monkeypatch)
    for name in ("x", "y"):
        cfg = PrizedSandboxConfig(setup=[f"touch ~/{name}"])
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        for _ in range(2):
            await PrizedSandboxEnvironment.sample_init("t", cfg, {})
    return [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]


async def test_two_refused_creates_each_free_a_template(monkeypatch):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    with sandbox_lifecycle_scope():
        templates = await _two_templates(monkeypatch)
        assert len(templates) == 2
        limit = len([sb for sb in FakeSandbox.made if not sb.deleted])  # the workspace is full
        real_create = env_mod.AsyncSandbox.create

        async def create(**options):
            await anyio.sleep(0)
            if len([sb for sb in FakeSandbox.made if not sb.deleted]) >= limit:
                raise PrizedError("sandbox_quota_exceeded", "disk", status=429, reason="retained_disk")
            return await real_create(**options)

        async def slow_delete(self, wait=False):
            await anyio.sleep(0.02)
            self.deleted, self.delete_waited = True, wait

        monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))
        monkeypatch.setattr(FakeSandbox, "delete", slow_delete)
        made = []

        async def sample():
            envs = await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(), {})
            made.append(envs["default"])

        async with anyio.create_task_group() as tg:
            tg.start_soon(sample)
            tg.start_soon(sample)
        assert len(made) == 2 and all(t.deleted for t in templates)


async def test_a_template_waits_for_its_copies_and_a_cancelled_drop_is_redone(monkeypatch):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    with sandbox_lifecycle_scope():
        template = (await _two_templates(monkeypatch))[0]
        entry = next(e for e in env_mod._state().templates.values() if e.sandbox is template)
        entry.forking = 1  # a copy is still starting
        with anyio.move_on_after(0.1):
            await env_mod._drop_template(entry)
        assert not template.deleted and entry.sandbox is template and entry.gone is None
        entry.forking = 0
        await env_mod._drop_template(entry)
        assert template.deleted and template.delete_waited and entry.sandbox is None


async def test_a_create_frees_as_many_templates_as_it_needs(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    with sandbox_lifecycle_scope():
        templates = await _two_templates(monkeypatch)
        limit = len([sb for sb in FakeSandbox.made if not sb.deleted]) - 1  # one more than full
        real_create = env_mod.AsyncSandbox.create

        async def create(**options):
            if len([sb for sb in FakeSandbox.made if not sb.deleted]) >= limit:
                raise PrizedError("sandbox_quota_exceeded", "disk", status=429, reason="retained_disk")
            return await real_create(**options)

        monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))
        envs = await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(), {})
        assert envs["default"] and all(t.deleted for t in templates)


async def test_a_template_build_that_fails_waits_for_its_delete(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)

    async def pause_fails(self, wait=True):
        raise PrizedError("sandbox_not_running", "could not pause", status=409)

    with sandbox_lifecycle_scope():
        cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        monkeypatch.setattr(FakeSandbox, "pause", pause_fails)
        envs = await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        [template] = [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]
        assert template.deleted and template.delete_waited  # gone before the sample's own create
        sb = envs["default"].as_type(PrizedSandboxEnvironment).sandbox
        assert not hasattr(sb, "forked_from") and os.path.exists(os.path.join(sb.root, "ready"))


def _counted():
    """Sandboxes that count toward the number of sandboxes: not deleted, not paused (`error` counts)."""
    return [sb for sb in FakeSandbox.made if not sb.deleted and not sb.paused]


@pytest.mark.parametrize("code", ["parent_not_running", "parent_not_forkable", "not_found", "sandbox_deleted"])
async def test_a_template_that_cant_be_forked_is_deleted_at_once(monkeypatch, code):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    with sandbox_lifecycle_scope():
        cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        [template] = [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]
        real_create = env_mod.AsyncSandbox.create
        if code == "parent_not_running":
            # The template went to `error`, so it counts toward the number of sandboxes, and the
            # workspace is at that limit (the free tier's 10 at the default concurrency): the sample
            # fits only once the template is gone.
            template.paused, template.errored = False, True
            full = len(_counted())

            async def create(**options):
                if len(_counted()) >= full:
                    raise PrizedError("sandbox_quota_exceeded", "n", status=429, reason="sandbox_limit")
                return await real_create(**options)

            monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))
        FakeSandbox.fork_error = PrizedError(code, "can't fork", status=409, sandbox_id=template.id)
        envs = await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        sb = envs["default"].as_type(PrizedSandboxEnvironment).sandbox
        assert template.deleted and template.delete_waited and template.closed  # gone before the create
        assert template.refreshes == 0  # the fork's refusal deleted it, not a create at the limit
        assert not hasattr(sb, "forked_from") and os.path.exists(os.path.join(sb.root, "ready"))
        monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(real_create))
        FakeSandbox.fork_error = AssertionError("a template that can't be forked is not tried again")
        envs = await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        assert not hasattr(envs["default"].as_type(PrizedSandboxEnvironment).sandbox, "forked_from")


async def test_a_template_that_cant_be_forked_waits_for_copies_still_starting(monkeypatch):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    _sweeper(monkeypatch)
    with sandbox_lifecycle_scope():
        cfg = PrizedSandboxConfig(setup=["touch ~/ready"])
        await PrizedSandboxEnvironment.task_init("startup", cfg)
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        await PrizedSandboxEnvironment.sample_init("t", cfg, {})
        [template] = [sb for sb in FakeSandbox.made if sb.labels.get("inspect-role") == "template"]
        real_fork = FakeSandbox.fork
        started = anyio.Event()
        seen = []

        async def fork(self, count=1, labels=None):
            if not started.is_set():  # this fork got in before the template broke: its copy is starting
                started.set()
                await anyio.sleep(0.05)
                copies = await real_fork(self, count, labels)
                seen.append(self.deleted)
                return copies
            raise PrizedError("parent_not_running", "error", status=409, sandbox_id=self.id)

        monkeypatch.setattr(FakeSandbox, "fork", fork)
        made = []

        async def sample():
            envs = await PrizedSandboxEnvironment.sample_init("t", cfg, {})
            made.append(envs["default"].as_type(PrizedSandboxEnvironment).sandbox)

        async with anyio.create_task_group() as tg:
            tg.start_soon(sample)
            await started.wait()
            tg.start_soon(sample)
        assert seen == [False]  # the template was still there when its last copy finished starting
        assert template.deleted and template.delete_waited
        assert sorted(hasattr(sb, "forked_from") for sb in made) == [False, True]


def _full_at_the_sandbox_count(monkeypatch):
    """Creates are refused at sandbox_limit while as many sandboxes count as do now."""
    full = len(_counted())
    real_create = env_mod.AsyncSandbox.create

    async def create(**options):
        if len(_counted()) >= full:
            raise PrizedError("sandbox_quota_exceeded", "n", status=429, reason="sandbox_limit")
        return await real_create(**options)

    monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))


async def test_at_the_sandbox_limit_a_template_in_error_is_found_and_deleted(monkeypatch):
    # No fork comes to find it out (its config has no samples left): the create reads the templates.
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    with sandbox_lifecycle_scope():
        x, y = await _two_templates(monkeypatch)
        real_create = env_mod.AsyncSandbox.create
        _full_at_the_sandbox_count(monkeypatch)
        # Paused templates don't count toward the number of sandboxes: none is deleted, the sample fails.
        with pytest.raises(PrizedError, match="sandbox_limit"):
            await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(), {})
        assert not x.deleted and not y.deleted and x.refreshes == y.refreshes == 1
        x.paused, x.errored = False, True  # x went to `error`: it counts now
        monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(real_create))
        _full_at_the_sandbox_count(monkeypatch)
        envs = await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(), {})
        assert envs["default"] and x.deleted and x.delete_waited and not y.deleted


async def test_at_a_disk_quota_a_given_up_template_goes_first(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    with sandbox_lifecycle_scope():
        x, y = await _two_templates(monkeypatch)
        entry = next(e for e in env_mod._state().templates.values() if e.sandbox is y)
        entry.failed = True  # no sample forks it any more (its drop was cancelled, say)
        real_create = env_mod.AsyncSandbox.create
        refused = []

        async def create(**options):
            if not refused:
                refused.append(options)
                raise PrizedError("sandbox_quota_exceeded", "disk", status=429, reason="retained_disk")
            return await real_create(**options)

        monkeypatch.setattr(env_mod.AsyncSandbox, "create", staticmethod(create))
        await PrizedSandboxEnvironment.sample_init("t", PrizedSandboxConfig(), {})
        assert y.deleted and not x.deleted


async def test_task_cleanup_takes_the_templates_from_samples_still_starting(monkeypatch):
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    with sandbox_lifecycle_scope():
        x, _ = await _two_templates(monkeypatch)
        entry = next(e for e in env_mod._state().templates.values() if e.sandbox is x)
        await PrizedSandboxEnvironment.task_cleanup("shutdown", PrizedSandboxConfig(), True)
        assert x.deleted and x.closed and entry.sandbox is None and entry.failed  # nothing left to fork


async def test_reading_a_template_at_the_sandbox_limit(monkeypatch):
    import anyio
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_scope

    with sandbox_lifecycle_scope():
        x, _ = await _two_templates(monkeypatch)
        entry = next(e for e in env_mod._state().templates.values() if e.sandbox is x)
        assert not await env_mod._counts(entry)  # paused

        async def gone(self):
            raise PrizedError("not_found", "nope", status=404)

        monkeypatch.setattr(FakeSandbox, "refresh", gone)
        assert not await env_mod._counts(entry)  # deleted or unreadable: left to a fork to find out

        async def drop_starts(self):
            entry.gone = anyio.Event()  # another sample starts deleting it while this one reads it

        monkeypatch.setattr(FakeSandbox, "refresh", drop_starts)
        assert await env_mod._counts(entry)  # counts until it is gone: wait for that drop, then retry
