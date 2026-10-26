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

"""PrizedFileStore against the offline fake (real local files and bash, no network)."""

from __future__ import annotations

import os

import pytest
from agent_framework import AgentSession, FileAccessProvider, FileStoreEntry, create_harness_agent
from agent_framework_tools.shell import ShellExecutionError

from agent_framework_prized import PrizedFileStore, PrizedShellTool
from tests.fakes import FakeSdk
from tests.test_agent import ScriptedClient


@pytest.fixture
def sdk():
    fake = FakeSdk()
    with fake.patch():
        yield fake


async def test_write_read_list_delete_round_trip(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    assert sdk.created == []  # nothing until used
    await store.write("notes/plan.md", "step 1\nstep 2\n")
    assert await store.read("notes/plan.md") == "step 1\nstep 2\n"
    assert await store.file_exists("notes/plan.md")
    assert not await store.file_exists("notes")  # a directory is not a file
    await store.write("a.txt", "é")
    assert await store.list_children() == [FileStoreEntry("notes", "directory"), FileStoreEntry("a.txt", "file")]
    assert await store.list_children("notes") == [FileStoreEntry("plan.md", "file")]
    assert await store.delete("a.txt") is True
    assert await store.delete("a.txt") is False
    assert await store.delete("notes") is False  # not a regular file
    assert await store.read("a.txt") is None
    assert await store.read("notes") is None
    assert await store.list_children("missing") == []
    assert await store.list_children("notes/plan.md") == []
    # The shell sees the same files.
    assert (await shell.run("cat notes/plan.md | wc -l")).stdout.strip() == "2"
    await shell.close()
    assert sdk.alive() == []


async def test_exclusive_create_and_overwrite(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await store.write("x.txt", "one", overwrite=False)
    with pytest.raises(FileExistsError):
        await store.write("x.txt", "two", overwrite=False)
    assert await store.read("x.txt") == "one"
    await store.write("x.txt", "three")
    assert await store.read("x.txt") == "three"
    staged = [n for n in (await shell.run("ls -A /home/prized/.prized/maf")).stdout.split() if n.startswith("put-")]
    assert staged == []  # staging files are removed
    await shell.close()


async def test_write_keeps_an_executable_executable(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await shell.run("printf '#!/bin/sh\\necho v1\\n' > run.sh && chmod 755 run.sh")
    await store.write("run.sh", "#!/bin/sh\necho v2\n")
    assert (await shell.run("./run.sh")).stdout == "v2\n"
    await shell.close()


async def test_errors_are_the_ones_the_file_tools_expect(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await store.write("file.txt", "x")
    with pytest.raises(NotADirectoryError):
        await store.write("file.txt/child.txt", "y")
    await store.create_directory("dir/sub")
    with pytest.raises(IsADirectoryError):
        await store.write("dir/sub", "y")
    await shell.run("printf '\\xff\\xfe' > bin.dat")
    with pytest.raises(ValueError, match="not UTF-8"):
        await store.read("bin.dat")
    for bad in ("/etc/passwd", "../up.txt", "a/../../b", "C:/x", "a/./b", "", "dir/"):
        with pytest.raises(ValueError):
            await store.read(bad)
    assert await store.list_children("") != []
    await shell.close()


async def test_root_and_symlinks(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell, root="/home/prized/agent-files")
    await store.write("a.txt", "rooted")
    assert (await shell.run("cat /home/prized/agent-files/a.txt")).stdout == "rooted"
    await shell.run("ln -s /home/prized/agent-files/a.txt /home/prized/agent-files/link.txt")
    assert await store.read("link.txt") == "rooted"  # the file API refuses symlinks; the shell reads it
    assert [e.name for e in await store.list_children()] == ["a.txt"]  # links are left out
    with pytest.raises(ValueError):
        PrizedFileStore(shell, root="relative")
    # A root the file API refuses (here a symlinked directory) is listed through the shell.
    await shell.run("mkdir -p /home/prized/real/sub && echo x > /home/prized/real/f && ln -s real /home/prized/linked")
    linked = PrizedFileStore(shell, root="/home/prized/linked")
    assert await linked.list_children() == [FileStoreEntry("sub", "directory"), FileStoreEntry("f", "file")]
    assert await linked.read("f") == "x\n"
    assert await PrizedFileStore(shell, root="/home/prized/linked-missing").list_children() == []
    await shell.close()


async def test_follows_the_shell_when_its_sandbox_is_replaced(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await store.write("a.txt", "old")
    sdk.created[0].state = "deleted"
    assert await store.read("a.txt") is None  # a fresh, empty sandbox
    assert len(sdk.created) == 2
    await store.write("b.txt", "new")
    assert await store.read("b.txt") == "new"
    assert os.path.exists(sdk.created[1].local("/home/prized/work/b.txt"))
    await shell.close()
    assert sdk.alive() == []


async def test_attached_sandbox_gone_is_a_shell_execution_error(sdk):
    existing = sdk.add()
    store = PrizedFileStore(PrizedShellTool(sandbox_id=existing.id))
    await store.write("a.txt", "x")
    existing.state = "deleted"
    with pytest.raises(ShellExecutionError):
        await store.read("a.txt")


async def test_harness_file_tools_and_shell_share_the_sandbox(sdk):
    client = ScriptedClient(
        [
            "echo from-shell > shared.txt",
            (FileAccessProvider.READ_TOOL_NAME, {"file_name": "shared.txt"}),
            (FileAccessProvider.WRITE_TOOL_NAME, {"file_name": "tool.txt", "content": "from-tool"}),
            "cat tool.txt",
        ]
    )
    async with PrizedShellTool(approval_mode="never_require") as shell:
        agent = create_harness_agent(
            client,
            shell_executor=shell,
            file_access_store=PrizedFileStore(shell),
            file_access_disable_readonly_tool_approval=True,
            file_access_disable_write_tool_approval=True,
            disable_file_memory=True,
            disable_web_search=True,
        )
        assert (await agent.run("go", session=AgentSession())).text == "done"
        assert "from-shell" in client.results[1]
        assert "written" in client.results[2]
        assert "from-tool" in client.results[3]
    assert sdk.alive() == []
