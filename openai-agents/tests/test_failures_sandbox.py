"""Files, snapshots, and background processes, against every way each can fail.

The rest of the failure table in FAILURES.md: a path is
judged on what is actually opened (a link swapped in after the SDK's check
changes nothing), staged parts never outlive a failed or cancelled transfer,
and a background process this session may have started is always ended.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tarfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from agents.sandbox.errors import (
    ExecTransportError,
    InvalidManifestPathError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
)
from agents.sandbox.snapshot import LocalSnapshotSpec

import openai_agents_prized._files as files_module
from openai_agents_prized import PrizedSandboxClientOptions, PrizedSandboxSession
from openai_agents_prized._core import STAGE_ROOT
from openai_agents_prized._pty import PTY_ROOT, PtyProcess
from openai_agents_prized._scripts import PTY_START
from tests.conftest import Gate, settle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from agents.sandbox.session import SandboxSession

    from openai_agents_prized import PrizedSandboxClient
    from tests.fake_prized import FakePrized, FakeSandbox

EXEC = r"/process/execute$"
WRITE_JOIN = "openai-agents-prized-$$"
"""Text only the staged write's join script has."""
READ_COPY = "/proc/$$/fd/3"
"""Text only the staged read's copy script has."""


def _inner(session: SandboxSession) -> PrizedSandboxSession:
    inner = session._inner
    assert isinstance(inner, PrizedSandboxSession)
    return inner


def _local(fake: FakePrized, session: SandboxSession, path: str) -> Path:
    return Path(fake.sandbox(_inner(session).sandbox_id).local(path))


def _staged(fake: FakePrized, session: SandboxSession) -> list[Path]:
    stage = _local(fake, session, STAGE_ROOT)
    return list(stage.iterdir()) if stage.exists() else []


async def _read(session: SandboxSession, path: str) -> bytes:
    handle = await session.read(Path(path))
    with handle:
        data = handle.read()
    assert isinstance(data, bytes)
    return data


def _sleeper() -> str:
    """A command line no other process on this machine has."""
    return f"sleep 900.{uuid.uuid4().int % 10**9}"


def _running(needle: str) -> list[int]:
    """Pids on this machine whose command line is exactly `needle`'s words."""
    want = needle.encode().split()
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")[:-1]
            state = (entry / "stat").read_text().rsplit(") ", 1)[1][:1]
        except (OSError, IndexError):
            continue
        if argv == want and state not in {"Z", "X"}:
            pids.append(int(entry.name))
    return pids


async def _none_running(needle: str) -> bool:
    for _ in range(100):
        if not _running(needle):
            return True
        await asyncio.sleep(0.02)
    return False


def _tar(members: list[tarfile.TarInfo], data: dict[str, bytes] | None = None) -> io.BytesIO:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for info in members:
            payload = (data or {}).get(info.name)
            if payload is not None:
                info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload) if payload is not None else None)
    out.seek(0)
    return out


def _block_input(process_dir: str) -> None:
    """Make the process's input FIFO a directory: nothing can be written there now."""
    fifo = Path(process_dir, "in")
    fifo.unlink()
    fifo.mkdir()


