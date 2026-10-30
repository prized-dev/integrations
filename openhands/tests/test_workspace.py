"""PrizedWorkspace end to end against fake Prized sandboxes and a fake Agent Server.

The fakes run the real in-sandbox scripts (probe, stop, start, health, the
WebSocket relay) with the local bash; only the install step is canned."""

import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import PRIZED_KEY, free_port
from fakes import FakeAsyncSandbox, FakeSandbox

from openhands_prized import PrizedWorkspace


@pytest.fixture
def fake_prized(monkeypatch, tmp_path):
    import prized

    home = tmp_path / "home"
    home.mkdir()
    reg: dict = {}
    created: list = []
    asyncs: list = []
    opts = SimpleNamespace(raw={}, fail_install=False, install_stdout=None)

    class FakeSandboxCls:
        @staticmethod
        def create(template, **kw):
            assert kw["api_key"] == PRIZED_KEY
            sb = FakeSandbox(
                home=str(home), raw=opts.raw, fail_install=opts.fail_install, install_stdout=opts.install_stdout
            )
            sb.create_kw = {"template": template, **kw}
            reg[sb.id] = sb
            created.append(sb)
            return sb

        @staticmethod
        def connect(sandbox_id, **kw):
            assert kw["api_key"] == PRIZED_KEY
            return reg[sandbox_id]

    class FakeAsyncCls:
        @staticmethod
        async def connect(sandbox_id, **kw):
            assert kw["api_key"] == PRIZED_KEY
            a = FakeAsyncSandbox(home=str(home))
            asyncs.append(a)
            return a

    monkeypatch.setattr(prized, "Sandbox", FakeSandboxCls)
    monkeypatch.setattr(prized, "AsyncSandbox", FakeAsyncCls)
    monkeypatch.delenv("PRIZED_API_KEY", raising=False)
    ns = SimpleNamespace(home=home, reg=reg, created=created, asyncs=asyncs, opts=opts, tmp=tmp_path)
    yield ns
    for sb in list(reg.values()):
        sb.stop_all()


def _ws(fake_prized, **kw):
    work = fake_prized.tmp / "work"
    work.mkdir(exist_ok=True)
    args = {
        "prized_api_key": PRIZED_KEY,
        "working_dir": str(work),
        "server_port": free_port(),
        "agent_server_version": "1.59.0",
        "keep_alive_interval": 0.2,
    }
    args.update(kw)
    return PrizedWorkspace(**args)


def _port_closed(url: str) -> bool:
    host, port = url.split("//")[1].split(":")
    with socket.socket() as s:
        return s.connect_ex((host, int(port))) != 0


def test_full_lifecycle(fake_prized):
    ws = _ws(fake_prized, labels={"team": "evals"}, vcpu=2, memory_gb=4)
    sb = fake_prized.created[0]
    try:
        assert sb.create_kw["labels"] == {"openhands": "workspace", "team": "evals"}
        assert sb.create_kw["vcpu"] == 2 and sb.create_kw["memory_gb"] == 4
        assert ws.transport == "exec"
        assert ws.host.startswith("http://127.0.0.1:")
        assert ws.api_key and len(ws.api_key) > 30
        assert ws.alive
        assert {"sandbox_s", "install_s", "start_s", "server_s", "ready_s"} <= set(ws.timings)
        # The SDK's own client, through the proxy, with the session key.
        assert ws.git_changes(str(fake_prized.tmp / "work")) == []
        assert httpx.get(ws.host + "/api/echo").status_code == 401
        echo = httpx.get(ws.host + "/api/echo", headers={"X-Session-API-Key": ws.api_key}).json()
        assert echo["method"] == "GET"
        # The session key reached the server in its environment, not its command line.
        start = [c for k, *c in sb.log if k == "exec" and "agent-server" in c[0] and "--port" in c[0]]
        assert start and ws.api_key not in start[0][0]
        state = json.loads((fake_prized.home / ".openhands-prized" / "server.json").read_text())
        assert state["key"] == ws.api_key and state["version"] == "1.59.0"
        assert oct((fake_prized.home / ".openhands-prized" / "server.json").stat().st_mode & 0o777) == "0o600"
    finally:
        ws.cleanup()
    assert ("delete",) in sb.log
    assert _port_closed(ws.host)
    assert all(h.status().running is False for h in sb.handles)


