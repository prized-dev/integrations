"""An in-process stand-in for the Prized SDK's ``AsyncSandbox``.

Commands run for real, with this machine's bash, so the shell agent-env and the provider send (base64 writes, joining
uploads, sha256 checks) is exercised as written; tests keep every path they touch under a temporary directory. A test
answers particular commands itself through ``FakeCloud.answer``, as the root probe and Docker's readiness are answered.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prized import PrizedError

ANSWER = Callable[[str], "Result | None"]


@dataclass
class Result:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False


@dataclass
class Info:
    raw: dict[str, Any]


@dataclass
class Record:
    """One sandbox as the fake API keeps it."""

    id: str
    template: str
    labels: dict[str, str]
    create_kwargs: dict[str, Any]
    state: str = "running"
    raw: dict[str, Any] = field(default_factory=dict)


class FakeCloud:
    """The fake API's state, shared by every ``FakeSandbox`` a test makes."""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.sandboxes: dict[str, Record] = {}
        self.commands: list[str] = []
        self.uploads: list[str] = []
        self.connects = 0
        self.closed = 0
        self.deletes: list[str] = []
        self._ids = itertools.count(1)
        self._answers: list[ANSWER] = []
        # Set by tests: what a create answers in the document, an error a create raises, an error execs raise.
        self.document: dict[str, Any] = {}
        self.create_error: PrizedError | None = None
        self.exec_error: Callable[[str], PrizedError | None] | None = None
        self.write_hook: Callable[[str, bytes], bytes] | None = None

    def answer(self, responder: ANSWER) -> None:
        """Answer the commands ``responder`` returns a ``Result`` for, before running anything."""
        self._answers.append(responder)

    def answer_matching(self, needle: str, result: Result) -> None:
        self.answer(lambda command: result if needle in command else None)

    def cls(self) -> type[FakeSandbox]:
        cloud = self

        class Bound(FakeSandbox):
            _cloud = cloud

        return Bound

    def new_record(self, template: str, labels: dict[str, str], kwargs: dict[str, Any]) -> Record:
        sandbox_id = f"sbx_{next(self._ids):016x}"
        record = Record(sandbox_id, template, dict(labels), kwargs, raw={"id": sandbox_id, **self.document})
        self.sandboxes[sandbox_id] = record
        return record

    async def run(self, command: str) -> Result:
        self.commands.append(command)
        if self.exec_error is not None and (error := self.exec_error(command)) is not None:
            raise error
        for responder in self._answers:
            if (result := responder(command)) is not None:
                return result
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workdir,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.workdir)},
        )
        out, err = await process.communicate()
        return Result(
            out.decode("utf-8", "surrogateescape"), err.decode("utf-8", "surrogateescape"), process.returncode or 0
        )


def not_found() -> PrizedError:
    return PrizedError("not_found", status=404)


def error(code: str, status: int | None = None) -> PrizedError:
    return PrizedError(code, status=status)


class FakeFiles:
    def __init__(self, sandbox: FakeSandbox):
        self._sandbox = sandbox

    async def write(self, path: str, data: bytes, *, mode: int | None = None) -> Any:
        cloud = self._sandbox._cloud
        cloud.uploads.append(path)
        if cloud.write_hook is not None:
            data = cloud.write_hook(path, data)
        Path(path).write_bytes(data)
        return {"path": path, "size": len(data)}


class FakeSandbox:
    """``prized.AsyncSandbox``'s surface that the provider uses."""

    _cloud: FakeCloud

    def __init__(self, record: Record):
        self._record = record
        self.files = FakeFiles(self)
        self.closed = False

    @property
    def id(self) -> str:
        return self._record.id

    @property
    def state(self) -> str:
        return self._record.state

    @property
    def labels(self) -> dict[str, str]:
        return dict(self._record.labels)

    @property
    def info(self) -> Info:
        return Info(self._record.raw)

    @classmethod
    async def create(cls, template: str = "prized-base-v1", **kwargs: Any) -> FakeSandbox:
        cloud = cls._cloud
        if cloud.create_error is not None:
            raise cloud.create_error
        return cls(cloud.new_record(template, kwargs.get("labels") or {}, kwargs))

    @classmethod
    async def connect(cls, sandbox_id: str, **kwargs: Any) -> FakeSandbox:
        cloud = cls._cloud
        cloud.connects += 1
        record = cloud.sandboxes.get(sandbox_id)
        if record is None or record.state == "deleted":
            raise not_found()
        return cls(record)

    async def exec(self, cmd: list[str] | str, *, timeout: float | None = None, **kwargs: Any) -> Result:
        assert timeout == 0, "the provider runs every command detached, with no time limit"
        if self._record.state in ("deleting", "deleted"):
            raise error("sandbox_deleted", 410)
        command = cmd if isinstance(cmd, str) else " ".join(shlex.quote(part) for part in cmd)
        return await self._cloud.run(command)

    async def delete(self, *, wait: bool = False, timeout: float = 120.0) -> None:
        self._cloud.deletes.append(self._record.id)
        if self._record.state == "deleted":
            # The SDK reads a 404 on delete as already deleted.
            return
        self._record.state = "deleted"

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._cloud.closed += 1
