"""Prized Sandboxes environment for Harbor: ``--env harbor_prized:PrizedEnvironment``.

A Prized sandbox is a Firecracker microVM started from a fixed template, not
from a Docker image, and its user has no root. To run a task's image anyway,
this environment pulls the task's prebuilt ``docker_image`` into a root
filesystem inside the sandbox (with ``crane export``) and runs every command
in that filesystem as root of an unprivileged user namespace (``unshare`` +
``chroot``). Commands see the image's files, ``ENV`` and ``WORKDIR``, run as
uid 0 inside the namespace, and can ``apt-get install``.

Limits, by design of that approach:

- Tasks need ``[environment].docker_image``. Building from the task's
  Dockerfile is not supported; build and push the image first.
- Only uid 0 is mapped, so ``user=`` switching is ignored and a ``chown`` to
  another user fails. ``TAR_OPTIONS=--no-same-owner`` is set so archives
  unpack, and apt and dpkg finish with warnings.
- No Docker Compose, GPUs, network allowlists or disabled internet.
- A command runs for at most an hour (the sandbox exec limit), and a sandbox
  runs 24 hours by default (up to 7 days on a paid plan).

Credentials: ``PRIZED_API_KEY`` (a key with Sandboxes: Write). Extra
constructor parameters (``--ek``): ``template`` (default ``prized-base-v1``),
``max_runtime_min`` (default 180), ``image_headroom_gb`` (disk added for
the unpacked image, default 20), ``labels`` (a dict, or ``k=v,k=v``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, override

import httpx
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.environments.tar_transfer import (
    extract_dir_from_bytes,
    pack_dir_to_bytes,
    remote_pack_command,
    remote_unpack_command,
)
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

_API = os.environ.get("PRIZED_API_URL", "https://api.prized.dev/api/v1")
_HOME = "/home/prized"
_ROOT = f"{_HOME}/rootfs"  # the image's filesystem, as seen from the sandbox
_XFER = "/.harbor-xfer"  # staging dir inside the image's filesystem
_ENTER = f"{_HOME}/harbor-enter.sh"
_CRANE_URL = (
    "https://github.com/google/go-containerregistry/releases/download/"
    "v0.20.3/go-containerregistry_Linux_x86_64.tar.gz"
)
_MAX_EXEC_SEC = 3600

_INLINE_LIMIT = 60_000  # longer scripts are uploaded instead of passed inline

# Runs a script as root of a fresh user namespace, chrooted into the image:
# either "-" plus the script base64-encoded, or the path of an uploaded script
# inside the image. Each call gets its own mount namespace, so /proc, /dev and
# /sys are bound per command; files persist because they live on the disk.
_ENTER_SCRIPT = f"""#!/bin/bash
set -e
if [ "$1" = - ]; then
  f={_ROOT}{_XFER}/cmd-$$-$RANDOM.sh
  printf %s "$2" | base64 -d > "$f"
else
  f={_ROOT}$1
fi
exec unshare -Urm --fork /bin/bash -c '
R=$0; f=$1
mount -t proc proc "$R/proc" 2>/dev/null || mount --rbind /proc "$R/proc"
mount --rbind /dev "$R/dev"
mount --rbind /sys "$R/sys" 2>/dev/null || true
cd "$R"
exec /usr/bin/env -i HOME=/root USER=root LOGNAME=root TERM=xterm \\
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\
  TAR_OPTIONS=--no-same-owner /usr/sbin/chroot "$R" /bin/bash "${{f#$R}}"' {_ROOT} "$f"