def test_commands_and_files_go_straight_to_the_sandbox(fake_prized):
    with _ws(fake_prized, env={"MY_VAR": "v1"}) as ws:
        r = ws.execute_command("echo out; echo err >&2; echo $MY_VAR; pwd; exit 3")
        assert (r.exit_code, r.stdout.split(), r.stderr.strip()) == (3, ["out", "v1", ws.working_dir], "err")
        assert ws.execute_command("pwd", cwd="sub").exit_code != 0  # no such dir: a result, not a raise
        up = ws.file_upload(b"hello", "notes/a.txt")
        assert up.success and up.destination_path == f"{ws.working_dir}/notes/a.txt" and up.file_size == 5
        local = fake_prized.tmp / "out" / "a.txt"
        down = ws.file_download("notes/a.txt", local)
        assert down.success and local.read_bytes() == b"hello"
        missing = ws.file_download("nope.txt", fake_prized.tmp / "x")
        assert not missing.success and missing.error


def test_keys_never_serialized_or_shown(fake_prized):
    with _ws(fake_prized, env={"SECRET_TOKEN": "s3cret"}) as ws:
        dumped = json.dumps(ws.model_dump(mode="json"))
        for secret in (PRIZED_KEY, "s3cret", ws.api_key):
            assert secret not in dumped and secret not in repr(ws)


def test_commands_see_forwarded_env(fake_prized, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "fwd-1")
    with _ws(fake_prized, forward_env=["MY_TOKEN", "NOT_SET"]) as ws:
        assert ws.execute_command("echo $MY_TOKEN").stdout.strip() == "fwd-1"


def test_a_command_while_paused_resumes_properly(fake_prized):
    with _ws(fake_prized) as ws:
        ws.pause()
        assert ws.execute_command("echo back").stdout.strip() == "back"
        assert ("resume",) in ws.sandbox.log
        assert httpx.get(ws.host + "/health").status_code == 200, "the proxy is open again"


def test_key_from_environment_and_missing_key(fake_prized, monkeypatch):
    with pytest.raises(ValueError, match="No Prized API key"):
        _ws(fake_prized, prized_api_key=None)
    monkeypatch.setenv("PRIZED_API_KEY", PRIZED_KEY)
    with _ws(fake_prized, prized_api_key=None) as ws:
        assert ws.alive


def test_keep_alive_while_open(fake_prized):
    with _ws(fake_prized, keep_alive_interval=0.1) as ws:
        time.sleep(0.5)
        assert ("keep_alive",) in ws.sandbox.log


def test_reattach_reuses_the_running_server(fake_prized):
    first = _ws(fake_prized, on_exit="keep")
    port, key = first.server_port, first.api_key
    sb = first.sandbox
    first.cleanup()
    assert ("delete",) not in sb.log
    starts = len(sb.handles)
    second = _ws(fake_prized, sandbox_id=sb.id, server_port=port)
    try:
        assert second.api_key == key, "same server, same session key"
        assert len(sb.handles) == starts, "nothing restarted"
        assert second.alive
    finally:
        second.cleanup()
    assert ("delete",) not in sb.log, "an attached sandbox is kept by default"


def test_reattach_with_new_env_restarts(fake_prized):
    first = _ws(fake_prized, on_exit="keep")
    sb, port = first.sandbox, first.server_port
    first.cleanup()
    second = _ws(fake_prized, sandbox_id=sb.id, server_port=port, env={"NEW": "1"})
    try:
        assert second.api_key != first.api_key
        assert second.alive
        assert sb.handles[0].status().running is False, "the old server was stopped before the new one started"
        assert second.git_changes(str(fake_prized.tmp / "work")) == [], "the new key works"
    finally:
        second.cleanup()


def test_pause_on_exit_and_pause_resume(fake_prized):
    ws = _ws(fake_prized, on_exit="pause")
    ws.pause()
    assert httpx.get(ws.host + "/health").status_code == 503, "nothing may wake a paused sandbox"
    ws.resume()
    assert ws.alive
    ws.cleanup()
    log = [e[0] for e in ws.sandbox.log]
    assert log.count("pause") == 2 and "resume" in log and "delete" not in log


def test_fork_from(fake_prized):
    parent = _ws(fake_prized, on_exit="keep")
    parent_sb = parent.sandbox
    parent.cleanup()
    child = _ws(fake_prized, fork_from=parent_sb.id, labels={"run": "7"}, server_port=parent.server_port)
    try:
        assert ("fork", 1, {"openhands": "workspace", "run": "7"}) in parent_sb.log
        assert child.sandbox is not parent_sb
        assert child.alive
        assert child.api_key == parent.api_key, "a fork carries the running server: reused, not restarted"
    finally:
        child.cleanup()
    assert ("delete",) in child.sandbox.log


