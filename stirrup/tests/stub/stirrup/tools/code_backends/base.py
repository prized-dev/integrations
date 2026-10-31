"""Stand-in for stirrup.tools.code_backends.base (0.2.1), written for these tests.

Same names, fields, abstract methods and signatures as Stirrup's; the bodies are minimal
versions of the documented behaviour (allowlist parsing, output path rules, cross-environment
upload), enough to drive a backend the way Stirrup's Agent does.
"""

from __future__ import annotations

import re
import shlex
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath
from typing import Literal

from pydantic import BaseModel
from stirrup.core.models import ImageContentBlock, Tool, ToolProvider, ToolResult, ToolUseCountMetadata

SHELL_TIMEOUT = 60 * 5
RejectionReason = Literal["empty_command", "no_pattern_match", "shell_syntax", "shell_assignment"]
_SHELL_PUNCTUATION = ";<>|&()`\n"


@dataclass(frozen=True)
class OutputSourceRoot:
    path: str
    host_path: Path | None = None


class CodeExecutionParams(BaseModel):
    cmd: str


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    error_kind: str | None = None
    advice: str | None = None


@dataclass
class SavedFile:
    source_path: str
    output_path: PurePath
    size: int


@dataclass
class SaveOutputFilesResult:
    saved: list[SavedFile] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


@dataclass
class UploadedFile:
    source_path: Path
    dest_path: str
    size: int


@dataclass
class UploadFilesResult:
    uploaded: list[UploadedFile] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def format_result(result: CommandResult) -> ToolResult[ToolUseCountMetadata]:
    if result.error_kind:
        content = f"<shell_results>\n<error_kind>{result.error_kind}</error_kind>\n<details>{result.stderr}</details>"
        if result.advice:
            content += f"\n<advice>{result.advice}</advice>"
        content += "\n</shell_results>"
    else:
        content = (
            f"<shell_results>\n<exit_code>{result.exit_code}</exit_code>\n<stdout>{result.stdout}</stdout>"
            f"\n<stderr>{result.stderr}</stderr>\n</shell_results>"
        )
    return ToolResult(content=content, metadata=ToolUseCountMetadata())


def _unquoted_operator(cmd: str) -> bool:
    quote: str | None = None
    escaped = False
    for char in cmd:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char in {"'", '"'}:
            if quote is None:
                quote = char
            elif char == quote:
                quote = None
        elif quote is None and char in _SHELL_PUNCTUATION:
            return True
    return False


def _relative_output_path(source_path: str, roots: tuple[OutputSourceRoot, ...]) -> PurePosixPath:
    source = PurePosixPath(source_path)
    if not source_path or "\x00" in source_path or ".." in source.parts or not source.name:
        raise ValueError(f"Output source path is invalid: {source_path}")
    if not source.is_absolute():
        return source
    for root in roots:
        try:
            return source.relative_to(root.path)
        except ValueError:
            continue
    raise ValueError(f"Output source path is outside known execution roots: {source_path}")


