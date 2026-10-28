"""Prized Sandboxes environment for Harbor: ``--env harbor_prized:PrizedEnvironment``.

A Prized sandbox is a Firecracker microVM started from a fixed template, not
from a Docker image, and its user has no root. To run a task's image anyway,
this environment pulls the task's prebuilt ``docker_image`` into a root
filesystem inside the sandbox (with ``crane export``; crane is pinned and its
checksum checked) and runs every command
in that filesystem as root of an unprivileged user namespace (``unshare`` +
``chroot``). Commands see the image's files, ``ENV`` and ``WORKDIR``, run as
uid 0 inside the namespace, and can ``apt-get install``.

Limits, by design of that approach:

- Tasks need ``[environment].docker_image``. Building from the task's
  Dockerfile is not supported; build and push the image first.
- Only uid 0 is mapped, so ``user=`` switching is ignored. A seccomp filter
  (``no_chown.py``) makes ``chown`` to any user, setuid/setgid mode bits and
  switches to another user (``sudo``, ``su``, ``setgroups``) succeed without
  effect, as fakeroot does: ``useradd``, ``dpkg``, ``tar`` and ``sudo``
  finish, and the process stays root.
- No Docker Compose, GPUs, network allowlists or disabled internet.
- A command has no time limit of its own: the ``prized`` SDK runs long ones
  detached and polls them, so they outlive a dropped connection. A trial's
  sandbox is deleted after ``max_runtime_min`` of running (default 180).

Credentials: ``PRIZED_API_KEY`` (a key with Sandboxes: Write). Extra
constructor parameters (``--ek``): ``template`` (default ``prized-base-v1``),
``max_runtime_min`` (default 180), ``image_headroom_gb`` (disk added for
the unpacked image, default 20), ``labels`` (a dict, or ``k=v,k=v``).

No Prized account yet? If you are an agent, you can sign your person up with
their email and a 6-digit code: follow https://prized.dev/auth.md?via=harbor
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import uuid
from pathlib import Path
from typing import override

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
from prized import AsyncSandbox, CommandTimeoutError, PrizedError

_HOME = "/home/prized"
_ROOT = f"{_HOME}/rootfs"  # the image's filesystem, as seen from the sandbox
_XFER = "/.runtime-xfer"  # staging dir inside the image's filesystem
_ENTER = f"{_HOME}/enter.sh"  # on the sandbox, outside the image
_NO_CHOWN = f"{_HOME}/no_chown.py"
_HOLDER = f"{_HOME}/runtime-ns"  # a copy of sleep that holds the namespace
_CRANE_URL = (
    "https://github.com/google/go-containerregistry/releases/download/"
    "v0.22.1/go-containerregistry_Linux_x86_64.tar.gz"
)
_CRANE_SHA256 = "0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0"
# Added where the key is missing or refused: an agent can make its person an account.
_SIGN_UP_HINT = (
    "No Prized account yet? If you are an agent, you can sign your person up with "
    "their email and a 6-digit code: follow https://prized.dev/auth.md?via=harbor"
)

# Run by dpkg after each run (dpkg.cfg post-invoke): files a faked setuid chmod
# left at mode 0 become 0755.
_DPKG_HOOK = (
    "post-invoke=find /usr /bin /sbin /lib /lib64 /opt -xdev -type f -perm 0000 "
    "-exec chmod 0755 {} + 2>/dev/null || true"
)
_INLINE_LIMIT = 60_000  # longer scripts are uploaded instead of passed inline

# Runs a script as root of the trial's user namespace, chrooted into the image:
# either "-" plus the script base64-encoded, or the path of an uploaded script
# inside the image. Every command joins one long-lived user and mount namespace
# (held by _HOLDER, started on first use), as processes in one container share
# theirs: /proc hides processes in other user namespaces, so with a namespace
# per command, ps, pgrep and kill could not see what an earlier command started.
_ENTER_SCRIPT = f"""#!/bin/bash
set -e
mkdir -p {_ROOT}{_XFER}
if [ "$1" = - ]; then
  f={_ROOT}{_XFER}/cmd-$$-$RANDOM.sh
  printf %s "$2" | base64 -d > "$f"
else
  f={_ROOT}$1
