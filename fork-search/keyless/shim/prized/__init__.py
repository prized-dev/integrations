"""A stand-in for the prized SDK inside a keyless sandbox.

Harbor and harbor-prized run in a sandbox that holds no Prized key. Each SDK
call becomes a request file in SPOOL/req; a broker outside the sandbox, which
holds the key, carries it out and writes the answer to SPOOL/resp. Only the
calls harbor-prized makes are here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid

SPOOL = os.environ.get("PRIZED_BROKER_SPOOL", "/home/prized/rpc")
__version__ = "0.1.2+broker"


class PrizedError(Exception):
    def __init__(self, code: str = "error", message: str | None = None, status: int | None = None):
        super().__init__(message or code)
        self.code, self.status = code, status


class CommandTimeoutError(PrizedError):
    def __init__(self, message: str = "command timed out", timeout: float | None = None):
        super().__init__("command_timeout", message)
        self.timeout = timeout


class ExecResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0):
        self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code


async def _call(op: str, **args):
    rid = uuid.uuid4().hex
    tmp, req = f"{SPOOL}/req/.{rid}.tmp", f"{SPOOL}/req/{rid}.json"
    with open(tmp, "w") as f:
        json.dump({"id": rid, "op": op, "args": args}, f)
    os.rename(tmp, req)
    resp, pause = f"{SPOOL}/resp/{rid}.json", 0.02
    while True:
        await asyncio.sleep(pause)
        pause = min(pause * 1.5, 0.5)
        try:
            with open(resp) as f:
                answer = json.load(f)
        except (OSError, ValueError):  # not there yet, or still being written
            continue
        os.remove(resp)
        break
    error = answer.get("error")
    if error:
        if error.get("type") == "CommandTimeoutError":
            raise CommandTimeoutError(error.get("message") or "command timed out")
        raise PrizedError(error.get("code") or "error", error.get("message"), error.get("status"))
    return answer.get("result")


class _Files:
    def __init__(self, sandbox: "AsyncSandbox"):
        self._sandbox = sandbox

    async def write(self, path: str, data, mode: int | None = None) -> None:
        if isinstance(data, str):
            data = data.encode()
        await _call("write", sandbox=self._sandbox.id, path=path,
                    data=base64.b64encode(data).decode(), mode=mode)

    async def read_bytes(self, path: str) -> bytes:
        return base64.b64decode(await _call("read", sandbox=self._sandbox.id, path=path))


class AsyncSandbox:
    def __init__(self, data: dict):
        self.id, self.state = data["id"], data.get("state")
        self.files = _Files(self)

    @classmethod
    async def create(cls, template: str | None = None, **kwargs) -> "AsyncSandbox":
        return cls(await _call("create", template=template, **kwargs))

    async def exec(self, command: str, timeout: float | None = None, full_output: bool = False):
        r = await _call("exec", sandbox=self.id, command=command, timeout=timeout)
        return ExecResult(r["stdout"], r["stderr"], r["exit_code"])

    async def fork(self, count: int = 1, *, labels: dict | None = None, **_):
        return [AsyncSandbox(d) for d in await _call("fork", sandbox=self.id, count=count, labels=labels)]

    async def keep_alive(self) -> None:
        await _call("keep_alive", sandbox=self.id)

    async def pause(self) -> None:
        await _call("pause", sandbox=self.id)

    async def delete(self) -> None:
        await _call("delete", sandbox=self.id)

    async def close(self) -> None:
        await _call("close", sandbox=self.id)


async def broker_copy(src: str, src_path: str, dst: str, dst_path: str) -> dict:
    """Not in the real SDK: the broker copies a file between two sandboxes
    itself, so it never passes through this sandbox (forksearch transplants)."""
    return await _call("copy", src=src, src_path=src_path, dst=dst, dst_path=dst_path)


__all__ = ["AsyncSandbox", "CommandTimeoutError", "ExecResult", "PrizedError", "broker_copy"]
