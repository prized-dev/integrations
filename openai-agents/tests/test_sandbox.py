"""PrizedSandboxClient and PrizedSandboxSession against the fake API."""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agents.sandbox import Manifest, SandboxPathGrant, User
from agents.sandbox.entries import S3Mount
from agents.sandbox.entries.mounts import InContainerMountStrategy, RcloneMountPattern
from agents.sandbox.errors import (
    ConfigurationError,
    ExecTimeoutError,
    ExecTransportError,
    InvalidManifestPathError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
)
from agents.sandbox.manifest import Environment
from agents.sandbox.session import BaseSandboxClientOptions, SandboxSessionState
from agents.sandbox.snapshot import LocalSnapshotSpec

import openai_agents_prized._files as files_module
from openai_agents_prized import (
    DEFAULT_PRIZED_WORKSPACE_ROOT,
    PrizedAPIError,
    PrizedError,
    PrizedSandboxClient,
    PrizedSandboxClientOptions,
    PrizedSandboxSession,
    PrizedSandboxSessionState,
    PrizedSandboxTimeouts,
)
from tests.fake_prized import API_KEY, API_ORIGIN, PRIZED_HOME

if TYPE_CHECKING:
    from collections.abc import Callable

    from agents.sandbox.session import SandboxSession

    from tests.fake_prized import FakeClock, FakePrized

MakeClient = "Callable[..., PrizedSandboxClient]"


def _inner(session: SandboxSession) -> PrizedSandboxSession:
    inner = session._inner
    assert isinstance(inner, PrizedSandboxSession)
    return inner


def _local(fake: FakePrized, session: SandboxSession, path: str) -> Path:
    return Path(fake.sandbox(_inner(session).sandbox_id).local(path))


def _creates(fake: FakePrized) -> list[dict[str, Any]]:
    return [r.json for r in fake.calls("POST", r"^/api/v1/sandboxes$")]


async def _read(session: SandboxSession, path: str) -> bytes:
    handle = await session.read(Path(path))
    with handle:
        data = handle.read()
    assert isinstance(data, bytes)
    return data


# --- options, state, and registration ------------------------------------------


def test_options_and_state_register_with_the_sdk() -> None:
    options = BaseSandboxClientOptions.parse({"type": "prized", "vcpu": 2, "pause_on_exit": True})
    assert isinstance(options, PrizedSandboxClientOptions)
    assert options.vcpu == 2
    assert options.pause_on_exit is True
    assert options.template == "prized-base-v1"
    # Positional arguments follow the field order, as the built-in options do.
    assert PrizedSandboxClientOptions("prized-agent-v1", 4).vcpu == 4
    payload = options.model_dump(mode="json")
    assert payload["type"] == "prized"
    assert BaseSandboxClientOptions.parse(payload) == options


async def test_session_state_round_trips(make_client: Callable[..., PrizedSandboxClient]) -> None:
    client = make_client()
    session = await client.create(
        options=PrizedSandboxClientOptions(env_vars={"TOKEN": "x"}, labels={"run": "1"})
    )
    payload = client.serialize_session_state(session.state)
    assert payload["type"] == "prized"
    assert payload["sandbox_id"] == _inner(session).sandbox_id
    restored = client.deserialize_session_state(dict(payload))
    assert isinstance(restored, PrizedSandboxSessionState)
    assert restored.sandbox_id == _inner(session).sandbox_id
    assert restored.base_env_vars == {"TOKEN": "x"}
    assert isinstance(SandboxSessionState.parse(dict(payload)), PrizedSandboxSessionState)
    await client.delete(session)


def test_client_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="PRIZED_API_KEY") as raised:
        PrizedSandboxClient()
    # A newcomer is sent to sign up, a member to the API keys page.
    assert "https://prized.dev/onboarding/sandboxes" in str(raised.value)
    assert "https://prized.dev/dashboard/workspace/api-keys" in str(raised.value)
    monkeypatch.setenv("PRIZED_API_KEY", "dcp_env")
    with pytest.raises(ValueError, match="https"):
        PrizedSandboxClient(api_url="http://example.com")
    assert PrizedSandboxClient()._client.api_url == "https://api.prized.dev"