class CodeExecToolProvider(ToolProvider):
    def __init__(self, *, allowed_commands: list[str] | None = None, shell_timeout: int = SHELL_TIMEOUT) -> None:
        self._allowed_commands = allowed_commands
        self._shell_timeout = shell_timeout
        self._compiled_allowed: list[re.Pattern[str]] | None = None
        if allowed_commands is not None:
            self._compiled_allowed = [re.compile(p) for p in allowed_commands]

    @property
    def temp_dir(self) -> Path | None:
        return None

    def _prepare_command(self, cmd: str) -> tuple[list[str] | None, CommandResult | None]:
        if self._compiled_allowed is None:
            return None, None
        try:
            argv = shlex.split(cmd)
        except ValueError:
            return None, self._rejection(cmd, "shell_syntax")
        if not argv:
            return None, self._rejection(cmd, "empty_command")
        if _unquoted_operator(cmd):
            return None, self._rejection(cmd, "shell_syntax")
        if "=" in argv[0]:
            return None, self._rejection(cmd, "shell_assignment")
        if not any(p.match(shlex.join(argv)) for p in self._compiled_allowed):
            return None, self._rejection(cmd, "no_pattern_match")
        return argv, None

    def _rejection(self, cmd: str, reason: RejectionReason) -> CommandResult:
        return CommandResult(
            exit_code=1,
            stdout="",
            stderr=f"Command not allowed: '{cmd}'",
            error_kind="command_not_allowed",
            advice=reason,
        )

    @abstractmethod
    async def __aenter__(self) -> Tool[CodeExecutionParams, ToolUseCountMetadata]: ...

    @abstractmethod
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None: ...

    @abstractmethod
    async def run_command(self, cmd: str, *, timeout: int | None = None) -> CommandResult: ...

    @abstractmethod
    async def read_file_bytes(self, path: str) -> bytes: ...

    @abstractmethod
    async def write_file_bytes(self, path: str, content: bytes) -> None: ...

    @abstractmethod
    async def file_exists(self, path: str) -> bool: ...

    @abstractmethod
    async def is_directory(self, path: str) -> bool: ...

    @abstractmethod
    async def list_files(self, path: str) -> list[str]: ...

    @abstractmethod
    async def view_image(self, path: str) -> ImageContentBlock: ...

    def output_source_roots(self) -> tuple[OutputSourceRoot, ...]:
        return ()

    async def resolve_output_source(self, source_path: str) -> str:
        return source_path

    async def output_destination_identity(self, destination: str, output_root: Path | str) -> str:
        normalized = PurePosixPath(destination)
        if ".." in normalized.parts:
            raise ValueError(f"Output destination escapes output directory: {destination}")
        try:
            normalized.relative_to(PurePosixPath(str(output_root)))
        except ValueError as exc:
            raise ValueError(f"Output destination escapes output directory: {destination}") from exc
        return normalized.as_posix()

    async def save_output_files(
        self, paths: list[str], output_dir: Path | str, dest_env: CodeExecToolProvider | None = None
    ) -> SaveOutputFilesResult:
        """Each source keeps its path relative to its root; sources are resolved, then read."""
        result = SaveOutputFilesResult()
        claimed: dict[str, str] = {}
        for source_path in dict.fromkeys(paths):
            try:
                relative = _relative_output_path(source_path, self.output_source_roots())
                content = await self.read_file_bytes(await self.resolve_output_source(source_path))
                if dest_env is not None:
                    destination = PurePosixPath(str(output_dir)) / relative
                    identity = await dest_env.output_destination_identity(destination.as_posix(), output_dir)
                    if identity in claimed:
                        raise ValueError(f"Output destination collision: {destination}")
                    claimed[identity] = source_path
                    await dest_env.write_file_bytes(destination.as_posix(), content)
                    result.saved.append(SavedFile(source_path, destination, len(content)))
                else:
                    local = Path(output_dir).joinpath(*relative.parts)
                    if str(local) in claimed:
                        raise ValueError(f"Output destination collision: {local}")
                    claimed[str(local)] = source_path
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_bytes(content)
                    result.saved.append(SavedFile(source_path, local, len(content)))
            except Exception as exc:
                result.failed[source_path] = str(exc)
        return result

    async def upload_files(
        self, *paths: Path | str, source_env: CodeExecToolProvider | None = None, dest_dir: str | None = None
    ) -> UploadFilesResult:
        """Cross-environment copies only: the local case is every real backend's own override."""
        result = UploadFilesResult()
        dest = dest_dir or ""
        for path in paths:
            path_str = str(path)
            try:
                assert source_env is not None, "the stand-in only does cross-environment uploads"
                if await source_env.is_directory(path_str):
                    name = Path(path_str).name
                    for rel in await source_env.list_files(path_str):
                        target = f"{dest}/{rel}" if dest else f"{name}/{rel}"
                        content = await source_env.read_file_bytes(f"{path_str}/{rel}")
                        await self.write_file_bytes(target, content)
                        result.uploaded.append(UploadedFile(Path(f"{path_str}/{rel}"), target, len(content)))
                else:
                    content = await source_env.read_file_bytes(path_str)
                    target = f"{dest}/{Path(path_str).name}" if dest else Path(path_str).name
                    await self.write_file_bytes(target, content)
                    result.uploaded.append(UploadedFile(Path(path_str), target, len(content)))
            except Exception as exc:
                result.failed[path_str] = str(exc)
        return result

    def get_code_exec_tool(
        self, *, name: str = "code_exec", description: str | None = None
    ) -> Tool[CodeExecutionParams, ToolUseCountMetadata]:
        env = self

        async def executor(params: CodeExecutionParams) -> ToolResult[ToolUseCountMetadata]:
            return format_result(await env.run_command(params.cmd))

        return Tool[CodeExecutionParams, ToolUseCountMetadata](
            name=name,
            description=description or "Execute a shell command in the execution environment.",
            parameters=CodeExecutionParams,
            executor=executor,
        )
