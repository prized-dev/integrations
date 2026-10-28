"""The Agents SDK's own end-to-end sandbox lifecycle scenario, for Prized.

Ported from openai-agents-python's
`tests/sandbox/integration_tests/test_runner_pause_resume.py` and its
`_helpers.py` (the test the SDK runs against its Unix-local backend), with
what a Prized sandbox cannot do taken out:

- no mounts (no root, so no FUSE): the manifest has no mount entries;
- the symlink escape is made inside the sandbox with `ln -s`, since the
  sandbox's files are not on this machine.

Everything else is the SDK's: a `SandboxAgent` driven by scripted model turns
materializes a manifest with every other entry type (inline files, an
ephemeral file, a directory tree, a local file, a local directory, a git
repo), writes a file, applies a patch, checks that reads, writes, and patches
outside the workspace are refused, extracts an archive, talks to a process
through a TTY, and stops on a tool that needs approval. The run state goes
through JSON; the resumed run gets the approval and checks that every file
came back.
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Mapping  # noqa: TC003  # pydantic reads RepoFixture's fields
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents import RunConfig, Runner, function_tool
from agents.editor import ApplyPatchOperation
from agents.items import RunItem, ToolCallOutputItem
from agents.run_state import RunState
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Capability
from agents.sandbox.entries import Dir, File, GitRepo, LocalDir, LocalFile
from agents.sandbox.errors import (
    ApplyPatchPathError,
    InvalidManifestPathError,
    WorkspaceReadNotFoundError,
)
from agents.sandbox.files import EntryKind
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import CallbackSink, Instrumentation, SandboxSessionEvent
from agents.sandbox.workspace_paths import SandboxPathGrant
from agents.testing import ScriptedModel, assistant_message, function_call

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
    from agents.tool import Tool

    from openai_agents_prized import PrizedSandboxClient, PrizedSandboxClientOptions

DURABLE_WORKSPACE_TEXTS = {
    "inline.txt": "inline file v1\n",
    "delete_me.txt": "delete me v1\n",
    "tree/nested.txt": "nested file v1\n",
    "copied_file.txt": "local file source v1\n",
    "copied_dir/child.txt": "local dir child v1\n",
    "copied_dir/nested/grandchild.txt": "local dir grandchild v1\n",
}
EPHEMERAL_WORKSPACE_TEXTS = {"tree/ephemeral.txt": "ephemeral file v1\n"}
ARCHIVE_WORKSPACE_TEXTS = {"archive_dir/hello.txt": "hello from tar archive\n"}
RUNTIME_WORKSPACE_TEXTS = {"runtime_note.txt": "runtime note v1\n"}
PATCHED_WORKSPACE_TEXTS = {
    "inline.txt": "inline file v2\n",
    "created_by_patch.txt": "created by patch",
}
RESTORED_WORKSPACE_DIRS = {"archive_dir", "copied_dir", "copied_dir/nested", "repo", "tree"}
RESTORED_WORKSPACE_FILES = {
    "archive_dir/hello.txt",
    "bundle.tar",
    "copied_dir/child.txt",
    "copied_dir/nested/grandchild.txt",
    "copied_file.txt",
    "created_by_patch.txt",
    "inline.txt",
    "runtime_note.txt",
    "tree/ephemeral.txt",
    "tree/nested.txt",
}


@dataclass
class RepoFixture:
    """The git repo the manifest clones, and what it holds."""

    repo: str
    ref: str
    texts: Mapping[str, str]
    dirs: frozenset[str] = frozenset()


MOCK_REPO = RepoFixture(
    repo="openai/mock-sandbox-fixture",
    ref="main",
    texts={
        "README.md": "mock git repo readme v1\n",
        "pkg/module.py": "VALUE = 'mock git module v1'\n",
    },
    dirs=frozenset({"pkg"}),
)
"""The SDK's own fixture; its `git` and `cp` are the mocks in `mock_tools`."""

PUBLIC_REPO = RepoFixture(
    repo="octocat/Hello-World", ref="master", texts={"README": "Hello World!\n"}
)
"""A real, tiny public repository, cloned for real in a live sandbox."""


def create_local_sources(tmp_path: Path) -> Path:
    source_root = tmp_path / "manifest-sources"
    nested_dir = source_root / "local-dir" / "nested"
    nested_dir.mkdir(parents=True)
    (source_root / "local-file.txt").write_text("local file source v1\n", encoding="utf-8")
    (source_root / "local-dir" / "child.txt").write_text("local dir child v1\n", encoding="utf-8")
    (nested_dir / "grandchild.txt").write_text("local dir grandchild v1\n", encoding="utf-8")
    return source_root


