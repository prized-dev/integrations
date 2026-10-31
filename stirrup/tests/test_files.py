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

"""Files, output paths, uploads and images."""

import os
import struct
import zlib
from pathlib import Path

import pytest
from prized import PrizedError
from stirrup.tools.code_backends.base import OutputSourceRoot

from stirrup_prized import PrizedCodeExecToolProvider

XFER = "/home/prized/.stirrup-prized/xfer"


def png_bytes() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


@pytest.fixture
async def provider(world):
    p = PrizedCodeExecToolProvider()
    async with p:
        yield p


@pytest.fixture
def remote(world, provider):
    return world.only()


def put(remote, path: str, data: bytes = b"data") -> str:
    local = remote.local(path)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "wb") as f:
        f.write(data)
    return local


def staged_files(remote) -> list[str]:
    local = remote.local(XFER)
    return os.listdir(local) if os.path.isdir(local) else []


async def test_write_and_read_relative_to_the_working_directory(provider, remote):
    blob = bytes(range(256)) * 10
    await provider.write_file_bytes("out/data.bin", blob)
    with open(remote.local("/home/prized/work/out/data.bin"), "rb") as f:
        assert f.read() == blob
    assert await provider.read_file_bytes("out/data.bin") == blob
    assert await provider.read_file_bytes("/home/prized/work/out/data.bin") == blob
    result = await provider.run_command("cat out/data.bin | wc -c")
    assert result.stdout.strip() == "2560"
    assert all(c[0] != "exec" for c in remote.calls[-4:-1]), "files under the home take the files API"


async def test_read_errors(provider, remote):
    with pytest.raises(FileNotFoundError):
        await provider.read_file_bytes("missing.txt")
    os.makedirs(remote.local("/home/prized/work/adir"))
    with pytest.raises(ValueError, match="not a file"):
        await provider.read_file_bytes("adir")
    put(remote, "/home/prized/work/afile")
    with pytest.raises(FileNotFoundError):
        await provider.read_file_bytes("afile/under")


async def test_read_and_write_outside_the_home(provider, remote):
    put(remote, "/srv/data/input.bin", b"\x00\x01binary\xff")
    assert await provider.read_file_bytes("/srv/data/input.bin") == b"\x00\x01binary\xff"
    await provider.write_file_bytes("/srv/new/dir/out.txt", b"written")
    with open(remote.local("/srv/new/dir/out.txt"), "rb") as f:
        assert f.read() == b"written"
    assert staged_files(remote) == [], "staged copies are removed"
    with pytest.raises(FileNotFoundError):
        await provider.read_file_bytes("/srv/data/missing.bin")
    with pytest.raises(ValueError, match="not a file"):
        await provider.read_file_bytes("/srv/data")
    assert staged_files(remote) == []


async def test_read_and_write_through_a_symlink(provider, remote):
    put(remote, "/srv/real.txt", b"through the link")
    os.symlink(remote.local("/srv/real.txt"), remote.local("/home/prized/work/link.txt"))
    assert await provider.read_file_bytes("link.txt") == b"through the link"
    await provider.write_file_bytes("link.txt", b"changed")
    with open(remote.local("/srv/real.txt"), "rb") as f:
        assert f.read() == b"changed"
    assert staged_files(remote) == []


async def test_a_write_outside_the_home_that_fails(provider, remote):
    put(remote, "/srv/blocker", b"a file where a directory should be")
    with pytest.raises(OSError, match="Could not write"):
        await provider.write_file_bytes("/srv/blocker/child.txt", b"x")
    assert staged_files(remote) == []


async def test_other_file_errors_are_raised(provider, world):
    world.fail("read", PrizedError("file_too_large", status=413))
    with pytest.raises(PrizedError, match="file_too_large"):
        await provider.read_file_bytes("big.bin")