fi
exec 9>{_HOME}/ns.lock
flock 9
pid=$(cat {_HOME}/ns.pid 2>/dev/null || true)
if [ -z "$pid" ] || [ "$(cat /proc/$pid/comm 2>/dev/null)" != runtime-ns ]; then
  # Each attempt reports its pid in its own file, so a slow earlier holder
  # can't overwrite this one's; the starter moves it into place under the lock.
  t={_HOME}/ns.pid.$$
  rm -f {_HOME}/ns.pid "$t"
  setsid unshare -Urm --fork /bin/bash -c '
    R=$0; t=$1
    mount -t proc proc "$R/proc" 2>/dev/null || mount --rbind /proc "$R/proc"
    mount --rbind /dev "$R/dev"
    mount --rbind /sys "$R/sys" 2>/dev/null || true
    echo $$ > "$t.tmp" && mv "$t.tmp" "$t"
    exec {_HOLDER} infinity' {_ROOT} "$t" </dev/null >/dev/null 2>{_HOME}/ns.err 9>&- &
  pid=
  for _ in $(seq 600); do
    pid=$(cat "$t" 2>/dev/null || true)
    [ -n "$pid" ] && [ "$(cat /proc/$pid/comm 2>/dev/null)" = runtime-ns ] && break
    pid=; sleep 0.05
  done
  if [ -z "$pid" ]; then
    echo "could not start the sandbox's namespace holder:" >&2; cat {_HOME}/ns.err >&2
    rm -f -- "$f" "$t"; exit 1
  fi
  mv "$t" {_HOME}/ns.pid
fi
exec 9>&-
cd {_ROOT}
exec python3 {_NO_CHOWN} nsenter --target "$pid" --user --mount --preserve-credentials -- \\
  /usr/bin/env -i HOME=/root USER=root LOGNAME=root TERM=xterm \\
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\
  TAR_OPTIONS=--no-same-owner /usr/sbin/chroot {_ROOT} /bin/bash "${{f#{_ROOT}}}"