def build_manifest(*, source_root: Path, repo: RepoFixture) -> Manifest:
    # The SDK's default root: the client maps it to /home/prized/work.
    return Manifest(
        extra_path_grants=(SandboxPathGrant(path=str(source_root)),),
        entries={
            "inline.txt": File(content=DURABLE_WORKSPACE_TEXTS["inline.txt"].encode()),
            "delete_me.txt": File(content=DURABLE_WORKSPACE_TEXTS["delete_me.txt"].encode()),
            "tree": Dir(
                children={
                    "nested.txt": File(content=DURABLE_WORKSPACE_TEXTS["tree/nested.txt"].encode()),
                    "ephemeral.txt": File(
                        content=EPHEMERAL_WORKSPACE_TEXTS["tree/ephemeral.txt"].encode(),
                        ephemeral=True,
                    ),
                }
            ),
            "copied_file.txt": LocalFile(src=source_root / "local-file.txt"),
            "copied_dir": LocalDir(src=source_root / "local-dir"),
            "repo": GitRepo(repo=repo.repo, ref=repo.ref),
        },
    )


async def read_text(session: BaseSandboxSession, path: str | Path) -> str:
    handle = await session.read(Path(path))
    try:
        payload = handle.read()
    finally:
        handle.close()
    return payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)


async def write_text(session: BaseSandboxSession, path: str | Path, text: str) -> None:
    await session.write(Path(path), io.BytesIO(text.encode("utf-8")))


async def assert_texts(session: BaseSandboxSession, expected: Mapping[str, str]) -> None:
    actual = {path: await read_text(session, path) for path in expected}
    assert actual == dict(expected)


async def assert_missing(session: BaseSandboxSession, path: str) -> None:
    try:
        await read_text(session, path)
    except WorkspaceReadNotFoundError:
        return
    msg = f"Expected workspace path to be missing: {path}"
    raise AssertionError(msg)


def _repo_texts(repo: RepoFixture) -> dict[str, str]:
    return {f"repo/{path}": text for path, text in repo.texts.items()}


def _tar_bytes(members: Mapping[str, str]) -> io.BytesIO:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, text in members.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    archive.seek(0)
    return archive


def _lifecycle_patch() -> list[ApplyPatchOperation | dict[str, object]]:
    return [
        ApplyPatchOperation(
            type="update_file", path="inline.txt", diff="@@\n-inline file v1\n+inline file v2\n"
        ),
        ApplyPatchOperation(
            type="create_file", path="created_by_patch.txt", diff="+created by patch\n"
        ),
        ApplyPatchOperation(type="delete_file", path="delete_me.txt"),
    ]


async def _blocked(action: Any, *errors: type[BaseException]) -> bool:
    try:
        await action()
    except errors:
        return True
    return False


class SandboxFileCapability(Capability):
    type: str = "sandbox-file"

    def __init__(self) -> None:
        super().__init__(type="sandbox-file")

    def tools(self) -> list[Tool]:
        @function_tool(name_override="write_file", failure_error_function=None)
        async def write_file(path: str, content: str) -> str:
            assert self.session is not None
            await write_text(self.session, path, content)
            return f"wrote {path}"

        @function_tool(name_override="read_file", failure_error_function=None)
        async def read_file(path: str) -> str:
            assert self.session is not None
            return await read_text(self.session, path)

        return [write_file, read_file]


