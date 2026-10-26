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

"""An Agent Framework file store whose files live in a PrizedShellTool's sandbox."""

from __future__ import annotations

import base64
import contextlib
import errno
import posixpath
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar

from agent_framework import AgentFileStore, FileStoreEntry
from agent_framework_tools.shell import ShellExecutionError
from prized import PrizedError

from ._shell import _MAYBE_GONE_CODES, _STATE_DIR, PrizedShellTool

if TYPE_CHECKING:
    from prized import AsyncSandbox
    from prized import ExecResult as PrizedExecResult

T = TypeVar("T")

_HELPER_TIMEOUT = 120
_DRIVE = re.compile(r"^[A-Za-z]:")
# Every helper gets the store's root as $1 and a path under it as $2, and first checks
# that the path, symlinks resolved, is still under the root (exit 7 if not).
_INSIDE = (
    'r=$(realpath -m -- "$1") && t=$(realpath -m -- "$2") || exit 9; '
    '[ "$r" = / ] || case $t/ in "$r"/*) ;; *) exit 7 ;; esac; '
)
# A write lands whole or not at all: the content goes to a temporary file next to the
# target (through a symlink, the file it points to), which takes the target's mode, so
# an edited script stays executable, and then replaces it. overwrite=False first claims
# the name exclusively (noclobber), and gives it back if the write fails.
_WRITE = (
    'trap \'rm -f -- "$4" "${tmp-}"\' EXIT; ' + _INSIDE + 'mkdir -p -- "$(dirname -- "$2")" || exit 3; '
    '[ -d "$2" ] && exit 4; '
    # Anything already there counts (opening a pipe to claim it would block).
    'if [ "$3" = new ]; then { [ -e "$2" ] || [ -L "$2" ]; } && exit 5; '
    '(set -o noclobber; : > "$2") || { [ -e "$2" ] && exit 5; exit 6; }; claimed=1; fi; '
    'tmp=$(mktemp -- "$t.prized-XXXXXX") || exit 6; '
    '[ -f "$t" ] && chmod --reference="$t" -- "$tmp"; '
    'cat -- "$4" > "$tmp" && mv -f -- "$tmp" "$t" || { [ -n "${claimed-}" ] && rm -f -- "$2"; exit 6; }'
)
_READ = _INSIDE + '[ -f "$2" ] || exit 3; base64 -w0 -- "$2"'
_DELETE = _INSIDE + '[ -f "$2" ] || exit 3; rm -f -- "$2"'
_EXISTS = _INSIDE + '[ -f "$2" ]'
_MKDIR = _INSIDE + 'mkdir -p -- "$2"'
# Directories, then regular files; symlinks, sockets and pipes are left out.
_LIST = (
    _INSIDE + '[ -d "$2" ] || exit 3; '
    "find -H \"$2\" -mindepth 1 -maxdepth 1 \\( -type d -printf 'd/%f\\0' \\) -o \\( -type f -printf 'f/%f\\0' \\)"
)
_OUTSIDE = 7