async def test_file_exists(provider, remote):
    put(remote, "/home/prized/work/f.txt")
    os.makedirs(remote.local("/home/prized/work/d"))
    os.symlink("/nowhere", remote.local("/home/prized/work/broken"))
    assert await provider.file_exists("f.txt")
    assert await provider.file_exists("d")
    assert await provider.file_exists("broken")
    assert not await provider.file_exists("nope")
    put(remote, "/srv/x")
    assert await provider.file_exists("/srv/x")


async def test_is_directory(provider, remote):
    put(remote, "/home/prized/work/f.txt")
    os.makedirs(remote.local("/home/prized/work/d/sub"))
    os.symlink(remote.local("/home/prized/work/d"), remote.local("/home/prized/work/dlink"))
    assert await provider.is_directory("d")
    assert await provider.is_directory("/home/prized/work/d/sub")
    assert await provider.is_directory("dlink")
    assert not await provider.is_directory("f.txt")
    assert not await provider.is_directory("missing")


async def test_list_files(provider, remote):
    for path in ("a.txt", "sub/b.txt", "sub/deeper/c d.txt", "sub/new\nline.txt"):
        put(remote, f"/home/prized/work/proj/{path}")
    os.makedirs(remote.local("/home/prized/work/proj/empty"))
    assert await provider.list_files("proj") == ["a.txt", "sub/b.txt", "sub/deeper/c d.txt", "sub/new\nline.txt"]
    assert await provider.list_files("/home/prized/work/proj/sub/") == ["b.txt", "deeper/c d.txt", "new\nline.txt"]
    assert await provider.list_files("proj/a.txt") == []
    assert await provider.list_files("missing") == []


async def test_output_source_roots(provider):
    roots = provider.output_source_roots()
    assert roots == (OutputSourceRoot(path="/home/prized/work"), OutputSourceRoot(path="/home/prized"))


async def test_resolve_output_source(provider, remote):
    put(remote, "/home/prized/work/report.md")
    put(remote, "/home/prized/notes.txt")
    put(remote, "/srv/secret.txt")
    os.symlink(remote.local("/srv/secret.txt"), remote.local("/home/prized/work/leak.txt"))
    os.symlink(remote.local("/home/prized/work/report.md"), remote.local("/home/prized/work/alias.md"))
    os.makedirs(remote.local("/home/prized/work/folder"))
    assert await provider.resolve_output_source("report.md") == "/home/prized/work/report.md"
    assert await provider.resolve_output_source("alias.md") == "/home/prized/work/report.md"
    assert await provider.resolve_output_source("/home/prized/notes.txt") == "/home/prized/notes.txt"
    with pytest.raises(ValueError, match="outside the execution root"):
        await provider.resolve_output_source("leak.txt")
    with pytest.raises(ValueError, match="not a file"):
        await provider.resolve_output_source("folder")
    with pytest.raises(ValueError, match="Could not resolve"):
        await provider.resolve_output_source("missing.md")


async def test_save_output_files_to_the_host(provider, remote, tmp_path):
    put(remote, "/home/prized/work/out/report.txt", b"report")
    put(remote, "/home/prized/notes.txt", b"notes")
    put(remote, "/srv/secret.txt", b"secret")
    os.symlink(remote.local("/srv/secret.txt"), remote.local("/home/prized/work/leak.txt"))
    result = await provider.save_output_files(
        ["/home/prized/work/out/report.txt", "/home/prized/notes.txt", "leak.txt", "missing.txt"], tmp_path
    )
    assert sorted(s.source_path for s in result.saved) == ["/home/prized/notes.txt", "/home/prized/work/out/report.txt"]
    assert (tmp_path / "out" / "report.txt").read_bytes() == b"report"
    assert (tmp_path / "notes.txt").read_bytes() == b"notes"
    assert set(result.failed) == {"leak.txt", "missing.txt"}
    assert not (tmp_path / "leak.txt").exists()