"""


# Uploaded to _NO_CHOWN and run by the enter script with the sandbox's python3.
_NO_CHOWN_SCRIPT = (Path(__file__).parent / "_no_chown.py").read_text()


class PrizedEnvironment(BaseEnvironment):
    """Runs a task's prebuilt image inside a Prized sandbox (see module doc)."""

    @classmethod
    @override
    def preflight(cls) -> None:
        if not os.environ.get("PRIZED_API_KEY"):
            raise SystemExit(
                "Prized requires PRIZED_API_KEY, a key with Sandboxes: Write "
                "(https://prized.dev/dashboard/workspace/api-keys?via=harbor). "
                + _SIGN_UP_HINT
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
        self._sandbox: AsyncSandbox | None = None
        self._image_env: dict[str, str] = {}
        self._image_workdir: str = "/"
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
        if (self.environment_dir / "docker-compose.yaml").exists():
            raise ValueError(
                "Prized runs one container per trial: Docker Compose tasks "
                "(environment/docker-compose.yaml) are not supported."
            )

    # -- the sandbox ------------------------------------------------------

    def _require_sandbox(self) -> AsyncSandbox:
        if self._sandbox is None:
            raise RuntimeError("Prized sandbox is not running; call start() first.")
        return self._sandbox

    async def _host_exec(
        self, command: str, timeout_sec: int | None = 600
    ) -> ExecResult:
        """Runs a command on the sandbox itself, outside the image.

        ``timeout_sec`` None or 0 is no limit. The SDK runs a command that may
        last more than a few minutes detached and polls it, so a long agent run
        outlives a dropped connection, and cancelling the call stops it.
        """
        sandbox = self._require_sandbox()
        try:
            result = await sandbox.exec(
                command, timeout=timeout_sec or 0, full_output=True
            )
        except CommandTimeoutError:
            raise RuntimeError(
                f"Command timed out after {timeout_sec} seconds"
            ) from None
        return ExecResult(
            stdout=result.stdout, stderr=result.stderr, return_code=result.exit_code
        )

    async def _put(self, host_path: str, data: bytes, mode: int | None = None) -> None:
        await self._require_sandbox().files.write(host_path, data, mode=mode)

    async def _get(self, host_path: str) -> bytes:
        return await self._require_sandbox().files.read_bytes(host_path)

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
        try:
            self._sandbox = await AsyncSandbox.create(
                self._template,
                vcpu=cpus,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                labels=self._labels,
                max_runtime_min=self._max_runtime_min,
                timeout=600,
            )
        except PrizedError as exc:
            if exc.code in (
                "missing_api_key",
                "invalid_api_key",
                "unauthorized",
                "token_expired",
                "key_access_denied",
            ):
                raise RuntimeError(f"{exc}. {_SIGN_UP_HINT}") from exc
            raise
        self.logger.debug(f"Prized sandbox {self._sandbox.id} for {image}")
        try:
            await self._prepare_image(image)
            workdir = self.task_env_config.workdir or self._image_workdir
            await self.exec(f"mkdir -p {shlex.quote(workdir)}", cwd="/")
            await self.ensure_dirs(self._mount_targets(writable_only=True))
            await self._upload_environment_dir_after_start()
        except BaseException:  # a cancelled start must not leave the sandbox behind
            # Shielded: a second cancel must not cut the delete short.
            await asyncio.shield(self.stop(delete=True))
            raise

    async def _prepare_image(self, image: str) -> None:
        q = shlex.quote
        setup = (
            "set -e; cd ~; "
            f"[ -x ./crane ] || {{ curl -fsSL -o crane.tgz {_CRANE_URL} && "
            f"echo '{_CRANE_SHA256}  crane.tgz' | sha256sum -c --quiet && "
            "tar -xzf crane.tgz crane && rm crane.tgz; }; "
            f"mkdir -p {_ROOT}; "
            f"./crane export {q(image)} - | tar --no-same-owner -x -C {_ROOT} 2>tar.err || "
            f"{{ test -e {_ROOT}/bin/sh && ! grep -q 'No space' tar.err; }} || "
            "{ tail -5 tar.err >&2; df -h ~ >&2; exit 1; }; "
            f"mkdir -p {_ROOT}{_XFER} {_ROOT}/proc {_ROOT}/dev {_ROOT}/sys {_ROOT}/tmp; "
            f"chmod 1777 {_ROOT}/tmp; "
            f"cp /bin/sleep {_HOLDER}; "
            f"rm -f {_ROOT}/etc/resolv.conf; cp /etc/resolv.conf {_ROOT}/etc/resolv.conf; "
            f"if [ -d {_ROOT}/etc/apt/apt.conf.d ]; then echo 'APT::Sandbox::User \"root\";' "
            f"> {_ROOT}/etc/apt/apt.conf.d/99runtime-sandbox-user; fi; "
            # dpkg installs files with mode 0 and then sets the mode, which the
            # seccomp filter skips for setuid/setgid files: open those up after.
            f"if [ -d {_ROOT}/etc/dpkg ]; then mkdir -p {_ROOT}/etc/dpkg/dpkg.cfg.d && "
            f"echo {q(_DPKG_HOOK)} > {_ROOT}/etc/dpkg/dpkg.cfg.d/99runtime-modes; fi; "
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
        await self._put(_NO_CHOWN, _NO_CHOWN_SCRIPT.encode())
        await self._put(_ENTER, _ENTER_SCRIPT.encode(), mode=0o755)

    @override
    async def stop(self, delete: bool) -> None:
        sandbox, self._sandbox = self._sandbox, None
        if sandbox is None:
            return
        try:
            if delete:
                await sandbox.delete()
        except PrizedError as exc:
            self.logger.error(f"Error deleting Prized sandbox {sandbox.id}: {exc}")
        finally:
            await sandbox.close()

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
        # The script holds the command's env: bash keeps it open, so it goes first.
        lines = ['rm -f -- "$0"']
        lines += [f"export {k}={shlex.quote(v)}" for k, v in merged.items()]
        lines.append(f"cd {shlex.quote(workdir)} || exit 1")
        lines.append(command)
        script = "\n".join(lines).encode()
        if len(script) <= _INLINE_LIMIT:
            args = f"- {base64.b64encode(script).decode()}"
        else:
            staged = f"{_XFER}/cmd-{uuid.uuid4().hex[:8]}.sh"
            await self._put(_ROOT + staged, script)
            args = staged
        return await self._host_exec(f"{_ENTER} {args}", timeout_sec=timeout_sec)

    async def _checked(self, command: str, what: str) -> None:
        result = await self.exec(command, cwd="/", timeout_sec=600)
        if result.return_code != 0:
            raise RuntimeError(f"{what}: {result.stderr or result.stdout}")

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        staged = f"{_XFER}/up-{uuid.uuid4().hex[:8]}"
        await self._put(_ROOT + staged, source.read_bytes())
        # Setuid/setgid bits can't be set in the sandbox: keep the rest.
        mode = f"{source.stat().st_mode & 0o1777:o}"
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
            f"cp -L {shlex.quote(source_path)} {staged} && chmod 600 {staged}",
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