class SandboxLifecycleProbeCapability(Capability):
    type: str = "sandbox-lifecycle-probe"
    repo: RepoFixture = MOCK_REPO

    def __init__(self, repo: RepoFixture) -> None:
        super().__init__(type="sandbox-lifecycle-probe", repo=repo)

    def tools(self) -> list[Tool]:  # noqa: PLR0915  # one tool per SDK lifecycle step
        repo = self.repo

        @function_tool(name_override="assert_manifest_materialized", failure_error_function=None)
        async def assert_manifest_materialized() -> str:
            session = self._session()
            await assert_texts(session, DURABLE_WORKSPACE_TEXTS)
            await assert_texts(session, EPHEMERAL_WORKSPACE_TEXTS)
            await assert_texts(session, _repo_texts(repo))
            return "manifest materialized"

        @function_tool(name_override="apply_lifecycle_patch", failure_error_function=None)
        async def apply_lifecycle_patch() -> str:
            session = self._session()
            assert await session.apply_patch(_lifecycle_patch()) == "Done!"
            await assert_lifecycle_patch_state(session)
            return "lifecycle patch applied"

        @function_tool(name_override="assert_workspace_escape_blocked", failure_error_function=None)
        async def assert_workspace_escape_blocked() -> str:
            session = self._session()
            for path in ("../outside.txt", "/tmp/sandbox-outside.txt", "symlink_escape.txt"):
                if path == "symlink_escape.txt":
                    # Inside the sandbox: a link in the workspace to a file beside it.
                    root = session.state.manifest.root
                    made = await session.exec(
                        f"printf 'outside\\n' > {root}/../symlink-outside.txt && "
                        f"ln -s {root}/../symlink-outside.txt {root}/symlink_escape.txt"
                    )
                    assert made.ok(), made.stderr
                try:
                    assert await _blocked(
                        lambda p=path: read_text(session, p), InvalidManifestPathError
                    )
                    assert await _blocked(
                        lambda p=path: write_text(session, p, "outside write\n"),
                        InvalidManifestPathError,
                    )
                    assert await _blocked(
                        lambda p=path: session.apply_patch(
                            ApplyPatchOperation(type="create_file", path=p, diff="+outside patch\n")
                        ),
                        ApplyPatchPathError,
                        InvalidManifestPathError,
                    )
                finally:
                    if path == "symlink_escape.txt":
                        root = session.state.manifest.root
                        await session.exec(
                            f"rm -f {root}/symlink_escape.txt {root}/../symlink-outside.txt"
                        )
            return "workspace escape blocked"

        @function_tool(name_override="extract_lifecycle_archive", failure_error_function=None)
        async def extract_lifecycle_archive() -> str:
            session = self._session()
            await session.extract("bundle.tar", _tar_bytes(ARCHIVE_WORKSPACE_TEXTS))
            await assert_texts(session, ARCHIVE_WORKSPACE_TEXTS)
            return "archive extracted"

        @function_tool(name_override="start_lifecycle_pty", failure_error_function=None)
        async def start_lifecycle_pty() -> str:
            session = self._session()
            pty = await session.pty_exec_start(
                "sh",
                "-c",
                "printf 'ready\\n'; while IFS= read -r line; do printf 'got:%s\\n' \"$line\"; done",
                shell=False,
                tty=True,
                yield_time_s=0.25,
            )
            assert pty.process_id is not None
            output = pty.output.decode("utf-8", errors="replace").replace("\r\n", "\n")
            assert output == "ready\n", output
            update = await session.pty_write_stdin(
                session_id=pty.process_id, chars="hello pty\n", yield_time_s=0.25
            )
            written = update.output.decode("utf-8", errors="replace").replace("\r\n", "\n")
            assert written == "hello pty\ngot:hello pty\n", written
            assert update.process_id == pty.process_id
            assert update.exit_code is None
            return "pty started and echoed stdin"

        @function_tool(name_override="assert_restored_lifecycle_state", failure_error_function=None)
        async def assert_restored_lifecycle_state() -> str:
            session = self._session()
            await assert_lifecycle_patch_state(session)
            await assert_texts(session, ARCHIVE_WORKSPACE_TEXTS)
            await assert_texts(session, EPHEMERAL_WORKSPACE_TEXTS)
            await assert_texts(session, _repo_texts(repo))
            dirs, files = await workspace_tree(session)
            want_dirs = RESTORED_WORKSPACE_DIRS | {f"repo/{d}" for d in repo.dirs}
            want_files = RESTORED_WORKSPACE_FILES | set(_repo_texts(repo))
            assert dirs == want_dirs, sorted(dirs ^ want_dirs)
            assert files == want_files, sorted(files ^ want_files)
            return "restored lifecycle state verified"

        return [
            assert_manifest_materialized,
            apply_lifecycle_patch,
            assert_workspace_escape_blocked,
            extract_lifecycle_archive,
            start_lifecycle_pty,
            assert_restored_lifecycle_state,
        ]

    def _session(self) -> BaseSandboxSession:
        assert self.session is not None
        return self.session


async def assert_lifecycle_patch_state(session: BaseSandboxSession) -> None:
    await assert_texts(
        session,
        {
            **{p: t for p, t in DURABLE_WORKSPACE_TEXTS.items() if p != "delete_me.txt"},
            **RUNTIME_WORKSPACE_TEXTS,
            **PATCHED_WORKSPACE_TEXTS,
        },
    )
    await assert_missing(session, "delete_me.txt")


async def workspace_tree(session: BaseSandboxSession) -> tuple[set[str], set[str]]:
    root = Path(session.state.manifest.root)
    dirs: set[str] = set()
    files: set[str] = set()

    async def collect(path: Path) -> None:
        for entry in await session.ls(path):
            entry_path = Path(entry.path)
            rel = (
                entry_path.relative_to(root) if entry_path.is_absolute() else entry_path
            ).as_posix()
            if entry.kind == EntryKind.DIRECTORY:
                if rel.startswith(".git") or "/.git" in rel:
                    continue
                dirs.add(rel)
                await collect(Path(rel))
            elif entry.kind == EntryKind.FILE:
                files.add(rel)

    await collect(Path())
    return dirs, files