def _link(name: str, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info


@pytest.fixture
async def session(make_client: Callable[..., PrizedSandboxClient]) -> AsyncIterator[SandboxSession]:
    created = await make_client().create()
    async with created:
        yield created


# --- write -----------------------------------------------------------------------------


async def test_write_never_follows_a_link_swapped_in_after_the_check(
    session: SandboxSession, fake: FakePrized
) -> None:
    await session.write(Path("target.txt"), io.BytesIO(b"v1\n"))
    outside = _local(fake, session, "/home/prized/outside.txt")
    outside.write_bytes(b"keep\n")
    target = _local(fake, session, "/home/prized/work/target.txt")

    def swap(_: FakeSandbox) -> None:  # after the SDK's check, before the join
        target.unlink()
        target.symlink_to(outside)

    fake.before_exec(WRITE_JOIN, swap)
    with pytest.raises(InvalidManifestPathError):
        await session.write(Path("target.txt"), io.BytesIO(b"through the link\n"))
    assert outside.read_bytes() == b"keep\n"
    assert _staged(fake, session) == []


async def test_write_never_follows_a_directory_swapped_for_a_link(
    session: SandboxSession, fake: FakePrized
) -> None:
    await session.write(Path("sub/first.txt"), io.BytesIO(b"1\n"))
    elsewhere = _local(fake, session, "/home/prized/elsewhere")
    elsewhere.mkdir()
    sub = _local(fake, session, "/home/prized/work/sub")

    def swap(_: FakeSandbox) -> None:
        shutil.rmtree(sub)
        sub.symlink_to(elsewhere)

    fake.before_exec(WRITE_JOIN, swap)
    with pytest.raises(InvalidManifestPathError):
        await session.write(Path("sub/second.txt"), io.BytesIO(b"2\n"))
    assert list(elsewhere.iterdir()) == []


async def test_write_never_lands_in_a_sibling_named_like_the_root_plus_a_newline(
    session: SandboxSession, fake: FakePrized
) -> None:
    # `$(pwd -P)` would cut the newline and judge `/home/prized/work/second.txt`.
    await session.write(Path("sub/first.txt"), io.BytesIO(b"1\n"))
    sibling = _local(fake, session, "/home/prized/work\n")
    sibling.mkdir()
    sub = _local(fake, session, "/home/prized/work/sub")

    def swap(_: FakeSandbox) -> None:
        shutil.rmtree(sub)
        sub.symlink_to(sibling)

    fake.before_exec(WRITE_JOIN, swap)
    with pytest.raises(InvalidManifestPathError):
        await session.write(Path("sub/second.txt"), io.BytesIO(b"2\n"))
    assert list(sibling.iterdir()) == []


async def test_write_replaces_a_hard_link_instead_of_writing_through_it(
    session: SandboxSession, fake: FakePrized
) -> None:
    outside = _local(fake, session, "/home/prized/outside.txt")
    outside.write_bytes(b"keep\n")
    os.link(outside, _local(fake, session, "/home/prized/work/hard.txt"))
    await session.write(Path("hard.txt"), io.BytesIO(b"new\n"))
    assert outside.read_bytes() == b"keep\n"
    assert await _read(session, "hard.txt") == b"new\n"


async def test_write_through_a_link_to_a_name_ending_in_a_newline_is_refused(
    session: SandboxSession, fake: FakePrized
) -> None:
    # The SDK's resolver cuts trailing newlines, so it would judge `t`, not
    # `t\n`: the write is refused rather than approved for another name.
    target = _local(fake, session, "/home/prized/work/t\n")
    target.write_bytes(b"old\n")
    _local(fake, session, "/home/prized/work/link").symlink_to("t\n")
    with pytest.raises(InvalidManifestPathError):
        await session.write(Path("link"), io.BytesIO(b"new\n"))
    assert target.read_bytes() == b"old\n"
    assert not _local(fake, session, "/home/prized/work/t").exists()


async def test_write_cancelled_midway_leaves_no_staged_parts(
    fake: FakePrized,
    gated: Callable[..., PrizedSandboxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(files_module, "TRANSFER_PART_BYTES", 4096)
    gate = Gate(
        fake,
        lambda r: (
            r.url.path.endswith("/files/upload")
            and r.url.params.get("path", "").endswith("part-0001")
        ),
    )
    client = gated(gate)
    session = await client.create()
    async with session:
        task = asyncio.create_task(session.write(Path("big.bin"), io.BytesIO(b"x" * 10_000)))
        await gate.reached.wait()
        task.cancel()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle(client)
        assert _staged(fake, session) == []
        assert not _local(fake, session, "/home/prized/work/big.bin").exists()


async def test_write_whose_upload_fails_leaves_no_staged_parts(
    session: SandboxSession, fake: FakePrized, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_module, "TRANSFER_PART_BYTES", 4096)
    fake.max_transfer = 4096
    real_upload = fake._upload
    seen: list[str] = []

    def upload_then_fail(sandbox: FakeSandbox, raw: str | None, body: bytes) -> object:
        seen.append(raw or "")
        if len(seen) == 2:
            fake.max_transfer = 1  # the second part is refused: 413
        return real_upload(sandbox, raw, body)

    monkeypatch.setattr(fake, "_upload", upload_then_fail)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.write(Path("big.bin"), io.BytesIO(b"x" * 10_000))
    assert _staged(fake, session) == []


# --- read ------------------------------------------------------------------------------


async def test_read_never_returns_a_file_a_swapped_link_points_out_to(
    session: SandboxSession, fake: FakePrized
) -> None:
    await session.write(Path("real.txt"), io.BytesIO(b"real\n"))
    secret = _local(fake, session, "/home/prized/secret.txt")
    secret.write_bytes(b"secret\n")
    link = _local(fake, session, "/home/prized/work/link.txt")
    link.symlink_to("real.txt")
    assert await _read(session, "link.txt") == b"real\n"  # a link inside is fine

    def swap(_: FakeSandbox) -> None:
        link.unlink()
        link.symlink_to(secret)

    fake.before_exec(READ_COPY, swap)
    with pytest.raises(InvalidManifestPathError):
        await _read(session, "link.txt")
    assert _staged(fake, session) == []


def _helper_that(tmp_path: Path, helper: str, before: str, after: str = "") -> str:
    """A resolver that lets a process in the sandbox act around the read's own check."""
    wrapper = tmp_path / f"resolver-{uuid.uuid4().hex[:8]}"
    wrapper.write_text(f"""#!/bin/sh
{before}
"{helper}" "$@"
code=$?
{after}
exit $code
""")
    wrapper.chmod(0o755)
    return str(wrapper)


async def _check_reads_with(
    session: SandboxSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    before: str,
    after: str,
) -> None:
    inner = _inner(session)
    helper, root, grants = await inner._path_rules()
    wrapper = _helper_that(tmp_path, helper, before, after)

    async def rules() -> tuple[str, str, list[str]]:
        return wrapper, root, grants

    monkeypatch.setattr(inner, "_path_rules", rules)


async def test_read_refuses_a_file_moved_out_while_it_is_checked(
    session: SandboxSession, fake: FakePrized, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await session.write(Path("x/notes.txt"), io.BytesIO(b"private\n"))
    _local(fake, session, "/home/prized/work/link.txt").symlink_to("x/notes.txt")  # staged
    work_x = _local(fake, session, "/home/prized/work/x")
    private = _local(fake, session, "/home/prized/private")
    # Opened and located inside the workspace; while the resolver checks
    # that place, the file is moved out, and a decoy takes its name.
    await _check_reads_with(
        session,
        monkeypatch,
        tmp_path,
        before=(
            f'mv "{work_x}" "{private}" && mkdir "{work_x}" && echo decoy > "{work_x}/notes.txt"'
        ),
        after="",
    )
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await _read(session, "link.txt")
    assert caught.value.context.get("reason") == "moved_while_checked"
    assert _staged(fake, session) == []


async def test_read_refuses_a_path_a_link_redirects_while_it_is_checked(
    session: SandboxSession, fake: FakePrized, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await session.write(Path("x/notes.txt"), io.BytesIO(b"decoy\n"))
    private = _local(fake, session, "/home/prized/private")
    private.mkdir()
    (private / "notes.txt").write_bytes(b"private\n")
    link = _local(fake, session, "/home/prized/work/link.txt")
    link.symlink_to("x/notes.txt")
    work_x = _local(fake, session, "/home/prized/work/x")

    def swap(_: FakeSandbox) -> None:  # after the SDK's own check
        link.unlink()
        link.symlink_to(private / "notes.txt")

    fake.before_exec(READ_COPY, swap)
    # The file opened is outside. For the check only, its directory becomes a
    # link into the workspace, so its path resolves to the decoy.
    moved = private.with_name("private.held")
    await _check_reads_with(
        session,
        monkeypatch,
        tmp_path,
        before=f'mv "{private}" "{moved}" && ln -s "{work_x}" "{private}"',
        after=f'rm "{private}" && mv "{moved}" "{private}"',
    )
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await _read(session, "link.txt")
    assert caught.value.context.get("reason") == "moved_while_checked"
    assert (private / "notes.txt").read_bytes() == b"private\n"


# --- snapshots ---------------------------------------------------------------------------


async def test_snapshot_stores_links_as_links(session: SandboxSession, fake: FakePrized) -> None:
    secret = _local(fake, session, "/home/prized/secret.txt")
    secret.write_bytes(b"secret\n")
    _local(fake, session, "/home/prized/work/out").symlink_to(secret)
    archive = await session.persist_workspace()
    with archive, tarfile.open(fileobj=archive) as tar:
        member = tar.getmember("./out")
        assert member.issym()
        for info in tar.getmembers():
            if info.isreg():
                extracted = tar.extractfile(info)
                assert extracted is not None
                assert b"secret" not in extracted.read()


async def test_a_virtualenv_comes_back_in_a_new_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized, tmp_path: Path
) -> None:
    client = make_client()
    snapshot = LocalSnapshotSpec(base_path=tmp_path / "snapshots")
    session = await client.create(snapshot=snapshot)
    async with session:
        made = await session.exec("python3 -m venv --without-pip venv && venv/bin/python -c 1")
        assert made.ok(), made.stderr
        # The interpreter link points outside the workspace, as venv makes it.
        linked = await session.exec("find venv/bin -type l -lname '/*' -printf '%p %l\\n'")
        assert linked.stdout.strip(), "venv made no link to an absolute path"
    # Deleted at the end of the run; the resume makes a new sandbox and
    # restores the snapshot, the interpreter link included.
    state = client.deserialize_session_state(client.serialize_session_state(session.state))
    resumed = await client.resume(state)
    assert _inner(resumed).sandbox_id != _inner(session).sandbox_id
    async with resumed:
        ran = await resumed.exec("venv/bin/python -c 'print(6 * 7)'")
        assert ran.stdout == b"42\n", ran.stderr
        relinked = await resumed.exec("find venv/bin -type l -lname '/*' -printf '%p %l\\n'")
        assert relinked.stdout == linked.stdout


async def test_a_snapshot_with_hard_links_restores(
    make_client: Callable[..., PrizedSandboxClient],
) -> None:
    client = make_client()
    source = await client.create()
    async with source:
        made = await source.exec("printf one > a.txt && ln a.txt b.txt")
        assert made.ok(), made.stderr
        archive = await source.persist_workspace()
        with archive:
            payload = archive.read()
    target = await client.create()
    async with target:
        await target.hydrate_workspace(io.BytesIO(payload))
        assert await _read(target, "a.txt") == b"one"
        assert await _read(target, "b.txt") == b"one"


async def test_cleanup_that_arrives_after_release_leaves_the_sandbox_paused(
    fake: FakePrized, gated: Callable[..., PrizedSandboxClient]
) -> None:
    def staged_read_cleanup(request: httpx.Request) -> bool:
        body = request.content
        return (
            request.url.path.endswith("/process/execute") and b"rm -rf" in body and b"-read" in body
        )

    gate = Gate(fake, staged_read_cleanup, in_flight=True)
    gate.armed = False
    client = gated(gate)
    session = await client.create(options=PrizedSandboxClientOptions(pause_on_exit=True))
    sandbox_id = _inner(session).sandbox_id
    await session.start()
    await session.write(Path("real.txt"), io.BytesIO(b"real\n"))
    _local(fake, session, "/home/prized/work/link.txt").symlink_to("real.txt")
    gate.armed = True
    read = asyncio.create_task(_read(session, "link.txt"))  # staged: the file API refuses links
    await gate.reached.wait()  # read; its cleanup is on its way
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    await client.delete(session)  # pause_on_exit: paused
    assert fake.sandbox(sandbox_id).state == "paused"
    gate.release.set()  # the cleanup reaches the edge only now
    await settle(client)
    assert fake.sandbox(sandbox_id).state == "paused"
    cleanup = [r for r in fake.calls("POST", EXEC) if staged_read_cleanup_cmd(r.json)]
    assert cleanup
    assert cleanup[-1].headers.get("prized-auto-resume") == "false"


def staged_read_cleanup_cmd(doc: object) -> bool:
    command = doc.get("command", "") if isinstance(doc, dict) else ""
    return "rm -rf" in command and "-read" in command


async def test_hydrate_refuses_a_member_under_a_link_of_its_own(
    session: SandboxSession, fake: FakePrized
) -> None:
    uploads = len(fake.calls("POST", r"/files/upload$"))
    archive = _tar([_link("d", "/tmp"), tarfile.TarInfo("d/x")], {"d/x": b"x"})
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(archive)
    assert len(fake.calls("POST", r"/files/upload$")) == uploads  # refused before upload


async def test_hydrate_never_writes_through_a_link_already_in_the_workspace(
    session: SandboxSession, fake: FakePrized
) -> None:
    outside = _local(fake, session, "/home/prized/outside")
    outside.mkdir()
    _local(fake, session, "/home/prized/work/d").symlink_to(outside)
    # Only `d/f`, no `d/` entry: the archive alone looks harmless.
    archive = _tar([tarfile.TarInfo("d/f")], {"d/f": b"f"})
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(archive)
    assert list(outside.iterdir()) == []
    assert _staged(fake, session) == []


async def test_snapshots_refuse_a_workspace_root_swapped_for_a_link(
    session: SandboxSession, fake: FakePrized
) -> None:
    await session.write(Path("a.txt"), io.BytesIO(b"a\n"))
    private = _local(fake, session, "/home/prized/private")
    private.mkdir()
    (private / "secret.txt").write_bytes(b"secret\n")
    work = _local(fake, session, "/home/prized/work")
    work.rename(work.with_name("work.moved"))
    work.symlink_to(private)
    with pytest.raises(WorkspaceArchiveReadError) as read_error:
        await session.persist_workspace()
    assert read_error.value.context.get("reason") == "workspace_root_is_a_link"
    archive = _tar([tarfile.TarInfo("planted.txt")], {"planted.txt": b"planted\n"})
    with pytest.raises(WorkspaceArchiveWriteError) as write_error:
        await session.hydrate_workspace(archive)
    assert write_error.value.context.get("reason") == "workspace_root_is_a_link"
    assert sorted(p.name for p in private.iterdir()) == ["secret.txt"]
    assert _staged(fake, session) == []


async def test_snapshots_refuse_a_root_linked_to_a_sibling_named_like_it_plus_a_newline(
    session: SandboxSession, fake: FakePrized
) -> None:
    # `$(pwd -P)` would cut the newline, so the sibling would pass for the root.
    await session.write(Path("a.txt"), io.BytesIO(b"a\n"))
    sibling = _local(fake, session, "/home/prized/work\n")
    sibling.mkdir()
    (sibling / "secret.txt").write_bytes(b"secret\n")
    work = _local(fake, session, "/home/prized/work")
    work.rename(work.with_name("work.moved"))
    work.symlink_to(sibling)
    with pytest.raises(WorkspaceArchiveReadError) as read_error:
        await session.persist_workspace()
    assert read_error.value.context.get("reason") == "workspace_root_is_a_link"
    archive = _tar([tarfile.TarInfo("planted.txt")], {"planted.txt": b"planted\n"})
    with pytest.raises(WorkspaceArchiveWriteError) as write_error:
        await session.hydrate_workspace(archive)
    assert write_error.value.context.get("reason") == "workspace_root_is_a_link"
    assert sorted(p.name for p in sibling.iterdir()) == ["secret.txt"]
    assert _staged(fake, session) == []


# --- background processes ----------------------------------------------------------------


async def test_start_whose_answer_is_lost_kills_the_process(
    session: SandboxSession, fake: FakePrized
) -> None:
    fake.lose("POST", EXEC, command="mkfifo")
    sleeper = _sleeper()
    with pytest.raises(ExecTransportError):
        await session.pty_exec_start(f"exec {sleeper}", yield_time_s=0.3)
    assert await _none_running(sleeper)
    assert _inner(session)._pty_unconfirmed == {}


async def test_lost_start_whose_kill_fails_is_ended_by_terminate_all(
    session: SandboxSession, fake: FakePrized
) -> None:
    fake.lose("POST", EXEC, command="mkfifo")
    fake.script(
        "POST",
        EXEC,
        httpx.Response(502, json={"error": "sandbox_unreachable"}),
        command="task/*/children",
    )
    sleeper = _sleeper()
    with pytest.raises(ExecTransportError):
        await session.pty_exec_start(f"exec {sleeper}", yield_time_s=0.3)
    assert len(_inner(session)._pty_unconfirmed) == 1  # still tracked
    await session.pty_terminate_all()
    assert _inner(session)._pty_unconfirmed == {}
    assert await _none_running(sleeper)


async def test_a_kill_that_arrives_before_its_start_stops_the_start(
    session: SandboxSession,
) -> None:
    inner = _inner(session)
    entry = PtyProcess(
        dir=f"{PTY_ROOT}/{inner.state.session_id.hex}/late-{uuid.uuid4().hex}", tty=False
    )
    await inner._terminate_pty_processes([entry])  # the kill came first
    sleeper = _sleeper()
    started = await inner._run(
        ["bash", "-c", PTY_START, "bash", entry.dir, f"exec {sleeper}", "0"], timeout=30
    )
    assert not started.ok()
    assert await _none_running(sleeper)


async def test_input_that_cannot_be_delivered_is_an_error(session: SandboxSession) -> None:
    started = await session.pty_exec_start("sleep 30", tty=True, yield_time_s=0.3)
    assert started.process_id is not None
    _block_input(_inner(session)._pty_processes[started.process_id].dir)
    with pytest.raises(ExecTransportError) as caught:
        await session.pty_write_stdin(session_id=started.process_id, chars="lost\n")
    assert caught.value.context["reason"] == "stdin_not_delivered"


async def test_input_to_a_process_that_exited_returns_its_exit(session: SandboxSession) -> None:
    started = await session.pty_exec_start("sleep 0.3; exit 3", tty=True, yield_time_s=0.05)
    assert started.process_id is not None
    process_dir = _inner(session)._pty_processes[started.process_id].dir
    await asyncio.sleep(0.6)
    _block_input(process_dir)
    update = await session.pty_write_stdin(session_id=started.process_id, chars="late\n")
    assert update.process_id is None
    assert update.exit_code == 3


async def test_concurrent_polls_return_each_byte_once(session: SandboxSession) -> None:
    script = "for i in $(seq 1 40); do echo line$i; sleep 0.02; done"
    started = await session.pty_exec_start(script, yield_time_s=0.1)
    assert started.process_id is not None
    output = started.output
    done = False
    while not done:
        updates = await asyncio.gather(
            session.pty_write_stdin(session_id=started.process_id, chars="", yield_time_s=0.3),
            session.pty_write_stdin(session_id=started.process_id, chars="", yield_time_s=0.3),
        )
        for update in updates:
            output += update.output
            done = done or update.process_id is None
    assert output == b"".join(f"line{i}\n".encode() for i in range(1, 41))
