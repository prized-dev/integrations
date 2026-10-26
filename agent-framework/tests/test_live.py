# Copyright 2026 Prized
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Live tests against the real Prized API.

They create real sandboxes (billed to the key's workspace) and delete every one.
Run with: PRIZED_API_KEY=dcp_... PRIZED_LIVE=1 pytest -m live -s
Set PRIZED_LIVE_IDS_FILE to a path to have every sandbox id they touch appended there.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from typing import Any

import prized
import pytest
from agent_framework import Agent, AgentSession, FileAccessProvider, FileStoreEntry, create_harness_agent
from agent_framework_tools.shell import ShellEnvironmentProvider, ShellExecutionError
from prized import AsyncSandbox

from agent_framework_prized import PrizedFileStore, PrizedShellTool
from tests.test_agent import ScriptedClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("PRIZED_API_KEY") and os.environ.get("PRIZED_LIVE")),
        reason="set PRIZED_API_KEY and PRIZED_LIVE=1 to run live tests",
    ),
]

RUN = uuid.uuid4().hex[:8]
LABELS = {"test": "agent-framework-prized-live", "run": RUN}
# Small shapes: forks run 3 at once, and the test workspace shares capacity.
SMALL: dict[str, Any] = {"vcpu": 1, "memory_gb": 2}
SEEN: list[str] = []
GONE = ("deleting", "deleted", "gone")


def log(*parts: Any) -> None:
    print("[live]", *parts, flush=True)


def seen(*ids: str | None) -> None:
    for sandbox_id in ids:
        if sandbox_id and sandbox_id not in SEEN:
            SEEN.append(sandbox_id)


async def state_of(sandbox_id: str) -> str:
    try:
        sb = await AsyncSandbox.connect(sandbox_id)
    except prized.PrizedError as e:
        if e.code == "not_found":
            return "gone"
        raise
    try:
        return sb.state
    finally:
        await sb.close()


async def delete(sandbox_id: str) -> None:
    sb = await AsyncSandbox.connect(sandbox_id)
    try:
        await sb.delete(wait=True)
    finally:
        await sb.close()


@pytest.fixture(scope="module", autouse=True)
def _sweep():
    yield

    async def sweep() -> None:
        for sb in await AsyncSandbox.list(labels={"run": RUN}):
            seen(sb.id)
            if sb.state not in GONE:
                log("sweeping leftover", sb.id, sb.state)
                await sb.delete(wait=True)
            await sb.close()

    asyncio.run(sweep())
    log("ids", " ".join(SEEN))
    path = os.environ.get("PRIZED_LIVE_IDS_FILE")
    if path:
        with open(path, "a") as f:
            f.writelines(f"{sandbox_id}\n" for sandbox_id in SEEN)


async def test_commands_session_pip_timeouts_and_big_commands():
    t0 = time.monotonic()
    shell = PrizedShellTool(**SMALL, labels=LABELS, timeout=60)
    result = await shell.run("pwd; python3 --version; node --version; echo oops >&2; exit 4")
    seen(shell.sandbox_id)
    log("first command incl. create", f"{time.monotonic() - t0:.2f}s", shell.sandbox_id)
    lines = result.stdout.splitlines()
    assert result.exit_code == 4 and result.stderr == "oops\n"
    assert lines[0] == "/home/prized/work" and lines[1].startswith("Python 3.12") and lines[2].startswith("v22.")

    t1 = time.monotonic()
    await shell.run("mkdir -p app && cd app && export STAGE=dev && COUNT=2 && greet() { echo hi $1; }")
    after = await shell.run('pwd; echo "$STAGE $COUNT"; greet there')
    log("warm command", f"{time.monotonic() - t1:.2f}s for two")
    assert after.stdout == "/home/prized/work/app\ndev 2\nhi there\n"

    pip = await shell.run("pip install -q --no-warn-script-location cowsay 2>&1 | tail -2; command -v cowsay")
    log("pip", pip.stdout.strip(), pip.stderr.strip())
    assert pip.stdout.strip().endswith("/home/prized/.local/bin/cowsay")

    venv = await shell.run("python3 -m venv .venv && . .venv/bin/activate && command -v python")
    assert venv.stdout == "/home/prized/work/app/.venv/bin/python\n", venv.stderr
    assert (await shell.run("command -v python; deactivate; command -v python || echo none")).stdout == (
        "/home/prized/work/app/.venv/bin/python\nnone\n"
    )

    timed = await shell.run("echo started; cd /; sleep 30", timeout=2)
    assert timed.timed_out and timed.exit_code == -1 and timed.stdout == "started\n"
    assert (await shell.run("pwd")).stdout == "/home/prized/work/app\n"

    payload = "z" * 200_000
    big = await shell.run(f"printf '%s' '{payload}' > big.txt && wc -c < big.txt")
    assert big.stdout.strip() == "200000", big.stderr
    leftovers = await shell.run("ls -A /home/prized/.prized/maf")
    assert leftovers.stdout.split() == ["shell-state.sh"]

    flood = await shell.run("yes | head -c 5000000")
    assert flood.truncated and "[... truncated" in flood.stdout
    assert len(flood.stdout.encode()) < 70 * 1024

    stateless = PrizedShellTool(sandbox_id=shell.sandbox_id, mode="stateless")
    assert (await stateless.run('pwd; echo "${STAGE-unset}"')).stdout == "/home/prized/work\nunset\n"
    await stateless.close()  # attached: kept

    sandbox_id = shell.sandbox_id
    assert sandbox_id is not None
    await shell.close()
    assert await state_of(sandbox_id) in GONE