@dataclass
class LifecycleResult:
    first_sandbox_id: str
    resumed_sandbox_id: str
    first_outputs: list[str]
    resumed_outputs: list[str]
    events: list[SandboxSessionEvent]
    session_state: dict[str, Any]


def _tool_outputs(items: Sequence[RunItem], *, agent: SandboxAgent) -> list[str]:
    return [
        item.output
        for item in items
        if isinstance(item, ToolCallOutputItem)
        and item.agent is agent
        and isinstance(item.output, str)
    ]


def successful_events(events: list[SandboxSessionEvent], *, op: str) -> int:
    return sum(
        1
        for event in events
        if event.op == op and event.phase == "finish" and getattr(event, "ok", False) is True
    )


async def run_lifecycle(
    *,
    make_client: Any,
    source_root: Path,
    repo: RepoFixture,
    options: PrizedSandboxClientOptions,
) -> LifecycleResult:
    """Run the SDK's pause-and-resume lifecycle through `Runner` on Prized."""
    events: list[SandboxSessionEvent] = []
    sandbox_ids: list[str] = []

    def record(event: SandboxSessionEvent, session: Any) -> None:
        events.append(event)
        sandbox_id = getattr(getattr(session, "state", None), "sandbox_id", None)
        if sandbox_id and (not sandbox_ids or sandbox_ids[-1] != sandbox_id):
            sandbox_ids.append(sandbox_id)

    client: PrizedSandboxClient = make_client(
        instrumentation=Instrumentation(sinks=[CallbackSink(record, mode="sync")])
    )
    manifest = build_manifest(source_root=source_root, repo=repo)

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    model = ScriptedModel(
        [
            [function_call("assert_manifest_materialized", {}, call_id="call_manifest")],
            [
                function_call(
                    "write_file",
                    {"path": "runtime_note.txt", "content": "runtime note v1\n"},
                    call_id="call_write",
                )
            ],
            [function_call("apply_lifecycle_patch", {}, call_id="call_patch")],
            [function_call("assert_workspace_escape_blocked", {}, call_id="call_escape")],
            [function_call("extract_lifecycle_archive", {}, call_id="call_extract")],
            [function_call("start_lifecycle_pty", {}, call_id="call_pty")],
            [function_call("approval_tool", {}, call_id="call_approval")],
        ]
    )
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Use the sandbox lifecycle tools.",
        default_manifest=manifest,
        tools=[approval_tool],
        capabilities=[SandboxFileCapability(), SandboxLifecycleProbeCapability(repo)],
    )
    run_config = RunConfig(sandbox=SandboxRunConfig(client=client, options=options))
    first = await Runner.run(
        agent, "verify the sandbox lifecycle and wait for approval", run_config=run_config
    )
    assert len(first.interruptions) == 1
    state = first.to_state()
    assert state._sandbox is not None
    session_state = state._sandbox["session_state"]
    assert isinstance(session_state, dict)
    first_sandbox_id = str(session_state["sandbox_id"])

    resumed_model = ScriptedModel(
        [
            [function_call("assert_restored_lifecycle_state", {}, call_id="call_restored")],
            [function_call("read_file", {"path": "runtime_note.txt"}, call_id="call_read")],
            [assistant_message("done")],
        ]
    )
    resumed_agent = SandboxAgent(
        name="sandbox",
        model=resumed_model,
        instructions="Use the sandbox lifecycle tools.",
        default_manifest=manifest,
        tools=[approval_tool],
        capabilities=[SandboxFileCapability(), SandboxLifecycleProbeCapability(repo)],
    )
    restored = await RunState.from_json(resumed_agent, json.loads(json.dumps(state.to_json())))
    interruptions = restored.get_interruptions()
    assert len(interruptions) == 1
    restored.approve(interruptions[0])
    resumed = await Runner.run(resumed_agent, restored, run_config=run_config)
    assert resumed.final_output == "done"
    return LifecycleResult(
        first_sandbox_id=first_sandbox_id,
        resumed_sandbox_id=sandbox_ids[-1],
        first_outputs=_tool_outputs(first.new_items, agent=agent),
        resumed_outputs=_tool_outputs(resumed.new_items, agent=resumed_agent),
        events=events,
        session_state=session_state,
    )


FIRST_RUN_OUTPUTS = [
    "manifest materialized",
    "wrote runtime_note.txt",
    "lifecycle patch applied",
    "workspace escape blocked",
    "archive extracted",
    "pty started and echoed stdin",
]
RESUMED_RUN_OUTPUTS = ["approved", "restored lifecycle state verified", "runtime note v1\n"]
