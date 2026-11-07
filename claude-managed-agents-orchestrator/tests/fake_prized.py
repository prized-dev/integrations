"""A stand-in for ``prized.AsyncSandbox``, with no network and no key.

Each fake sandbox is a directory standing in for ``/home/prized``. Commands really run, with bash,
``HOME`` pointed at that directory, so the orchestrator's in-sandbox scripts (and ant's install,
checked against its sha256) are tested as they are. Every process a sandbox starts carries
``PRIZED_FAKE_SANDBOX=<id>`` in its environment, detached ones too, so ``pause()`` can freeze them
all (SIGSTOP) as a real pause keeps memory and processes, ``resume()`` thaws them, and
``delete()`` kills them. A command sent to a paused sandbox resumes it first, as the real edge does.
``fork()`` copies the files (the real one copies memory too).

With ``World.auto_pause_after`` set, a sandbox with no command or file call for that long pauses
itself, which is what a real one does after its ``auto_pause_min``.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from prized import PrizedError

HOME = "/home/prized"
MARK = "PRIZED_FAKE_SANDBOX"


@dataclass
class Result:
    stdout: str
    stderr: str
    exit_code: int


class World:
    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="fake-prized-")
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.calls: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []  # create() keywords, for assertions
        self.execs: list[tuple[str, dict[str, str]]] = []  # (sandbox id, env) of every command
        self.ids = itertools.count(1)
        self.fail: dict[str, PrizedError] = {}  # "create", "resume", "fork", "write": raise this once
        self.fail_exec: list[tuple[str, PrizedError]] = []  # (substring of the command, error), once each
        # The same, but the command runs first and its answer is lost on the way back.
        self.fail_after_exec: list[tuple[str, PrizedError]] = []
        self.auto_pause_after: float | None = None
        self._watch: asyncio.Task[Any] | None = None
        # The account's side (GET /api/v1/me and ?ended=1): whether the workspace is on the free
        # tier (None: the key can't read billing), and the sandboxes the free tier deleted.
        self.trial: bool | None = None
        self.ended: dict[str, dict[str, Any]] = {}

    def end_at_run_limit(self, sb: FakeSandbox) -> None:
        """What the free tier does at a sandbox's 60 minutes: delete it, memory and files too."""
        self.ended[sb.id] = {
            "id": sb.id,
            "reason": "free_tier_run_limit",
            "limitMin": 60,
            "message": "Its 60 minutes of running on the free tier ran out; a top-up lifts this.",
        }
        sb._signal(signal.SIGCONT)
        sb._signal(signal.SIGKILL)
        sb.state = "deleted"
        self.calls.append(("run-limit", sb.id))

    def account(self) -> Any:
        world = self

        class Account:
            async def free_tier(self) -> tuple[bool | None, str]:
                if world.trial is not None:
                    return world.trial, "billing.trial in GET /api/v1/me"
                if world.ended:
                    return True, "the free tier's ended sandboxes"
                return None, "the key can't read billing"

            async def gone_reason(self, sandbox_id: str) -> str | None:
                row = world.ended.get(sandbox_id)
                if row is None:
                    return None
                return f"deleted at the free tier's {row['limitMin']}-minute run limit, memory and files with it"

        return Account()

    def close(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
        for sb in list(self.sandboxes.values()):
            sb._signal(signal.SIGCONT)
            sb._signal(signal.SIGKILL)
        shutil.rmtree(self.root, ignore_errors=True)

    def start_auto_pause(self, after: float) -> None:
        self.auto_pause_after = after

        async def watch() -> None:
            while True:
                await asyncio.sleep(0.05)
                for sb in list(self.sandboxes.values()):
                    if sb.state == "running" and time.monotonic() - sb.active > after:
                        await sb.pause()
                        self.calls.append(("auto-pause", sb.id))

        self._watch = asyncio.get_running_loop().create_task(watch())

    def live(self, **labels: str) -> list[FakeSandbox]:
        return [
            sb
            for sb in self.sandboxes.values()
            if sb.state != "deleted" and all(sb.labels.get(k) == v for k, v in labels.items())
        ]

    def _raise(self, what: str) -> None:
        err = self.fail.pop(what, None)
        if err is not None:
            raise err

    def api(self) -> Any:
        world = self

        class API:
            @staticmethod
            async def create(template: str = "prized-base-v1", **kw: Any) -> FakeSandbox:
                world._raise("create")
                world.created.append({"template": template, **kw})
                return world.new(template, kw)

            @staticmethod
            async def list(labels: dict[str, str] | None = None, state: Any = None, **_: Any) -> list[FakeSandbox]:
                return sorted(world.live(**(labels or {})), key=lambda sb: -sb.n)

            @staticmethod
            async def connect(sandbox_id: str, **_: Any) -> FakeSandbox:
                sb = world.sandboxes.get(sandbox_id)
                if sb is None or sb.state == "deleted":
                    raise PrizedError("not_found", status=404, sandbox_id=sandbox_id)
                return sb

        return API

    def new(self, template: str, kw: dict[str, Any], files_from: str | None = None) -> FakeSandbox:
        n = next(self.ids)
        sb = FakeSandbox(self, f"sbx_fake{n:04d}", n, template, dict(kw.get("labels") or {}), kw)
        if files_from:
            shutil.copytree(files_from, sb.home, symlinks=True, dirs_exist_ok=True)
        self.sandboxes[sb.id] = sb
        self.calls.append(("fork" if files_from else "create", sb.id))
        return sb


class Files:
    def __init__(self, sb: FakeSandbox) -> None:
        self.sb = sb

    async def write(self, path: str, data: str | bytes, *, mode: int | None = None) -> None:
        self.sb._touch()
        self.sb.world._raise("write")
        dest = self.sb.local(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
        os.chmod(dest, 0o600 if mode is None else mode)
        self.sb.world.calls.append(("write", self.sb.id))

    async def read(self, path: str) -> str:
        self.sb._touch()
        with open(self.sb.local(path)) as fh:
            return fh.read()


class FakeSandbox:
    def __init__(
        self, world: World, sid: str, n: int, template: str, labels: dict[str, str], options: dict[str, Any]
    ) -> None:
        self.world = world
        self.id = sid
        self.n = n
        self.template = template
        self.labels = labels
        self.options = options
        self.state = "running"
        self.home = os.path.join(world.root, sid)
        os.makedirs(os.path.join(self.home, "work"), exist_ok=True)
        self.files = Files(self)
        self.active = time.monotonic()
        self.created_at = datetime.now(timezone.utc)
        self.last_active_at = self.created_at
        self.expires_at: datetime | None = None
        self.error_reason: str | None = None

    def __repr__(self) -> str:
        return f"FakeSandbox({self.id}, {self.state})"

    def _touch(self) -> None:
        self._gone()
        self.active = time.monotonic()
        self.last_active_at = datetime.now(timezone.utc)

    def local(self, path: str) -> str:
        if path.startswith(HOME):
            return self.home + path[len(HOME) :]
        return os.path.join(self.home, "work", path)

    def _gone(self) -> None:
        if self.state == "deleted":
            raise PrizedError("sandbox_deleted", status=404, sandbox_id=self.id)

    def _env(self, env: dict[str, str] | None) -> dict[str, str]:
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
            "USER": "prized",
            "LANG": "C.UTF-8",
            "PRIZED_SANDBOX_ID": self.id,
            MARK: self.id,
        }
        for k, v in (env or {}).items():
            base[k] = v.replace(HOME, self.home)
        return base

    async def exec(
        self,
        cmd: str,
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        cwd: str | None = None,
        **_: Any,
    ) -> Result:
        self._touch()
        for i, (needle, err) in enumerate(self.world.fail_exec):
            if needle in cmd:
                del self.world.fail_exec[i]
                raise err
        if self.state == "paused":
            await self.resume()
        self.world.execs.append((self.id, dict(env or {})))
        workdir = self.local(cwd) if cwd else os.path.join(self.home, "work")
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            cmd,
            cwd=workdir,
            env=self._env(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout or 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return Result("", "timed out", -1)
        except BaseException:
            # Cancelled mid-command (a shutdown): don't leave the process to a closed loop.
            if proc.returncode is None:
                proc.kill()
            await asyncio.shield(proc.wait())
            raise
        self._touch()
        for i, (needle, error) in enumerate(self.world.fail_after_exec):
            if needle in cmd:
                del self.world.fail_after_exec[i]
                raise error
        return Result(out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0)

    def pids(self) -> list[int]:
        """Every process this sandbox started, found by the marker in its environment."""
        want = f"{MARK}={self.id}".encode()
        found = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/environ", "rb") as fh:
                    if want in fh.read().split(b"\0"):
                        found.append(int(name))
            except OSError:
                continue
        return found

    def _signal(self, sig: int) -> None:
        for pid in self.pids():
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    async def pause(self, **_: Any) -> None:
        self._gone()
        if self.state != "paused":
            self._signal(signal.SIGSTOP)
            self.state = "paused"
            self.world.calls.append(("pause", self.id))

    async def resume(self, **_: Any) -> None:
        self._gone()
        self.world._raise("resume")
        if self.state == "paused":
            self._signal(signal.SIGCONT)
            self.state = "running"
            self.active = time.monotonic()
            self.world.calls.append(("resume", self.id))

    async def refresh(self) -> FakeSandbox:
        return self

    async def keep_alive(self) -> None:
        self._touch()

    async def fork(self, count: int = 1, *, labels: dict[str, str] | None = None, **_: Any) -> list[FakeSandbox]:
        self._gone()
        self.world._raise("fork")
        return [
            self.world.new(
                self.template,
                {
                    "labels": labels if labels is not None else self.labels,
                    **{k: v for k, v in self.options.items() if k != "labels"},
                },
                files_from=self.home,
            )
            for _ in range(count)
        ]

    async def delete(self, **_: Any) -> None:
        if self.state != "deleted":
            self._signal(signal.SIGCONT)
            self._signal(signal.SIGKILL)
            self.state = "deleted"
            self.world.calls.append(("delete", self.id))