async def test_output_streams_as_it_is_printed():
    arrivals: list[tuple[float, str, str]] = []
    t0 = 0.0

    def on_output(stream: str, text: str) -> None:
        arrivals.append((round(time.monotonic() - t0, 1), stream, text))

    async with PrizedShellTool(**SMALL, labels=LABELS, on_output=on_output) as shell:
        seen(shell.sandbox_id)
        t0 = time.monotonic()
        result = await shell.run("for i in 1 2 3; do echo $i; sleep 1; done; echo bye >&2")
        log("arrivals", arrivals)
        assert result.stdout == "1\n2\n3\n" and result.stderr == "bye\n"
        assert "".join(t for _, s, t in arrivals if s == "stdout") == "1\n2\n3\n"
        assert arrivals[0][0] < 1.5, "the first line arrived before the command ended"
    assert await state_of(shell.sandbox_id or "") in GONE


async def test_background_server_keeps_running_between_commands():
    async with PrizedShellTool(**SMALL, labels=LABELS, approval_mode="never_require") as shell:
        seen(shell.sandbox_id)
        t0 = time.monotonic()
        started = await shell.run("nohup python3 -m http.server 8123 > server.log 2>&1 &")
        log("background start took", f"{time.monotonic() - t0:.2f}s")
        assert started.exit_code == 0 and time.monotonic() - t0 < 4
        fetched = await shell.run("sleep 1; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8123/")
        assert fetched.stdout == "200"


async def test_pause_on_close_restart_and_attach_keep_files_processes_and_session():
    shell = PrizedShellTool(**SMALL, labels=LABELS, on_close="pause")
    await shell.run(
        # `a && b && c &` would background the whole list (and its cd) in a subshell.
        "mkdir -p svc && cd svc && export MODE=paused-test\n"
        "nohup bash -c 'i=0; while true; do i=$((i+1)); echo $i > /home/prized/count; sleep 0.2; done' "
        ">/dev/null 2>&1 &"
    )
    sandbox_id = shell.sandbox_id
    assert sandbox_id is not None
    seen(sandbox_id)
    await asyncio.sleep(1)
    await shell.close()
    assert await state_of(sandbox_id) in ("pausing", "paused")

    t0 = time.monotonic()
    first = await shell.run('pwd; echo "$MODE"; cat /home/prized/count')  # restarts in the same sandbox
    log("resume on next command", f"{time.monotonic() - t0:.2f}s")
    assert shell.sandbox_id == sandbox_id
    where, mode, count = first.stdout.split()
    assert (where, mode) == ("/home/prized/work/svc", "paused-test")
    await asyncio.sleep(1)
    assert int((await shell.run("cat /home/prized/count")).stdout) > int(count), "the process kept counting"
    await shell.close()

    again = PrizedShellTool(sandbox_id=sandbox_id)
    assert (await again.run("pwd")).stdout == "/home/prized/work/svc\n"  # the session lives in the sandbox
    await again.close()  # attached: kept
    assert await state_of(sandbox_id) == "running"
    await delete(sandbox_id)