# --- create ------------------------------------------------------------------------


async def test_create_maps_the_sdk_default_root(
    make_client: Callable[..., PrizedSandboxClient],
) -> None:
    client = make_client()
    assert client.supports_default_options
    plain = await client.create()
    assert plain.state.manifest.root == DEFAULT_PRIZED_WORKSPACE_ROOT
    sdk_default = await client.create(manifest=Manifest())
    assert sdk_default.state.manifest.root == DEFAULT_PRIZED_WORKSPACE_ROOT
    own = await client.create(manifest=Manifest(root="/home/prized/project"))
    assert own.state.manifest.root == "/home/prized/project"


async def test_create_sends_the_options_and_waits_until_running(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.start_polls = 12  # longer than the create's hold
    client = make_client()
    options = PrizedSandboxClientOptions(
        template="prized-agent-v1",
        vcpu=2,
        memory_mb=4096,
        disk_gb=20,
        env_vars={"A": "1"},
        labels={"run": "7"},
        auto_pause_min=10,
        auto_delete_min=60,
        max_runtime_min=45,
    )
    session = await client.create(options=options)
    assert _creates(fake) == [
        {
            "template": "prized-agent-v1",
            "resources": {"vcpu": 2, "memoryMb": 4096, "diskGb": 20},
            "envVars": {"A": "1"},
            "labels": {"run": "7"},
            "autoPauseMin": 10,
            "autoDeleteMin": 60,
            "maxRuntimeMin": 45,
        }
    ]
    create = fake.calls("POST", r"^/api/v1/sandboxes$")[0]
    assert create.query == {"wait": "running", "waitTimeoutMs": "8000"}
    assert create.headers["idempotency-key"].startswith("openai-agents-prized-")
    assert create.headers["user-agent"].startswith("openai-agents-prized/")
    assert fake.sandbox(_inner(session).sandbox_id).state == "running"
    # A running sandbox takes commands at once: the create sends nothing to
    # its edge, and no warm-up `true` comes before the session's own commands.
    assert fake.commands() == []
    async with session:
        result = await session.exec("echo $A")
    assert result.stdout == b"1\n"
    assert "true" not in fake.commands()


async def test_create_rejects_what_a_sandbox_cannot_do_before_paying(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    with pytest.raises(ConfigurationError, match="users and groups"):
        await client.create(manifest=Manifest(users=[User(name="alice")]))
    with pytest.raises(ConfigurationError, match="mounts are not supported"):
        await client.create(
            manifest=Manifest(
                entries={
                    "data": S3Mount(
                        bucket="b",
                        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                    )
                }
            )
        )
    assert _creates(fake) == []


async def test_create_deletes_a_sandbox_that_fails_to_boot(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.boot_fails = True
    fake.start_polls = 12
    with pytest.raises(PrizedError, match="state error"):
        await make_client().create()
    assert len(fake.calls("DELETE", r"/sandboxes/sbx_")) == 1


async def test_create_passes_api_refusals_through(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.script(
        "POST",
        r"^/api/v1/sandboxes$",
        httpx.Response(
            429, json={"error": "sandbox_quota_exceeded", "reason": "sandbox_limit", "limit": 4}
        ),
    )
    with pytest.raises(PrizedAPIError) as caught:
        await make_client().create()
    assert caught.value.code == "sandbox_quota_exceeded"
    assert "sandbox_limit" in str(caught.value)
    assert len(_creates(fake)) == 1  # never retried


async def test_start_reports_a_root_the_user_cannot_create(
    make_client: Callable[..., PrizedSandboxClient],
) -> None:
    session = await make_client().create(manifest=Manifest(root="/proc/nope"))
    with pytest.raises(WorkspaceStartError, match="cannot create the workspace root"):
        await session.start()


# --- exec ----------------------------------------------------------------------------


async def test_exec_runs_in_the_workspace_with_manifest_env(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    manifest = Manifest(environment=Environment(value={"GREETING": "hi"}))
    client = make_client()
    session = await client.create(
        manifest=manifest,
        options=PrizedSandboxClientOptions(env_vars={"BASE": "b", "GREETING": "base"}),
    )
    async with session:
        result = await session.exec("echo $GREETING $BASE; pwd; echo oops >&2; exit 3")
    assert result.exit_code == 3
    assert result.stdout == b"hi b\n/home/prized/work\n"
    assert result.stderr == b"oops\n"
    last = fake.calls("POST", r"/process/execute$")[-1].json
    assert last["cwd"] == "/home/prized/work"
    assert last["env"] == {"GREETING": "hi"}


async def test_exec_timeouts(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    session = await make_client().create()
    async with session:
        with pytest.raises(ExecTimeoutError):
            await session.exec("sleep 5", timeout=0.2)
        # The edge's own limit is whole seconds: a 1.5 s timeout asks for 2.
        with pytest.raises(ExecTimeoutError):
            await session.exec("sleep 5", timeout=1.5)
    timeouts = [r.json["timeout"] for r in fake.calls("POST", r"/process/execute$")]
    assert 1 in timeouts
    assert 2 in timeouts


async def test_exec_edge_timeout_raises(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    session = await make_client().create()
    async with session:
        fake.script(
            "POST",
            r"/process/execute$",
            httpx.Response(
                200, json={"stdout": "", "stderr": "", "exitCode": -1, "timedOut": True}
            ),
        )
        with pytest.raises(ExecTimeoutError):
            await session.exec("sleep 100")


async def test_exec_transport_errors(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    session = await make_client().create()
    async with session:
        # Lost after it may have run: not resent, retryability unknown.
        fake.script(
            "POST", r"/process/execute$", httpx.Response(502, json={"error": "sandbox_unreachable"})
        )
        with pytest.raises(ExecTransportError) as unreachable:
            await session.exec("echo once")
        assert unreachable.value.retryable is None
        assert unreachable.value.context["provider_error_code"] == "sandbox_unreachable"
        # Proven undelivered: resent by the client, and the command runs once.
        fake.script(
            "POST",
            r"/process/execute$",
            httpx.Response(429, json={"error": "rate_limited"}, headers={"Retry-After": "1"}),
        )
        result = await session.exec("echo twice")
        assert result.stdout == b"twice\n"
        # A refusal that will not pass.
        fake.script(
            "POST",
            r"/process/execute$",
            httpx.Response(409, json={"error": "insufficient_balance"}),
        )
        with pytest.raises(ExecTransportError) as refused:
            await session.exec("true")
        assert refused.value.retryable is False


async def test_exec_as_user(make_client: Callable[..., PrizedSandboxClient]) -> None:
    session = await make_client().create()
    async with session:
        result = await session.exec("echo $USER", user="prized")
        assert result.stdout == b"prized\n"
        with pytest.raises(ConfigurationError, match="running as `root`"):
            await session.exec("id", user="root")


# --- files ---------------------------------------------------------------------------


async def test_write_then_read(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    session = await make_client().create()
    async with session:
        await session.write(Path("a/b/c.bin"), io.BytesIO(b"\x00\xffbinary"))
        assert await _read(session, "a/b/c.bin") == b"\x00\xffbinary"
        assert (
            _local(fake, session, "/home/prized/work/a/b/c.bin").read_bytes() == b"\x00\xffbinary"
        )
        # Staging files are cleaned up.
        stage = _local(fake, session, f"{PRIZED_HOME}/.cache/openai-agents-prized")
        assert not stage.exists() or list(stage.iterdir()) == []


async def test_write_keeps_an_existing_files_mode(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    session = await make_client().create()
    async with session:
        await session.write(Path("run.sh"), io.BytesIO(b"#!/bin/sh\necho v1\n"))
        await session.exec("chmod 755 run.sh")
        await session.write(Path("run.sh"), io.BytesIO(b"#!/bin/sh\necho v2\n"))
        result = await session.exec("./run.sh")
    assert result.stdout == b"v2\n"
    assert _local(fake, session, "/home/prized/work/run.sh").stat().st_mode & 0o777 == 0o755


async def test_read_errors(make_client: Callable[..., PrizedSandboxClient]) -> None:
    session = await make_client().create()
    async with session:
        with pytest.raises(WorkspaceReadNotFoundError):
            await session.read(Path("missing.txt"))
        await session.exec("mkdir adir")
        with pytest.raises(WorkspaceArchiveReadError):
            await session.read(Path("adir"))
        for outside in ("../outside.txt", "/tmp/outside.txt"):
            with pytest.raises(InvalidManifestPathError):
                await session.read(Path(outside))
            with pytest.raises(InvalidManifestPathError):
                await session.write(Path(outside), io.BytesIO(b"x"))


async def test_symlinks_inside_the_workspace_are_followed(
    make_client: Callable[..., PrizedSandboxClient],
) -> None:
    # The file API refuses any symlink; the session copies through a command.
    session = await make_client().create()
    async with session:
        await session.write(Path("real.txt"), io.BytesIO(b"real\n"))
        await session.exec("ln -s real.txt link.txt && ln -s /home/prized/outside.txt escape.txt")
        assert await _read(session, "link.txt") == b"real\n"
        await session.write(Path("link.txt"), io.BytesIO(b"through the link\n"))
        assert await _read(session, "real.txt") == b"through the link\n"
        with pytest.raises(InvalidManifestPathError):
            await session.read(Path("escape.txt"))


async def test_extra_path_grants_outside_the_home(
    make_client: Callable[..., PrizedSandboxClient], tmp_path: Path
) -> None:
    # The file API reaches only /home/prized; a granted path elsewhere goes
    # through a staged copy. (The fake runs commands on this machine, so the
    # grant is a real temporary directory.)
    granted = tmp_path / "granted"
    granted.mkdir()
    (granted / "in.txt").write_bytes(b"from a grant\n")
    manifest = Manifest(extra_path_grants=(SandboxPathGrant(path=str(granted)),))
    session = await make_client().create(manifest=manifest)
    async with session:
        assert await _read(session, str(granted / "in.txt")) == b"from a grant\n"
        await session.write(granted / "out.txt", io.BytesIO(b"written\n"))
        assert (granted / "out.txt").read_bytes() == b"written\n"
        with pytest.raises(WorkspaceReadNotFoundError):
            await session.read(granted / "missing.txt")


async def test_bounded_read_stops_early(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    session = await make_client().create()
    async with session:
        await session.write(Path("big.bin"), io.BytesIO(b"x" * 500_000))
        assert await session.read_bounded(Path("big.bin"), max_bytes=10) == b"x" * 10


async def test_files_larger_than_one_transfer_move_in_parts(
    make_client: Callable[..., PrizedSandboxClient],
    fake: FakePrized,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(files_module, "TRANSFER_PART_BYTES", 4096)
    fake.max_transfer = 4096
    payload = bytes(range(256)) * 80  # 20,480 bytes: five parts
    session = await make_client().create()
    async with session:
        await session.write(Path("big.bin"), io.BytesIO(payload))
        assert _local(fake, session, "/home/prized/work/big.bin").read_bytes() == payload
        # The direct download is refused as too large; the parts are not.
        assert await _read(session, "big.bin") == payload
        assert await session.read_bounded(Path("big.bin"), max_bytes=5000) == payload[:5000]
    sizes = [u.size for u in fake.calls("POST", r"/files/upload$")]
    assert max(sizes) <= 4096
    assert len(sizes) == 5


# --- persist and hydrate ---------------------------------------------------------


async def test_persist_and_hydrate_round_trip_in_parts(
    make_client: Callable[..., PrizedSandboxClient],
    fake: FakePrized,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(files_module, "TRANSFER_PART_BYTES", 4096)
    client = make_client()
    source = await client.create(manifest=Manifest(root="/home/prized"))
    async with source:
        await source.exec("mkdir -p src && head -c 20000 /dev/urandom > src/blob && echo hi > note")
        archive = await source.persist_workspace()
        try:
            payload = archive.read()
        finally:
            archive.close()
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        names = set(tar.getnames())
    assert {"./src/blob", "./note"} <= names
    # The staging directory never ends up in the archive.
    assert not any(".cache/openai-agents-prized" in n for n in names)
    downloads = fake.calls("GET", r"/files/download$")
    assert sum(1 for d in downloads if "/part-" in d.query["path"]) >= 5

    target = await client.create()
    async with target:
        await target.hydrate_workspace(io.BytesIO(payload))
        blob = _local(fake, source, "/home/prized/src/blob").read_bytes()
        assert await _read(target, "src/blob") == blob
        assert await _read(target, "note") == b"hi\n"
    uploads = [u for u in fake.calls("POST", r"/files/upload$") if "/part-" in u.query["path"]]
    assert len(uploads) >= 5


async def test_hydrate_refuses_an_unsafe_archive(
    make_client: Callable[..., PrizedSandboxClient],
) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    session = await make_client().create()
    async with session:
        with pytest.raises(WorkspaceArchiveWriteError):
            await session.hydrate_workspace(io.BytesIO(archive.getvalue()))


# --- shutdown, delete, resume ----------------------------------------------------


async def test_shutdown_deletes_and_pause_on_exit_pauses(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    deleted = await client.create()
    async with deleted:
        pass
    assert fake.sandbox(_inner(deleted).sandbox_id).state in {"deleting", "deleted"}
    await client.delete(deleted)  # nothing left to do
    assert len(fake.calls("DELETE", _inner(deleted).sandbox_id)) == 1

    paused = await client.create(options=PrizedSandboxClientOptions(pause_on_exit=True))
    async with paused:
        pass
    assert fake.sandbox(_inner(paused).sandbox_id).state == "paused"
    assert fake.calls("DELETE", _inner(paused).sandbox_id) == []


async def test_delete_without_close(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create()
    await client.delete(session)
    assert fake.live() == []


async def test_running(make_client: Callable[..., PrizedSandboxClient], fake: FakePrized) -> None:
    session = await make_client().create()
    assert await session.running()
    fake.sandbox(_inner(session).sandbox_id).state = "paused"
    assert not await session.running()


async def test_resume_wakes_a_paused_sandbox_with_its_files(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(pause_on_exit=True))
    async with session:
        await session.write(Path("kept.txt"), io.BytesIO(b"kept\n"))
    sandbox_id = _inner(session).sandbox_id
    assert fake.sandbox(sandbox_id).state == "paused"

    state = client.deserialize_session_state(client.serialize_session_state(session.state))
    before = fake.commands()
    resumed = await client.resume(state)
    assert fake.commands() == before  # running takes commands at once: no warm-up `true`
    assert _inner(resumed).sandbox_id == sandbox_id
    assert _inner(resumed)._workspace_state_preserved_on_start()
    assert fake.calls("POST", rf"{sandbox_id}/resume$")
    async with resumed:
        assert await _read(resumed, "kept.txt") == b"kept\n"
    assert len(_creates(fake)) == 1


@pytest.mark.parametrize("gone", ["deleted", "error"])
async def test_resume_replaces_a_sandbox_that_is_gone(
    make_client: Callable[..., PrizedSandboxClient],
    fake: FakePrized,
    tmp_path: Path,
    gone: str,
) -> None:
    client = make_client()
    snapshot = LocalSnapshotSpec(base_path=tmp_path / "snapshots")
    session = await client.create(snapshot=snapshot)
    async with session:
        await session.write(Path("kept.txt"), io.BytesIO(b"from the snapshot\n"))
    old_id = _inner(session).sandbox_id
    if gone == "error":
        fake.sandbox(old_id).state = "error"

    state = client.deserialize_session_state(client.serialize_session_state(session.state))
    resumed = await client.resume(state)
    new_id = _inner(resumed).sandbox_id
    assert new_id != old_id
    assert not _inner(resumed)._workspace_state_preserved_on_start()
    async with resumed:
        assert await _read(resumed, "kept.txt") == b"from the snapshot\n"
    assert len(_creates(fake)) == 2


async def test_resume_does_not_replace_on_a_transient_failure(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client(max_retries=0)
    session = await client.create(options=PrizedSandboxClientOptions(pause_on_exit=True))
    async with session:
        pass
    fake.script(
        "GET",
        r"/api/v1/sandboxes/sbx_",
        httpx.Response(503, json={"error": "control_plane_unavailable"}),
    )
    with pytest.raises(PrizedAPIError):
        await client.resume(session.state)
    assert len(_creates(fake)) == 1


async def test_resume_rejects_another_backends_state(
    make_client: Callable[..., PrizedSandboxClient],
) -> None:
    with pytest.raises(TypeError):
        await make_client().resume(
            SandboxSessionState.model_construct(type="other")  # type: ignore[arg-type]
        )


async def test_timeouts_validate(make_client: Callable[..., PrizedSandboxClient]) -> None:
    with pytest.raises(ValueError, match="3600"):
        PrizedSandboxTimeouts(exec_timeout_unbounded_s=7200)
    # The options take a mapping as it is (pydantic keeps the dict member of
    # the union); the session state holds the validated model.
    options = PrizedSandboxClientOptions(timeouts={"start_s": 60})
    assert options.timeouts in ({"start_s": 60}, PrizedSandboxTimeouts(start_s=60))
    session = await make_client().create(options=options)
    assert session.state.timeouts == PrizedSandboxTimeouts(start_s=60)
    with pytest.raises(ValueError, match="3600"):
        await make_client().create(
            options=PrizedSandboxClientOptions(timeouts={"exec_timeout_unbounded_s": 7200})
        )


def test_api_origin_constant() -> None:
    # The fake answers on Prized's own domain, so the edge is trusted.
    assert API_ORIGIN.endswith("prized.dev")


# --- review round 1 -----------------------------------------------------------------


async def test_a_staged_read_that_fails_is_an_error_not_a_short_file(
    make_client: Callable[..., PrizedSandboxClient], tmp_path: Path
) -> None:
    granted = tmp_path / "granted"
    granted.mkdir()
    secret = granted / "unreadable.txt"
    secret.write_bytes(b"x" * 1000)
    secret.chmod(0)
    manifest = Manifest(extra_path_grants=(SandboxPathGrant(path=str(granted)),))
    session = await make_client().create(manifest=manifest)
    try:
        async with session:
            with pytest.raises(WorkspaceArchiveReadError):
                await session.read(secret)
    finally:
        secret.chmod(0o600)


async def test_delete_raises_when_the_sandbox_is_still_there(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client(max_retries=0)
    session = await client.create()
    fake.script(
        "DELETE", r"/sandboxes/sbx_", httpx.Response(503, json={"error": "sandboxes_disabled"}), 2
    )
    async with session:
        pass  # the shutdown's delete fails, and is logged
    with pytest.raises(PrizedAPIError):
        await client.delete(session)
    await client.delete(session)  # the API answers again: deleted
    assert fake.live() == []


async def test_a_cancelled_create_deletes_what_the_api_made(
    fake: FakePrized, clock: FakeClock
) -> None:
    accepted = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handle(request)
        if request.method == "POST" and request.url.path == "/api/v1/sandboxes":
            accepted.set()
            await release.wait()  # the answer is still on its way back
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PrizedSandboxClient(api_key=API_KEY, api_url=API_ORIGIN, http_client=http)
        client._client.sleep = clock.sleep
        task = asyncio.create_task(client.create())
        await accepted.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(50):
            if fake.live() == []:
                break
            await asyncio.sleep(0.01)
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


async def test_resume_replaces_a_sandbox_deleted_while_it_resumes(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(pause_on_exit=True))
    async with session:
        pass
    old_id = _inner(session).sandbox_id
    fake.script("POST", r"/resume$", httpx.Response(404, json={"error": "not_found"}))
    resumed = await client.resume(session.state)
    assert _inner(resumed).sandbox_id != old_id
    assert len(_creates(fake)) == 2