def test_failed_install_deletes_the_sandbox(fake_prized):
    fake_prized.opts.fail_install = True
    with pytest.raises(RuntimeError, match="installing openhands-agent-server 1.59.0 failed"):
        _ws(fake_prized)
    assert ("delete",) in fake_prized.created[0].log


def test_server_that_dies_is_reported_with_its_output(fake_prized):
    ws_args = {"server_args": ["--crash"]}
    real = Path(__file__).with_name("fake_agent_server.py")
    crash = fake_prized.tmp / "crash.py"
    crash.write_text("import sys\nprint('boom: no such tool', file=sys.stderr)\nsys.exit(4)\n")
    orig_install = FakeSandbox._fake_install

    def install(self):
        out = orig_install(self)
        script = Path(self.home) / ".openhands-prized" / "bin" / "agent-server"
        script.write_text(script.read_text().replace(str(real), str(crash)))
        return out

    FakeSandbox._fake_install = install
    try:
        with pytest.raises(RuntimeError, match="boom: no such tool"):
            _ws(fake_prized, **ws_args)
    finally:
        FakeSandbox._fake_install = orig_install
    assert ("delete",) in fake_prized.created[0].log


def test_connection_choice(fake_prized, monkeypatch):
    with pytest.raises(RuntimeError, match="not turned on"):
        _ws(fake_prized, connection="ingress")
    fake_prized.opts.raw = {"previewUrlTemplate": "https://{port}-sbx-00000000000000aa.p.prized.dev/"}
    monkeypatch.setattr(PrizedWorkspace, "_probe_ingress", staticmethod(lambda base, key: 402))
    with _ws(fake_prized) as ws:
        assert ws.transport == "exec", "exec is the default"
    with _ws(fake_prized, connection="auto") as ws:
        assert ws.transport == "exec", "auto falls back when the port URL refuses"
    with pytest.raises(RuntimeError, match="answered 402"):
        _ws(fake_prized, connection="ingress")


def test_sdk_websocket_client_through_the_proxy(fake_prized):
    from openhands.sdk.conversation.impl.remote_conversation import WebSocketCallbackClient
    from openhands.sdk.event import ConversationStateUpdateEvent

    hello = ConversationStateUpdateEvent(key="execution_status", value="idle").model_dump_json()
    with _ws(fake_prized, env={"FAKE_WS_HELLO": hello}) as ws:
        events = []
        client = WebSocketCallbackClient(
            host=ws.host, conversation_id="c-1", callback=events.append, api_key=ws.api_key
        )
        client.start()
        try:
            assert client.wait_until_ready(timeout=30), "the SDK saw the subscription complete"
        finally:
            client.stop()
        assert isinstance(events[0], ConversationStateUpdateEvent)


def test_serve_reattaches_to_the_newest_canvas_sandbox(monkeypatch):
    import prized

    from openhands_prized import cli

    seen = {}

    def fake_list(**kw):
        seen.update(kw)
        return [SimpleNamespace(id="sbx_failed", state="failed"), SimpleNamespace(id="sbx_new", state="paused")]

    monkeypatch.setattr(prized, "Sandbox", SimpleNamespace(list=fake_list))
    assert cli._previous_sandbox() == "sbx_new"
    assert seen["labels"] == {"openhands": "canvas"}
    monkeypatch.setattr(prized, "Sandbox", SimpleNamespace(list=lambda **kw: []))
    assert cli._previous_sandbox() is None


def test_cli_parser():
    from openhands_prized.cli import _labels, build_parser

    args = build_parser().parse_args(["serve", "--sandbox", "sbx_1", "--on-exit", "delete", "--label", "a=b"])
    assert (args.sandbox, args.on_exit, args.label) == ("sbx_1", "delete", ["a=b"])
    assert _labels(["a=b", "c=d=e"]) == {"a": "b", "c": "d=e"}
    with pytest.raises(SystemExit):
        _labels(["nope"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve", "--sandbox", "a", "--fork", "b"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve", "--sandbox", "a", "--new"])
    assert build_parser().parse_args(["serve"]).connection == "exec"
    from openhands_prized.cli import _free

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        assert not _free(taken.getsockname()[1])
    finally:
        taken.close()