async def test_fork_copies_files_and_session():
    async with PrizedShellTool(**SMALL, labels=LABELS) as parent:
        seen(parent.sandbox_id)
        await parent.run("mkdir -p w && cd w && export WHO=parent && echo parent > state.txt")
        copies = await parent.fork(2)
        seen(*(c.sandbox_id for c in copies))
        try:
            for i, copy in enumerate(copies):
                copied = await copy.run('pwd; echo "$WHO"; cat state.txt')
                assert copied.stdout == "/home/prized/work/w\nparent\nparent\n"
                await copy.run(f"echo copy{i} > state.txt")
            assert (await parent.run("cat state.txt")).stdout == "parent\n"
        finally:
            for copy in copies:
                await copy.close()
    for sandbox_id in [parent.sandbox_id] + [c.sandbox_id for c in copies]:
        assert await state_of(sandbox_id or "") in GONE


async def test_gone_sandbox_is_replaced_and_the_model_told():
    shell = PrizedShellTool(**SMALL, labels=LABELS)
    await shell.run("echo before > f")
    first = shell.sandbox_id
    assert first is not None
    seen(first)
    await delete(first)
    result = await shell.run("ls; echo after")
    seen(shell.sandbox_id)
    assert shell.sandbox_id != first
    assert result.stdout == "after\n"
    assert result.stderr.startswith("[prized: the previous sandbox was gone")
    await shell.close()
    assert await state_of(shell.sandbox_id or "") in GONE


async def test_attached_sandbox_that_is_gone_is_an_error():
    shell = PrizedShellTool(**SMALL, labels=LABELS)
    await shell.start()
    seen(shell.sandbox_id)
    attached = PrizedShellTool(sandbox_id=shell.sandbox_id)
    await attached.run("true")
    await shell.close()  # deletes without waiting
    for _ in range(60):
        if await state_of(shell.sandbox_id or "") in ("deleted", "gone"):
            break
        await asyncio.sleep(0.5)
    with pytest.raises(ShellExecutionError):
        await attached.run("true")
    await attached.close()


def test_threads_and_event_loops_share_one_sandbox():
    shell = PrizedShellTool(**SMALL, labels=LABELS, mode="stateless")
    asyncio.run(shell.run("echo 1 > f"))
    seen(shell.sandbox_id)
    assert asyncio.run(shell.run("cat f")).stdout == "1\n"
    out: list[str] = []
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            out.append(asyncio.run(shell.run(f"echo {n}")).stdout)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and sorted(out) == [f"{n}\n" for n in range(4)]

    async def mine() -> list[str]:
        return [sb.id for sb in await AsyncSandbox.list(labels={"run": RUN}) if sb.state not in GONE]

    assert asyncio.run(mine()).count(shell.sandbox_id or "") == 1
    asyncio.run(shell.close())


async def test_agents_drive_it_plain_and_as_the_harness_shell():
    client = ScriptedClient(["git --version && echo 'print(6 * 7)' > m.py", "python3 m.py"])
    async with PrizedShellTool(**SMALL, labels=LABELS, approval_mode="never_require") as shell:
        seen(shell.sandbox_id)
        agent = Agent(client=client, tools=[shell.as_function()])
        assert (await agent.run("go")).text == "done"
        assert "42" in client.results[-1]

        harness_client = ScriptedClient(["cat m.py"])
        harness = create_harness_agent(
            harness_client,
            shell_executor=shell,
            shell_environment_provider_options=shell.environment_options(),
            disable_file_memory=True,
            disable_web_search=True,
        )
        await harness.run("look", session=AgentSession())
        system = harness_client.instructions[0]
        log("environment block", system[system.find("## Shell environment") :][:400])
        assert "POSIX shell 5.2" in system and "Prized sandbox" in system
        assert "git (git version" in system and "python3 (Python 3.12" in system
        assert "print(6 * 7)" in harness_client.results[-1]

        snapshot = await ShellEnvironmentProvider(shell, shell.environment_options()).refresh()
        assert snapshot.working_directory == "/home/prized/work"