async def test_output_destination_identity(provider, remote):
    os.makedirs(remote.local("/home/prized/work/outputs"))
    os.symlink(remote.local("/srv"), remote.local("/home/prized/work/outputs/escape"))
    identity = await provider.output_destination_identity("/home/prized/work/outputs/a/b.txt", "outputs")
    assert identity == "/home/prized/work/outputs/a/b.txt"
    with pytest.raises(ValueError, match="escapes"):
        await provider.output_destination_identity("/home/prized/work/outputs/escape/x.txt", "outputs")
    with pytest.raises(ValueError, match="escapes"):
        await provider.output_destination_identity("/home/prized/work/elsewhere.txt", "/home/prized/work/outputs")


async def test_upload_local_files_and_directories(provider, remote, tmp_path):
    (tmp_path / "sample.txt").write_text("Hello")
    tree = tmp_path / "sample_dir"
    (tree / "subdir").mkdir(parents=True)
    (tree / "file1.txt").write_text("File 1")
    (tree / "subdir" / "file3.txt").write_text("File 3")

    result = await provider.upload_files(tmp_path / "sample.txt", tree, tmp_path / "missing")
    assert sorted(u.dest_path for u in result.uploaded) == [
        "/home/prized/work/sample.txt",
        "/home/prized/work/sample_dir/file1.txt",
        "/home/prized/work/sample_dir/subdir/file3.txt",
    ]
    assert list(result.failed) == [str((tmp_path / "missing").resolve())]
    assert await provider.read_file_bytes("sample_dir/subdir/file3.txt") == b"File 3"

    result = await provider.upload_files(tree, dest_dir="/home/prized/inputs")
    assert sorted(u.dest_path for u in result.uploaded) == [
        "/home/prized/inputs/file1.txt",
        "/home/prized/inputs/subdir/file3.txt",
    ]


async def test_upload_from_another_sandbox(world, tmp_path):
    source = PrizedCodeExecToolProvider()
    dest = PrizedCodeExecToolProvider()
    async with source, dest:
        await source.write_file_bytes("skills/a/SKILL.md", b"skill a")
        await source.write_file_bytes("skills/b.txt", b"b")
        result = await dest.upload_files("skills", source_env=source)
        assert sorted(u.dest_path for u in result.uploaded) == ["skills/a/SKILL.md", "skills/b.txt"]
        assert await dest.read_file_bytes("skills/a/SKILL.md") == b"skill a"
        saved = await source.save_output_files(["skills/b.txt"], "/home/prized/work/from-parent", dest_env=dest)
        assert [str(s.output_path) for s in saved.saved] == ["/home/prized/work/from-parent/skills/b.txt"]
        assert await dest.read_file_bytes("from-parent/skills/b.txt") == b"b"


async def test_view_image(provider, remote):
    put(remote, "/home/prized/work/plot.png", png_bytes())
    put(remote, "/home/prized/work/broken.png", b"not an image")
    block = await provider.view_image("plot.png")
    assert block.data == png_bytes()
    with pytest.raises(ValueError, match="Unsupported image type"):
        await provider.view_image("anim.gif")
    with pytest.raises(ValueError, match="corrupt"):
        await provider.view_image("broken.png")
    with pytest.raises(FileNotFoundError):
        await provider.view_image("missing.png")


async def test_view_image_turns_sandbox_errors_into_value_errors(provider, world):
    world.fail("read", PrizedError("sandbox_unreachable", status=502))
    with pytest.raises(ValueError, match="sandbox_unreachable"):
        await provider.view_image("plot.png")


def test_paths_must_be_strings():
    from stirrup_prized._provider import _absolute, _files_api_serves

    with pytest.raises(ValueError):
        _absolute("")
    assert _absolute("a/b") == "/home/prized/work/a/b"
    assert _files_api_serves("/home/prized/work/x")
    assert not _files_api_serves("/home/prized/../etc/passwd")
    assert not _files_api_serves("/home/prizedx/y")
    assert not _files_api_serves(str(Path("/srv/x")))