"""


class PrizedEnvironment(BaseEnvironment):
    """Runs a task's prebuilt image inside a Prized sandbox (see module doc)."""

    @classmethod
    @override
    def preflight(cls) -> None:
        if not os.environ.get("PRIZED_API_KEY"):
            raise SystemExit(
                "Prized requires PRIZED_API_KEY, a key with Sandboxes: Write "
                "(https://prized.dev/dashboard/workspace/api-keys?via=harbor)."
            )

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        *args,
        template: str = "prized-base-v1",
        max_runtime_min: int = 180,
        image_headroom_gb: int = 20,
        labels: dict[str, str] | str | None = None,
        **kwargs,
    ):
        if isinstance(labels, str):  # --ek labels=team=evals,run=42
            labels = dict(p.split("=", 1) for p in labels.split(",") if "=" in p)
        self._template = template
        self._max_runtime_min = int(max_runtime_min)
        self._image_headroom_gb = int(image_headroom_gb)
        self._labels = {"harbor": "1", **(labels or {})}
        self._sandbox_id: str | None = None
        self._toolbox: str | None = None
        self._image_env: dict[str, str] = {}
        self._image_workdir: str = "/"
        self._http: httpx.AsyncClient | None = None
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            **kwargs,
        )

    @staticmethod
    @override
    def type() -> str:
        return "prized"

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities()

    @override
    def _validate_definition(self) -> None:
        if not self.task_env_config.docker_image:
            raise ValueError(
                "Prized runs a task's prebuilt image: set [environment].docker_image "
                "in task.toml. Building from the task's Dockerfile is not supported."
            )

    # -- HTTP -------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {os.environ['PRIZED_API_KEY']}"},
                timeout=httpx.Timeout(60.0, read=_MAX_EXEC_SEC + 60.0),
            )
        return self._http

    async def _call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = await self._client().request(method, url, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Prized {method} {url} answered {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response

    async def _sandbox(self) -> dict[str, Any]:
        response = await self._call("GET", f"{_API}/sandboxes/{self._sandbox_id}")
        return response.json()["sandbox"]

    async def _host_exec(self, command: str, timeout_sec: int = 600) -> ExecResult:
        """Runs a command on the sandbox itself, outside the image."""
        if self._toolbox is None:
            raise RuntimeError("Prized sandbox is not running; call start() first.")
        timeout = max(1, min(int(timeout_sec), _MAX_EXEC_SEC))
        for attempt in range(6):
            response = await self._client().post(
                f"{self._toolbox}/process/execute",
                json={"command": command, "timeout": timeout},
            )
            # 409/429/503 come before the command runs, so a retry is safe.
            if response.status_code in (409, 429, 503) and attempt < 5:
                await asyncio.sleep(attempt + 1)
                continue
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Prized exec answered {response.status_code}: {response.text[:300]}"
                )
            body = response.json()
            return ExecResult(
                stdout=body.get("stdout", ""),
                stderr=body.get("stderr", ""),
                return_code=body.get("exitCode", -1),
            )
        raise RuntimeError("unreachable")

    async def _put(self, host_path: str, data: bytes) -> None:
        await self._call(
            "POST",
            f"{self._toolbox}/files/upload",
            params={"path": host_path},
            content=data,
        )

    async def _get(self, host_path: str) -> bytes:
        response = await self._call(
            "GET", f"{self._toolbox}/files/download", params={"path": host_path}
        )
        return response.content

    # -- lifecycle --------------------------------------------------------

    @override
    async def start(self, force_build: bool) -> None:
        image = self.task_env_config.docker_image
        if image is None:
            raise ValueError("Prized needs [environment].docker_image.")
        cpus = max(1, int(self.task_env_config.cpus or 2))
        memory_mb = max(1024, int(self.task_env_config.memory_mb or 4096))
        # The image is unpacked onto the sandbox's disk, so add room for it on top
        # of the task's storage (disk costs storage rates only).
        storage_gb = -(-int(self.task_env_config.storage_mb or 10240) // 1024)
        disk_gb = min(128, max(10, storage_gb) + self._image_headroom_gb)
        body = {
            "template": self._template,
            "resources": {"vcpu": cpus, "memoryMb": memory_mb, "diskGb": disk_gb},
            "maxRuntimeMin": self._max_runtime_min,
            "labels": self._labels,
        }
        response = await self._call("POST", f"{_API}/sandboxes", json=body)
        self._sandbox_id = response.json()["sandbox"]["id"]
        self.logger.debug(f"Prized sandbox {self._sandbox_id} for {image}")
        try:
            await self._wait_running()
            await self._prepare_image(image)
            workdir = self.task_env_config.workdir or self._image_workdir
            await self.exec(f"mkdir -p {shlex.quote(workdir)}", cwd="/")
            await self.ensure_dirs(self._mount_targets(writable_only=True))
            await self._upload_environment_dir_after_start()
        except Exception:
            await self.stop(delete=True)
            raise

    async def _wait_running(self) -> None:
        for _ in range(600):
            sandbox = await self._sandbox()
            if sandbox["state"] == "running":
                self._toolbox = sandbox["toolboxBase"]
                break
            if sandbox["state"] not in ("starting",):
                raise RuntimeError(
                    f"Prized sandbox {self._sandbox_id} is {sandbox['state']}: "
                    f"{sandbox.get('errorReason')}"
                )
            await asyncio.sleep(1)
        else:
            raise TimeoutError(f"Prized sandbox {self._sandbox_id} never started")
        # A new sandbox's first request can answer 502 for a moment.
        for attempt in range(10):
            try:
                if (await self._host_exec("true", 30)).return_code == 0:
                    return
            except (httpx.HTTPError, RuntimeError):
                pass
            await asyncio.sleep(1 + attempt)
        raise RuntimeError(f"Prized sandbox {self._sandbox_id} does not answer")

    async def _prepare_image(self, image: str) -> None:
        q = shlex.quote
        setup = (
            "set -e; cd ~; "
            f"[ -x ./crane ] || curl -fsSL {_CRANE_URL} | tar -xz crane; "
            f"mkdir -p {_ROOT}; "
            f"./crane export {q(image)} - | tar --no-same-owner -x -C {_ROOT} 2>tar.err || "
            f"{{ test -e {_ROOT}/bin/sh && ! grep -q 'No space' tar.err; }} || "
            "{ tail -5 tar.err >&2; df -h ~ >&2; exit 1; }; "
            f"mkdir -p {_ROOT}{_XFER} {_ROOT}/proc {_ROOT}/dev {_ROOT}/sys {_ROOT}/tmp; "
            f"chmod 1777 {_ROOT}/tmp; "
            f"rm -f {_ROOT}/etc/resolv.conf; cp /etc/resolv.conf {_ROOT}/etc/resolv.conf; "
            f"if [ -d {_ROOT}/etc/apt/apt.conf.d ]; then echo 'APT::Sandbox::User \"root\";' "
            f"> {_ROOT}/etc/apt/apt.conf.d/99harbor-prized; fi; "
            f"./crane config {q(image)}"
        )
        result = await self._host_exec(setup, timeout_sec=1800)
        if result.return_code != 0:
            raise RuntimeError(
                f"Could not unpack {image} in the sandbox: {result.stderr or result.stdout}"
            )
        config = json.loads(result.stdout or "{}").get("config") or {}
        for pair in config.get("Env") or []:
            name, _, value = pair.partition("=")
            self._image_env[name] = value
        self._image_workdir = config.get("WorkingDir") or "/"
        await self._put(_ENTER, _ENTER_SCRIPT.encode())
        await self._host_exec(f"chmod +x {_ENTER}", 30)

    @override
    async def stop(self, delete: bool) -> None:
        if self._sandbox_id is not None and delete:
            try:
                await self._call("DELETE", f"{_API}/sandboxes/{self._sandbox_id}")
            except (httpx.HTTPError, RuntimeError) as exc:
                self.logger.error(f"Error deleting Prized sandbox: {exc}")
        self._sandbox_id = None
        self._toolbox = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- commands and files -----------------------------------------------

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        user = self._resolve_user(user)
        if user is not None and str(user) not in ("root", "0"):
            self.logger.debug(f"Prized runs commands as root; ignoring user={user!r}")
        merged = {**self._image_env, **(self._merge_env(env) or {})}
        workdir = cwd or self.task_env_config.workdir or self._image_workdir
        lines = [f"export {k}={shlex.quote(v)}" for k, v in merged.items()]
        lines.append(f"cd {shlex.quote(workdir)} || exit 1")
        lines.append(command)
        script = "\n".join(lines).encode()
        if len(script) <= _INLINE_LIMIT:
            args = f"- {base64.b64encode(script).decode()}"
        else:
            staged = f"{_XFER}/cmd-{uuid.uuid4().hex[:8]}.sh"
            await self._put(_ROOT + staged, script)
            args = staged
        return await self._host_exec(
            f"{_ENTER} {args}", timeout_sec=timeout_sec or _MAX_EXEC_SEC
        )

    async def _checked(self, command: str, what: str) -> None:
        result = await self.exec(command, cwd="/", timeout_sec=600)
        if result.return_code != 0:
            raise RuntimeError(f"{what}: {result.stderr or result.stdout}")

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        staged = f"{_XFER}/up-{uuid.uuid4().hex[:8]}"
        await self._put(_ROOT + staged, source.read_bytes())
        mode = oct(source.stat().st_mode & 0o7777)[2:]
        target = shlex.quote(target_path)
        await self._checked(
            f'mkdir -p "$(dirname {target})" && mv {staged} {target} && chmod {mode} {target}',
            f"Failed to upload {source} to {target_path}",
        )

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        staged = f"{_XFER}/up-{uuid.uuid4().hex[:8]}.tar.gz"
        buffer = await asyncio.to_thread(pack_dir_to_bytes, source_dir, compress=True)
        await self._put(_ROOT + staged, buffer.getvalue())
        await self._checked(
            # Only uid 0 exists in the namespace, so never restore owners.
            f"{remote_unpack_command(staged, target_dir)} --no-same-owner "
            f"&& rm -f {staged}",
            f"Failed to extract upload archive into {target_dir!r}",
        )

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        staged = f"{_XFER}/down-{uuid.uuid4().hex[:8]}"
        await self._checked(
            f"cp -L {shlex.quote(source_path)} {staged}",
            f"Failed to read {source_path}",
        )
        data = await self._get(_ROOT + staged)
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        await self.exec(f"rm -f {staged}", cwd="/", timeout_sec=60)

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        staged = f"{_XFER}/down-{uuid.uuid4().hex[:8]}.tar.gz"
        await self._checked(
            remote_pack_command(source_dir, staged),
            f"Failed to archive {source_dir!r}",
        )
        data = await self._get(_ROOT + staged)
        await asyncio.to_thread(extract_dir_from_bytes, data, target_dir)
        await self.exec(f"rm -f {staged}", cwd="/", timeout_sec=60)


__all__ = ["PrizedEnvironment"]