async def test_file_store_shares_the_sandbox_with_the_shell():
    async with PrizedShellTool(**SMALL, labels=LABELS, approval_mode="never_require") as shell:
        seen(shell.sandbox_id)
        store = PrizedFileStore(shell)
        await store.write("notes/plan.md", "step 1\n")
        assert await store.read("notes/plan.md") == "step 1\n"
        assert await store.read("missing.md") is None
        assert await store.read("notes") is None
        with pytest.raises(FileExistsError):
            await store.write("notes/plan.md", "again", overwrite=False)
        await shell.run("printf '#!/bin/sh\\necho v1\\n' > run.sh && chmod 755 run.sh && ln -s run.sh link.sh")
        await store.write("run.sh", "#!/bin/sh\necho v2\n")
        assert (await shell.run("./run.sh")).stdout == "v2\n"
        assert await store.read("link.sh") == "#!/bin/sh\necho v2\n"
        assert await store.list_children() == [
            FileStoreEntry("notes", "directory"),
            FileStoreEntry("run.sh", "file"),
        ]
        assert await store.list_children("nope") == []
        assert await store.delete("notes/plan.md") is True and await store.delete("notes/plan.md") is False
        assert not await store.file_exists("notes/plan.md")
        outside = PrizedFileStore(shell, root="/tmp/maf-store")  # outside /home/prized: the shell serves it
        await outside.write("d/a.txt", "out")
        assert await outside.read("d/a.txt") == "out"
        assert await outside.list_children() == [FileStoreEntry("d", "directory")]
        assert await outside.list_children("d") == [FileStoreEntry("a.txt", "file")]

        client = ScriptedClient(
            [
                "echo from-shell > shared.txt",
                (FileAccessProvider.READ_TOOL_NAME, {"file_name": "shared.txt"}),
                (FileAccessProvider.WRITE_TOOL_NAME, {"file_name": "tool.txt", "content": "from-tool"}),
                "cat tool.txt",
            ]
        )
        agent = create_harness_agent(
            client,
            shell_executor=shell,
            file_access_store=store,
            file_access_disable_readonly_tool_approval=True,
            file_access_disable_write_tool_approval=True,
            disable_file_memory=True,
            disable_web_search=True,
        )
        await agent.run("go", session=AgentSession())
        assert "from-shell" in client.results[1] and "from-tool" in client.results[3]
        left = await shell.run("ls -A /home/prized/.prized/maf")
        assert left.stdout.split() == ["shell-state.sh"]  # staging files are gone


async def test_session_edge_cases_on_a_real_sandbox():
    shell = PrizedShellTool(**SMALL, labels=LABELS, env={"SECRET_TOKEN": "s3cr3t-value"}, on_output=lambda s, t: None)
    await shell.run(
        "shopt -s extglob\ncd /home/prized && export KEEP=1\nkeepers() { ls !(nothing); }\nzz() { echo zz; }"
    )
    seen(shell.sandbox_id)
    state = await shell.run("cat /home/prized/.prized/maf/shell-state.sh")
    assert "s3cr3t-value" not in state.stdout and "KEEP" in state.stdout
    assert (await shell.run('zz; pwd; echo "$KEEP $SECRET_TOKEN"')).stdout == "zz\n/home/prized\n1 s3cr3t-value\n"
    # Streamed, so the SDK runs it detached: a 70 KB quote-heavy command still runs.
    code = "print('hello')\n" * 5000
    big = await shell.run(f"cat > m.py <<'EOF'\n{code}EOF\npython3 m.py | wc -l")
    assert big.stdout.strip() == "5000", big.stderr
    # Sandboxes from some template builds stop answering once /home/prized/work is gone:
    # the command puts it back as it ends.
    stateless = PrizedShellTool(sandbox_id=shell.sandbox_id, mode="stateless")
    assert (await stateless.run("cd /home/prized && rm -rf work && echo gone")).stdout == "gone\n"
    assert (await stateless.run("pwd")).stdout == "/home/prized/work\n"
    await stateless.close()
    assert (await shell.run("rm -rf /home/prized/work; echo again")).stdout == "again\n"
    assert (await shell.run("echo alive")).stdout == "alive\n"
    traced = await shell.run("set -x; echo hi")
    assert "__prized" not in traced.stderr
    sandbox_id = shell.sandbox_id
    await shell.close()
    assert await state_of(sandbox_id or "") in GONE
