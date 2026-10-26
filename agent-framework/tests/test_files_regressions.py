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

"""Regression tests for PrizedFileStore: each failed before its fix."""

from __future__ import annotations

import os

import pytest
from prized import PrizedError

from agent_framework_prized import PrizedFileStore, PrizedShellTool
from agent_framework_prized import _files as files_module
from tests.fakes import FakeAsyncSandbox, FakeSdk


@pytest.fixture
def sdk():
    fake = FakeSdk()
    with fake.patch():
        yield fake


def is_write(cmd: object) -> bool:
    return isinstance(cmd, list) and cmd[:3] == ["bash", "-c", files_module._WRITE]


async def test_a_symlink_cannot_lead_a_path_out_of_the_root(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await shell.run(
        "mkdir -p /home/prized/outside && echo secret > /home/prized/outside/s.txt"
        " && ln -s /home/prized/outside /home/prized/work/link"
        " && ln -s /home/prized/outside/s.txt /home/prized/work/leaf"
        " && mkdir -p inner && echo in > inner/i.txt && ln -s inner inlink"
    )
    for call in (
        lambda: store.read("link/s.txt"),
        lambda: store.read("leaf"),
        lambda: store.list_children("link"),
        lambda: store.write("link/planted.txt", "x"),
        lambda: store.delete("leaf"),
        lambda: store.file_exists("leaf"),
        lambda: store.create_directory("link/new"),
    ):
        with pytest.raises(ValueError, match="out of the store's root"):
            await call()
    assert not sdk.created[0].exists("/home/prized/outside/planted.txt")
    assert sdk.created[0].exists("/home/prized/work/leaf")
    # A link that stays inside the root is followed, as in the shell.
    assert await store.read("inlink/i.txt") == "in\n"
    assert [e.name for e in await store.list_children()] == ["inner"]  # links themselves are left out
    await shell.close()


async def test_a_failed_exclusive_write_is_not_reported_as_existing_and_leaves_nothing(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await shell.run("true")
    remote = sdk.created[0]

    def drop_staged(cmd, kwargs):
        if is_write(cmd):  # stands in for any cat failure after the create (ENOSPC, EIO)
            os.remove(remote.local(cmd[-1]))

    remote.handler = drop_staged
    with pytest.raises(OSError) as info:
        await store.write("fresh.txt", "content", overwrite=False)
    assert not isinstance(info.value, FileExistsError)
    assert not remote.exists("/home/prized/work/fresh.txt")
    remote.handler = None
    await shell.close()


async def test_read_under_a_regular_file_is_none(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await store.write("file.txt", "x")
    assert await store.read("file.txt/child") is None
    await shell.close()


async def test_the_staging_file_is_removed_when_the_write_fails(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await shell.run("true")
    remote = sdk.created[0]

    def fail(cmd, kwargs):
        if is_write(cmd):
            raise PrizedError("sandbox_unreachable", status=502)

    remote.handler = fail
    with pytest.raises(Exception):  # noqa: B017 - any sandbox error
        await store.write("a.txt", "secret-ish content")
    remote.handler = None
    staged = [n for n in os.listdir(remote.local("/home/prized/.prized/maf")) if n.startswith("put-")]
    assert staged == []
    await shell.close()


async def test_pipes_and_sockets_are_not_listed(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await shell.run("mkfifo pipe && echo f > real.txt")
    assert [(e.name, e.type) for e in await store.list_children()] == [("real.txt", "file")]
    assert await store.file_exists("pipe") is False
    await shell.close()


async def test_paths_with_newlines_are_refused(sdk):
    store = PrizedFileStore(PrizedShellTool())
    with pytest.raises(ValueError):
        await store.write("a\n/b.txt", "x")
    assert sdk.created == []


async def test_a_missing_file_costs_at_most_one_status_check(sdk, monkeypatch):
    calls: list[str] = []
    real_refresh = FakeAsyncSandbox.refresh

    async def refresh(self):
        calls.append(self.id)
        return await real_refresh(self)

    monkeypatch.setattr(FakeAsyncSandbox, "refresh", refresh)
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await store.create_directory("")
    calls.clear()
    assert await store.list_children("nodir") == []
    assert calls == []  # a listing goes through the shell: no extra call
    assert await store.read("nope.txt") is None
    assert len(calls) <= 1
    await shell.close()
