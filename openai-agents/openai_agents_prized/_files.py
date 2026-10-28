"""Reading and writing files, and archiving the workspace, for a Prized session.

Every path is judged in the sandbox, on what is actually opened:

- A read under `/home/prized` goes through the file API, which refuses any
  symlink and opens with `O_NOFOLLOW`. Anything else (a link the SDK's check
  allowed, a path under a grant elsewhere, a file larger than one transfer)
  is copied by a command that opens the file first and then checks where
  the open descriptor really is.
- A write is uploaded in parts to a staging directory, then joined by a
  command that walks to the target's directory one component at a time,
  holds it, checks it, and renames a new file over the target, so a link
  swapped in after the SDK's check cannot carry the bytes elsewhere.
- A snapshot is a tar of the workspace that stores links as links; its
  restore is checked member by member before upload, and extracted beneath
  the root by GNU tar.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from agents.sandbox.errors import (
    InvalidManifestPathError,
    SandboxError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceWriteTypeError,
)
from agents.sandbox.util.tar_utils import UnsafeTarMemberError, validate_tarfile
from agents.sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

from openai_agents_prized._api import PrizedAPIError
from openai_agents_prized._core import (
    STAGE_ROOT,
    PrizedSessionBase,
    api_context,
    api_retryable,
    under_home,
)
from openai_agents_prized._options import PRIZED_HOME
from openai_agents_prized._scripts import (
    EXIT_ESCAPE,
    EXIT_MOVED,
    EXIT_NOT_FILE,
    EXIT_NOT_FOUND,
    EXIT_READ_ONLY_GRANT,
    EXIT_ROOT_MOVED,
    EXIT_STAGE_CHANGED,
    HYDRATE,
    PERSIST,
    READ_STAGED,
    WRITE_STAGED,
)

if TYPE_CHECKING:
    from agents.sandbox.types import ExecResult, User

TRANSFER_PART_BYTES = 64 * 1024 * 1024
"""One upload or download; the file API takes at most 100 MiB a transfer."""

_STAGED_READ_CODES = frozenset({"symlink_not_allowed", "path_outside_root"})
"""File API refusals that a copy through a command can still serve."""


def _escape(path: Path, result: ExecResult) -> InvalidManifestPathError:
    rel = path.as_posix()
    return InvalidManifestPathError(
        rel=rel,
        reason="absolute" if rel.startswith("/") else "escape_root",
        context={"resolved_path": result.stderr.decode("utf-8", "replace").strip()},
    )


def _read_full(stream: io.IOBase, size: int) -> bytes:
    """Read `size` bytes, or what is left; a str stream is encoded as UTF-8."""
    out = bytearray()
    while len(out) < size:
        chunk = stream.read(size - len(out))
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError(type(chunk).__name__)
        out.extend(chunk)
    return bytes(out)


class PrizedFilesMixin(PrizedSessionBase):
    """`read`, `write`, `persist_workspace`, and `hydrate_workspace` for a Prized session."""

    async def read(self, path: Path | str, *, user: str | User | None = None) -> io.IOBase:
        """Read a file from the sandbox.

        Args:
            path: A path relative to the workspace root, or an absolute path it
                may reach.
            user: The user to read as; only `prized` exists.

        Returns:
            The file's bytes as a binary stream.
        """
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            workspace_path = await self._check_read_with_exec(path, user=user)
        else:
            workspace_path = await self._validate_path_access(path)
        return await self._read_file(workspace_path, error_path=error_path, max_bytes=None)

    async def _read_bounded(self, path: Path, *, max_bytes: int) -> bytes:
        error_path = posix_path_as_path(coerce_posix_path(path))
        workspace_path = await self._validate_path_access(path)
        stream = await self._read_file(workspace_path, error_path=error_path, max_bytes=max_bytes)
        with stream:
            return _read_full(stream, max_bytes)

    async def _read_file(
        self, workspace_path: Path, *, error_path: Path, max_bytes: int | None
    ) -> io.IOBase:
        remote = sandbox_path_str(workspace_path)
        if under_home(remote):
            try:
                data = await self._download(remote, max_bytes=max_bytes)
            except PrizedAPIError as exc:
                if exc.code == "not_found":
                    raise WorkspaceReadNotFoundError(path=error_path, cause=exc) from exc
                if exc.code not in _STAGED_READ_CODES and exc.status != 413:  # noqa: PLR2004
                    raise WorkspaceArchiveReadError(
                        path=error_path,
                        context=api_context(exc),
                        cause=exc,
                        retryable=api_retryable(exc, idempotent=True),
                    ) from exc
            else:
                return io.BytesIO(data)
        # Through a symlink, outside the home, or larger than one transfer:
        # copy it in parts to where the file API serves them.
        return await self._read_staged(remote, error_path=error_path, max_bytes=max_bytes)

    async def _read_staged(
        self, remote: str, *, error_path: Path, max_bytes: int | None
    ) -> io.IOBase:
        helper, root, grants = await self._path_rules()
        stage = self._stage_path("read")
        limit = "" if max_bytes is None else str(max_bytes)
        argv = ["bash", "-c", READ_STAGED, "bash", helper, remote, stage]
        argv += [str(TRANSFER_PART_BYTES), limit, root, *grants]
        try:
            try:
                result = await self._run(argv, timeout=self.state.timeouts.workspace_tar_s)
            except SandboxError as exc:
                raise WorkspaceArchiveReadError(path=error_path, cause=exc) from exc
            if result.exit_code == EXIT_NOT_FOUND:
                raise WorkspaceReadNotFoundError(path=error_path)
            if result.exit_code == EXIT_ESCAPE:
                raise _escape(error_path, result)
            if not result.ok():
                reason = {EXIT_NOT_FILE: "not_a_file", EXIT_MOVED: "moved_while_checked"}.get(
                    result.exit_code, "copy_failed"
                )
                raise WorkspaceArchiveReadError(
                    path=error_path,
                    context={
                        "backend": "prized",
                        "reason": reason,
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.decode("utf-8", "replace"),
                    },
                )
            try:
                return await self._download_parts(stage, result.stdout)
            except PrizedAPIError as exc:
                raise WorkspaceArchiveReadError(
                    path=error_path,
                    context=api_context(exc),
                    cause=exc,
                    retryable=api_retryable(exc, idempotent=True),
                ) from exc
        finally:
            await self._finish(self._remove_stage(stage))

    async def _download_parts(self, stage: str, listing: bytes) -> io.IOBase:
        """Download and join the `part-NNNN` files a `split` left in `stage`.

        One part comes back in memory; more go to a temporary file, so a large
        read or snapshot never sits in memory whole.
        """
        parts = sorted(
            line
            for line in listing.decode("utf-8", "replace").splitlines()
            if line.startswith("part-")
        )
        if len(parts) <= 1:
            data = await self._download(f"{stage}/{parts[0]}") if parts else b""
            return io.BytesIO(data)
        spool = tempfile.TemporaryFile()  # noqa: SIM115  # returned open
        try:
            for part in parts:
                spool.write(await self._download(f"{stage}/{part}"))
            spool.seek(0)
        except BaseException:
            spool.close()
            raise
        return cast("io.IOBase", spool)

    async def _download(self, path: str, *, max_bytes: int | None = None) -> bytes:
        return await self.on_edge(
            lambda: self._client.download(self._toolbox, path, max_bytes=max_bytes, wake=False),
            wake=True,
        )

    async def _upload(self, path: str, content: bytes) -> None:
        await self.on_edge(
            lambda: self._client.upload(self._toolbox, path, content, wake=False), wake=True
        )

    async def _upload_parts(self, stage: str, stream: io.IOBase) -> None:
        """Upload `stream` as `stage/part-NNNN` files of at most one transfer each."""
        index = 0
        while True:
            chunk = _read_full(stream, TRANSFER_PART_BYTES)
            if chunk or index == 0:
                await self._upload(f"{stage}/part-{index:04d}", chunk)
                index += 1
            if len(chunk) < TRANSFER_PART_BYTES:
                return

    async def write(
        self,
        path: Path | str,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        """Write a file into the sandbox, creating missing parent directories.

        The file is replaced whole, never half-written; an existing file keeps
        its mode, as it would on a local disk. A symlink at the path is written
        through, when it stays inside the workspace.

        Args:
            path: A path relative to the workspace root, or an absolute path it
                may reach.
            data: A binary stream positioned at the start of the content.
            user: The user to write as; only `prized` exists.
        """
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            await self._check_write_with_exec(path, user=user)
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            raise WorkspaceWriteTypeError(path=error_path, actual_type=type(payload).__name__)
        workspace_path = await self._validate_path_access(path, for_write=True)
        await self._write_file(
            sandbox_path_str(workspace_path),
            bytes(payload),
            error_path=error_path,
            workspace_path=workspace_path,
        )

    async def _write_file(
        self, remote: str, payload: bytes, *, error_path: Path, workspace_path: Path
    ) -> None:
        helper, root, grants = await self._path_rules()
        want = hashlib.sha256(payload).hexdigest()
        stage = self._stage_path("write")
        joined = False  # the join script removes the stage on every exit
        try:
            try:
                await self._upload_parts(stage, io.BytesIO(payload))
            except PrizedAPIError as exc:
                raise WorkspaceArchiveWriteError(
                    path=workspace_path,
                    context=api_context(exc),
                    cause=exc,
                    retryable=api_retryable(exc, idempotent=True),
                ) from exc
            try:
                argv = ["bash", "-c", WRITE_STAGED, "bash", helper, stage, remote, root, want]
                result = await self._run(
                    [*argv, *grants], timeout=self.state.timeouts.workspace_tar_s
                )
            except SandboxError as exc:
                raise WorkspaceArchiveWriteError(path=workspace_path, cause=exc) from exc
            joined = True
        finally:
            if not joined:
                await self._finish(self._remove_stage(stage))
        if result.exit_code == EXIT_ESCAPE:
            raise _escape(error_path, result)
        if not result.ok():
            reason = {
                EXIT_READ_ONLY_GRANT: "read_only_extra_path_grant",
                EXIT_STAGE_CHANGED: "staged_upload_changed",
            }.get(result.exit_code, "write_failed")
            raise WorkspaceArchiveWriteError(
                path=workspace_path,
                context={
                    "backend": "prized",
                    "reason": reason,
                    "exit_code": result.exit_code,
                    "stderr": result.stderr.decode("utf-8", "replace"),
                },
            )

    async def persist_workspace(self) -> io.IOBase:
        """Archive the workspace as a tar stream of workspace-relative paths.

        Symlinks are stored as links, never followed.

        Returns:
            The tar archive.
        """
        root = self._workspace_root_path()
        stage = self._stage_path("persist")
        excludes = [
            f"--exclude=./{rel.as_posix().lstrip('/')}"
            for rel in sorted(self._persist_workspace_skip_relpaths(), key=lambda p: p.as_posix())
            if rel.as_posix().lstrip("/") not in {"", "."}
        ]
        argv = ["bash", "-c", PERSIST, "bash", sandbox_path_str(root), stage]
        argv += [str(TRANSFER_PART_BYTES), *excludes]
        try:
            try:
                result = await self._run(
                    argv, timeout=self.state.timeouts.workspace_tar_s, cwd=PRIZED_HOME
                )
            except SandboxError as exc:
                raise WorkspaceArchiveReadError(
                    path=root, context={"backend": "prized"}, cause=exc
                ) from exc
            if not result.ok():
                moved = result.exit_code == EXIT_ROOT_MOVED
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "backend": "prized",
                        "reason": "workspace_root_is_a_link" if moved else "tar_failed",
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.decode("utf-8", "replace"),
                    },
                    retryable=False,
                )
            try:
                return await self._download_parts(stage, result.stdout)
            except PrizedAPIError as exc:
                raise WorkspaceArchiveReadError(
                    path=root,
                    context=api_context(exc, reason="download_failed"),
                    cause=exc,
                    retryable=api_retryable(exc, idempotent=True),
                ) from exc
        finally:
            await self._finish(self._remove_stage(stage))

    def _persist_workspace_skip_relpaths(self) -> set[Path]:
        skip = super()._persist_workspace_skip_relpaths()
        root = PurePosixPath(sandbox_path_str(self._workspace_root_path()))
        stage = PurePosixPath(STAGE_ROOT)
        if stage != root and root in stage.parents:
            skip.add(Path(stage.relative_to(root).as_posix()))
        return skip

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        """Extract a tar archive made by `persist_workspace` into the workspace root.

        Links are restored as links, wherever they point (a virtualenv's
        `python` points at the system's), as the SDK's tar policy allows: no
        member may sit under a link of the archive, so nothing is written
        through one.

        Args:
            data: The tar archive.
        """
        root = self._workspace_root_path()
        with tempfile.SpooledTemporaryFile(max_size=TRANSFER_PART_BYTES) as spool:
            try:
                while chunk := _read_full(data, TRANSFER_PART_BYTES):
                    spool.write(chunk)
            except TypeError as exc:
                raise WorkspaceWriteTypeError(path=root, actual_type=str(exc)) from exc
            spool.seek(0)
            archive = cast("io.IOBase", spool)
            self._check_archive(archive, root=root)
            archive.seek(0)
            await self._extract(archive, root=root)

    def _check_archive(self, archive: io.IOBase, *, root: Path) -> None:
        try:
            with tarfile.open(fileobj=archive, mode="r:*") as tar:
                validate_tarfile(tar, allow_external_symlink_targets=True)
        except UnsafeTarMemberError as exc:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "backend": "prized",
                    "reason": "unsafe_or_invalid_tar",
                    "member": exc.member,
                    "detail": str(exc),
                },
                cause=exc,
            ) from exc
        except (tarfile.TarError, OSError) as exc:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "backend": "prized",
                    "reason": "unsafe_or_invalid_tar",
                    "detail": str(exc),
                },
                cause=exc,
            ) from exc

    async def _extract(self, archive: io.IOBase, *, root: Path) -> None:
        stage = self._stage_path("hydrate")
        try:
            try:
                await self._upload_parts(stage, archive)
            except PrizedAPIError as exc:
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context=api_context(exc, reason="upload_failed"),
                    cause=exc,
                    retryable=api_retryable(exc, idempotent=True),
                ) from exc
            try:
                result = await self._run(
                    ["bash", "-c", HYDRATE, "bash", sandbox_path_str(root), stage],
                    timeout=self.state.timeouts.workspace_tar_s,
                    cwd=PRIZED_HOME,
                )
            except SandboxError as exc:
                raise WorkspaceArchiveWriteError(
                    path=root, context={"backend": "prized"}, cause=exc
                ) from exc
            if not result.ok():
                moved = result.exit_code == EXIT_ROOT_MOVED
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "backend": "prized",
                        "reason": "workspace_root_is_a_link" if moved else "tar_extract_failed",
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.decode("utf-8", "replace"),
                    },
                )
        finally:
            await self._finish(self._remove_stage(stage))