class PrizedFileStore(AgentFileStore):
    """An ``AgentFileStore`` over files in a :class:`PrizedShellTool`'s sandbox, so the
    harness's file tools and the shell see the same files.

    Paths are relative to ``root`` (the shell's working directory by default) and can't
    leave it: absolute paths and ``..`` are refused, and so is a path that a symlink
    leads out of the root. It uses the shell's sandbox: starting it if needed, and
    following it when it is replaced or restarted.

    Example::

        shell = PrizedShellTool(approval_mode="never_require")
        agent = create_harness_agent(
            client, shell_executor=shell, file_access_store=PrizedFileStore(shell)
        )
    """

    def __init__(self, shell: PrizedShellTool, root: str | None = None) -> None:
        """Args:
        shell: The tool whose sandbox holds the files.
        root: Absolute directory in the sandbox that paths are relative to. Defaults to
            the shell's ``working_dir``. Created on the first write.
        """
        root = shell.working_dir if root is None else root
        if not isinstance(root, str) or not posixpath.isabs(root) or "\n" in root or "\0" in root:
            raise ValueError("root must be an absolute path")
        self._shell = shell
        self.root = posixpath.normpath(root)

    async def write(self, path: str, content: str, *, overwrite: bool = True) -> None:
        """Write ``content`` (UTF-8) to ``path``, creating its directories. With
        ``overwrite=False`` an existing file raises ``FileExistsError``."""
        full = self._full(path)
        staging = f"{_STATE_DIR}/put-{uuid.uuid4().hex}"
        data = content.encode("utf-8")

        async def put(sandbox: AsyncSandbox) -> PrizedExecResult:
            await sandbox.files.write(staging, data)
            try:
                return await _helper(sandbox, _WRITE, full, self.root, "replace" if overwrite else "new", staging)
            except Exception:
                with contextlib.suppress(Exception):  # the script removes it when it runs
                    await sandbox.files.remove(staging)
                raise

        result = await self._call(put)
        _check(result, path)
        if result.exit_code == 3:
            if "Permission denied" in result.stderr:
                raise PermissionError(errno.EACCES, result.stderr.strip(), path)
            if "Not a directory" in result.stderr or "File exists" in result.stderr:
                raise NotADirectoryError(errno.ENOTDIR, "a parent path is a file", path)
            raise _os_error(result.stderr, path)
        if result.exit_code == 4:
            raise IsADirectoryError(errno.EISDIR, "this path is a directory", path)
        if result.exit_code == 5:
            raise FileExistsError(errno.EEXIST, "file exists", path)
        if result.exit_code != 0:
            raise _os_error(result.stderr, path)

    async def read(self, path: str) -> str | None:
        """The file's text, or None if there is no regular file at ``path``. Raises
        ValueError if it is not UTF-8."""
        full = self._full(path)

        async def get(sandbox: AsyncSandbox) -> bytes | None:
            try:
                # The file API refuses symlinks and paths outside /home/prized; the
                # shell serves those, checking they stay under the root.
                return await sandbox.files.read_bytes(full)
            except PrizedError as e:
                if e.code in ("not_regular_file", "not_directory"):
                    return None
                if e.code not in ("symlink_not_allowed", "path_outside_root", "permission_denied"):
                    raise
            result = await _helper(sandbox, _READ, full, self.root)
            _check(result, path)
            if result.exit_code == 3:
                return None
            if result.exit_code != 0:
                raise _os_error(result.stderr, path)
            if result.truncated:
                raise OSError(errno.EFBIG, "too large to read through the shell (about 3 MiB at most)", path)
            return base64.b64decode(result.stdout)

        raw = await self._call(get, missing=None)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(f"File '{posixpath.basename(full)}' is not UTF-8 text and cannot be read.") from e

    async def delete(self, path: str) -> bool:
        """Delete the file; False if there was no regular file at ``path``."""
        full = self._full(path)
        result = await self._call(lambda sb: _helper(sb, _DELETE, full, self.root))
        _check(result, path)
        if result.exit_code == 3:
            return False
        if result.exit_code != 0:
            raise _os_error(result.stderr, path)
        return True

    async def list_children(self, directory: str = "") -> list[FileStoreEntry]:
        """The directories, then the regular files, directly in ``directory`` (symlinks
        left out); empty if it doesn't exist."""
        full = self._full(directory, is_directory=True)
        result = await self._call(lambda sb: _helper(sb, _LIST, full, self.root))
        _check(result, directory)
        if result.exit_code == 3:
            return []
        if result.exit_code != 0:
            raise _os_error(result.stderr, directory)
        if result.truncated:
            raise OSError(errno.EFBIG, "too many entries to list; use the shell", directory)
        found = [item.split("/", 1) for item in result.stdout.split("\0") if item]
        dirs = sorted(n for kind, n in found if kind == "d")
        files = sorted(n for kind, n in found if kind == "f")
        return [FileStoreEntry(n, FileStoreEntry.DIRECTORY) for n in dirs] + [
            FileStoreEntry(n, FileStoreEntry.FILE) for n in files
        ]

    async def file_exists(self, path: str) -> bool:
        """Whether a regular file is at ``path``."""
        full = self._full(path)
        result = await self._call(lambda sb: _helper(sb, _EXISTS, full, self.root))
        _check(result, path)
        return result.exit_code == 0

    async def create_directory(self, path: str) -> None:
        """Make ``path`` a directory, with its parents."""
        full = self._full(path, is_directory=True)
        result = await self._call(lambda sb: _helper(sb, _MKDIR, full, self.root))
        _check(result, path)
        if result.exit_code != 0:
            raise _os_error(result.stderr, path)

    # ---- internals ----

    def _full(self, path: str, *, is_directory: bool = False) -> str:
        """The absolute path in the sandbox for a store path, which may not leave the root."""
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        cleaned = path.strip().replace("\\", "/")
        if "\n" in cleaned or "\0" in cleaned:
            raise ValueError(f"Invalid path: {path!r}. Paths cannot contain newlines.")
        if cleaned.startswith("/") or _DRIVE.match(cleaned):
            raise ValueError(f"Invalid path: {path!r}. Use a path relative to the store's root.")
        if not is_directory and cleaned.endswith("/"):
            raise ValueError(f"Invalid path: {path!r}. A file path cannot end with a separator.")
        parts = [p for p in cleaned.split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise ValueError(f"Invalid path: {path!r}. '.' and '..' segments are not allowed.")
        if not parts and not is_directory:
            raise ValueError("Invalid path: a file path cannot be empty.")
        return posixpath.join(self.root, *parts) if parts else self.root

    async def _call(self, op: Callable[[AsyncSandbox], Awaitable[T]], **fallback: T) -> T:
        """Runs ``op`` on the shell's sandbox, again on a replacement if it was gone.
        With ``missing=...``, a ``not_found`` from a sandbox that is still there means
        the path is missing, and that value is returned."""
        shell = self._shell
        try:
            sandbox = await shell._ensure()
            try:
                return await op(sandbox)
            except PrizedError as e:
                if e.code not in _MAYBE_GONE_CODES:
                    raise
                retry = await shell._acquire(failed=sandbox)
                if retry is sandbox or retry is None:
                    if e.code == "not_found" and "missing" in fallback:
                        return fallback["missing"]
                    raise
                try:
                    return await op(retry)
                except PrizedError as again:
                    if again.code == "not_found" and "missing" in fallback:
                        return fallback["missing"]
                    raise
        except (PrizedError, RuntimeError) as e:
            if isinstance(e, ShellExecutionError):
                raise
            raise ShellExecutionError(f"The Prized sandbox could not do that: {e}") from e


async def _helper(sandbox: AsyncSandbox, script: str, full: str, root: str, *more: str) -> PrizedExecResult:
    result: PrizedExecResult = await sandbox.exec(
        ["bash", "-c", script, "store", root, full, *more], cwd="/", timeout=_HELPER_TIMEOUT
    )
    return result


def _check(result: PrizedExecResult, path: str) -> None:
    if result.exit_code == _OUTSIDE:
        raise ValueError(f"Invalid path: {path!r}. A symbolic link leads it out of the store's root.")


def _os_error(stderr: str, path: str) -> OSError:
    """The OSError a failed shell helper stands for."""
    message = stderr.strip() or "command failed"
    for needle, number, kind in (
        ("No such file or directory", errno.ENOENT, FileNotFoundError),
        ("Is a directory", errno.EISDIR, IsADirectoryError),
        ("Permission denied", errno.EACCES, PermissionError),
        ("Not a directory", errno.ENOTDIR, NotADirectoryError),
    ):
        if needle in message:
            return kind(number, message, path)
    return OSError(errno.EIO, message, path)
