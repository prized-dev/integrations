"""Prized Sandboxes environment for Harbor: ``--env harbor_prized:PrizedEnvironment``.

A Prized sandbox is a Firecracker microVM started from a fixed template, not
from a Docker image, and its user has no root. To run a task's image anyway,
this environment unpacks the task's prebuilt ``docker_image`` into a root
filesystem inside the sandbox (with ``crane export``; crane is pinned and its
checksum checked) and runs every command chrooted into it, in unprivileged user
namespaces (``_runtime.py``). Commands see the image's files, ``ENV``,
``WORKDIR`` and ``USER``, run as root (uid 0 of the namespace) or as the
requested user, and root can ``apt-get install``.

Limits, by design of that approach:

- Tasks need ``[environment].docker_image``, pulled anonymously unless
  ``registry_auth`` is set (see below). Building from a Dockerfile (the
  task's, or a verifier image in ``tests/Dockerfile``) is not supported; build
  and push the image first. The image's ``ENTRYPOINT`` and ``CMD`` don't run.
- One id: every user is the same sandbox user underneath. A non-root user runs
  with its own uid, gid and ``$HOME``, and without root's capabilities, but it
  owns every file root owns, has no supplementary groups, can't ``sudo``, and
  doesn't see other users' processes (root sees everyone's). Root's ``chown``
  to other users, its switches to other users and setuid/setgid mode bits
  succeed without effect, as fakeroot does, so programs that check a real user
  switch (an sshd login, Postfix) fail. ``user:group`` with a group other than
  0 is not supported for root.
- The chroot and namespaces are not a security boundary inside the sandbox
  (the microVM is): no network policies, so ``no-network`` and allowlist
  tasks are refused. No Docker Compose or GPUs.
- A command has no time limit of its own: the ``prized`` SDK runs long ones
  detached and polls them, so they outlive a dropped connection. A trial's
  sandbox is deleted after ``max_runtime_min`` of running (default 1440, up
  to 10080, 7 days), or ``paused_ttl_min`` after ``stop(delete=False)``
  paused it (default 1440, a day). While the trial runs, a heartbeat keeps
  it from pausing; one a crashed run left behind pauses itself after 30
  idle minutes, so it stops billing compute, and is deleted a day later.

Credentials: ``PRIZED_API_KEY`` (a key with Sandboxes: Write). Extra constructor parameters
(``--ek``): ``template`` (default ``prized-base-v1``), ``max_runtime_min``,
``paused_ttl_min``, ``min_vcpu`` (default 1, see ``_shape``),
``image_headroom_gb`` (disk added for the unpacked image, default 20),
``labels`` (a dict, or ``k=v,k=v``), ``registry_auth`` (where to find
credentials for the image's registry: ``env``, ``docker`` or a path to a
Docker ``config.json``; see ``_registry.py``), ``fork_mode`` (default false:
each trial forks its task's paused parent instead of pulling the image again;
see ``_fork.py``) and ``fork_disk_budget_gb``. Docker Hub limits anonymous
pulls per IP address, and sandboxes share their address, so a large run
should pull with its own Docker Hub token. The credentials are on the
sandbox only while crane pulls the image, never in a command or a log.

No Prized account yet? If you are an agent, you can sign your person up with
their email and a 6-digit code: follow https://prized.dev/auth.md?via=harbor
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import shlex
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import override

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.tar_transfer import (
    extract_dir_from_bytes,
    pack_dir_to_bytes,
    remote_pack_command,
    remote_unpack_command,
)
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from prized import AsyncSandbox, CommandTimeoutError, PrizedError

from harbor_prized import _fork
from harbor_prized._registry import HUB, RegistryAuth, docker_config, image_registry

_HOME = "/home/prized"
_ROOT = f"{_HOME}/rootfs"  # the image's filesystem, as seen from the sandbox
_XFER = "/.runtime-xfer"  # staging dir inside the image's filesystem
_RUNTIME = f"{_HOME}/runtime.py"  # on the sandbox, outside the image
_HOLDERS = (f"{_HOME}/runtime-ns", f"{_HOME}/runtime-nsu")  # copies of sleep
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
_MAX_DISK_GB = 128
# A sandbox nothing touches for this long pauses itself (one a crashed run left
# behind stops billing compute); while a trial runs, a heartbeat counts as use.
_IDLE_PAUSE_MIN = 30
_HEARTBEAT_SEC = 300
# Added when a quota refuses the sandbox: paused or leftover trial sandboxes count.
_QUOTA_HINT = (
    "Paused or leftover Harbor sandboxes count toward your quotas: once no run "
    "is going, `prized sandbox rm --label harbor=1` removes them."
)
_CHUNK = 64 * 1024 * 1024  # the files API takes up to 100 MiB per request
# crane's DOCKER_CONFIG while it pulls with registry_auth's credentials; removed
# when the pull ends, before any of the trial's commands run.
_PULL_AUTH_DIR = f"{_HOME}/.pull-auth"
_WARNED_NO_CREDENTIALS: set[tuple[str, str]] = set()
_REFUSED = r"401 Unauthorized|UNAUTHORIZED|DENIED"
# Added when Docker Hub refuses an anonymous pull.
_PULL_LIMIT_HINT = (
    "Docker Hub limits anonymous pulls per IP address, and sandboxes share "
    "theirs: pull with your own Docker Hub token (`--ek registry_auth=env` with "
    "DOCKERHUB_USERNAME and DOCKERHUB_TOKEN set), or run the trial again later."
)

# Run by dpkg after each run (dpkg.cfg post-invoke): files a faked setuid chmod
# left at mode 0 become 0755.
_DPKG_HOOK = (
    "post-invoke=find /usr /bin /sbin /lib /lib64 /opt -xdev -type f -perm 0000 "
    "-exec chmod 0755 {} + 2>/dev/null || true"
)
# What Docker writes into a container's /etc/hosts, for the sandbox's hostname.
_HOSTS = (
    "127.0.0.1\tlocalhost\n"
    "::1\tlocalhost ip6-localhost ip6-loopback\n"
    "127.0.1.1\t$(hostname)\n"
)
# As Docker does: the sandbox's resolver, hosts and hostname in the image. A
# fork gets a hostname and address of its own, so it writes them again.
_IDENTITY = (
    f"rm -f {_ROOT}/etc/resolv.conf {_ROOT}/etc/hosts {_ROOT}/etc/hostname; "
    f"cp /etc/resolv.conf {_ROOT}/etc/resolv.conf; "
    f'printf "{_HOSTS}" > {_ROOT}/etc/hosts; hostname > {_ROOT}/etc/hostname; '
)
# tar errors that unpacking an image as an unprivileged user may report and
# that leave the image usable: device nodes, and setuid/setgid modes the
# sandbox refuses. Anything else fails the start.
_BENIGN_TAR_ERRORS = "Cannot mknod|Cannot change mode|Exiting with failure status"
_INLINE_LIMIT = 60_000  # longer scripts are uploaded instead of passed inline
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WINDOWS_HOST = os.name == "nt"  # its files have no POSIX modes to keep

logger = logging.getLogger(__name__)
# runtime.py exits with this, and says "prized runtime:", when it can't run a command.
_RUNTIME_FAILED = 125

# Uploaded to _RUNTIME and run with the sandbox's python3.
_RUNTIME_SCRIPT = (Path(__file__).parent / "_runtime.py").read_text()


def tar_pack_succeeded(
    return_code: int | None,
    *,
    stdout: str | None = None,
    stderr: str | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Success, or GNU tar's exit 1 with only "file changed as we read it"."""
    if return_code == 0:
        return True
    lines = [
        line.strip()
        for output in (stdout, stderr)
        for line in (output or "").splitlines()
        if line.strip()
    ]
    accepted = (
        return_code == 1
        and bool(lines)
        and all(line.endswith("file changed as we read it") for line in lines)
    )
    if accepted and logger is not None:
        logger.warning(
            "tar reported files changed while packing; the archive may be "
            f"incomplete: {'; '.join(lines)}"
        )
    return accepted


def unpack_command(image: str, root: str, crane: str = "./crane") -> str:
    """Bash that unpacks ``image`` into ``root``, keeping its file modes.

    It fails if crane fails (a pull cut short must not pass as an image), or if
    tar reports anything but the benign errors above.
    """
    q = shlex.quote
    return (
        f"set +e; {crane} export {q(image)} - | "
        f"tar --no-same-owner --preserve-permissions -x -C {q(root)} 2>tar.err; "
        's="${PIPESTATUS[0]} ${PIPESTATUS[1]}"; set -e; '
        'if [ "${s% *}" != 0 ]; then echo "crane export failed" >&2; '
        "tail -5 tar.err >&2; exit 1; fi; "
        f'if [ "${{s#* }}" != 0 ] && grep -v -E {q(_BENIGN_TAR_ERRORS)} tar.err | grep -q .; '
        "then tail -5 tar.err >&2; df -h ~ >&2; exit 1; fi; "
    )


def _registry_auth(value: object) -> RegistryAuth | None:
    """``registry_auth`` as given (Harbor turns ``--ek x=false`` into False)."""
    if value is None or value is False:
        return None
    usage = "registry_auth takes env, docker or the path of a Docker config.json"
    if isinstance(value, (bool, int, float)):  # --ek registry_auth=true, =1
        raise TypeError(
            f"{usage}, not true or a number (Harbor reads --ek values as JSON)."
        )
    if not isinstance(value, str):  # never echoed: it may hold credentials
        raise TypeError(
            f"{usage}, never the credentials themselves (Harbor writes --ek "
            "values into the job's config.json)."
        )
    source = value.strip()
    if source.lower() in ("", "none", "off", "false"):
        return None
    return RegistryAuth(source)


def _flag(name: str, value: object) -> bool:
    """A yes/no option (Harbor reads ``--ek`` values as JSON: true, 1, "yes")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("1", "true", "yes", "on"):
            return True
        if word in ("", "0", "false", "no", "off"):
            return False
    raise TypeError(f"{name} takes true or false, not {value!r}")


def pull_auth_prefix(directory: str) -> str:
    """Bash that points crane at ``directory``'s config.json and removes the
    directory when the script exits, whether the pull worked or not."""
    q = shlex.quote(directory)
    return f"export DOCKER_CONFIG={q}; trap {shlex.quote(f'rm -rf {q}')} EXIT; "


async def _despite_cancel(work: Awaitable[None]) -> None:
    """Awaits ``work`` to the end even if the caller is cancelled meanwhile,
    then re-raises the cancellation (a delete must not be cut short)."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            # Unlike awaiting the task, wait() neither cancels it nor raises its error.
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled() and task.exception() is not None:
            logger.warning(f"Cleanup failed while cancelled: {task.exception()!r}")
        raise asyncio.CancelledError
    task.result()


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
        max_runtime_min: int = 1440,
        paused_ttl_min: int = 1440,
        min_vcpu: int = 1,
        image_headroom_gb: int = 20,
        labels: dict[str, str] | str | None = None,
        registry_auth: str | None = None,
        fork_mode: bool | str = False,
        fork_disk_budget_gb: int = _fork.DEFAULT_BUDGET_GB,
        **kwargs,
    ):
        if isinstance(labels, str):  # --ek labels=team=evals,run=42
            labels = dict(p.split("=", 1) for p in labels.split(",") if "=" in p)
        self._template = template
        self._max_runtime_min = int(max_runtime_min)
        self._paused_ttl_min = int(paused_ttl_min)
        self._min_vcpu = int(min_vcpu)
        self._image_headroom_gb = int(image_headroom_gb)
        self._registry_auth = _registry_auth(registry_auth)
        self._fork_mode = _flag("fork_mode", fork_mode)
        self._fork_disk_budget_gb = int(fork_disk_budget_gb)
        self._fork_counted = False  # this trial has asked for its task's parent
        self._labels = {
            "harbor": "1",
            "harbor-session": session_id[:256],
            **(labels or {}),
        }
        self._sandbox: AsyncSandbox | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._warned_one_id = False
        self._image_env: dict[str, str] = {}
        self._image_workdir: str = "/"
        self._image_user: str | None = None
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

    @classmethod
    @override
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # Each trial's sandbox is created with at least the task's vCPUs and
        # memory; fractional cpus (0.5) round up to whole vCPUs (_shape).
        # Harbor 0.24, which has no fractional_cpus field, ignores it.
        return EnvironmentResourceCapabilities(
            cpu_request=True, memory_request=True, fractional_cpus=True
        )

    @override
    def _validate_definition(self) -> None:
        if not self.task_env_config.docker_image:
            raise ValueError(
                "Prized runs a task's prebuilt image: set [environment].docker_image "
                "in task.toml. Building from a Dockerfile is not supported."
            )
        if (self.environment_dir / "docker-compose.yaml").exists():
            raise ValueError(
                "Prized runs one container per trial: Docker Compose tasks "
                "(environment/docker-compose.yaml) are not supported."
            )
        storage_gb = self._storage_gb()
        if storage_gb > _MAX_DISK_GB:
            raise ValueError(
                f"The task asks for {storage_gb} GB of storage; a Prized sandbox "
                f"has at most {_MAX_DISK_GB} GB of disk."
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

    async def _host_checked(self, command: str, what: str) -> ExecResult:
        result = await self._host_exec(command)
        if result.return_code != 0:
            raise RuntimeError(f"{what}: {result.stderr or result.stdout}")
        return result

    async def _put(self, host_path: str, data: bytes, mode: int | None = None) -> None:
        """Writes a file on the sandbox, in parts if it is too big for one request."""
        files = self._require_sandbox().files
        if len(data) <= _CHUNK:
            await files.write(host_path, data, mode=mode)
            return
        starts = range(0, len(data), _CHUNK)
        parts = [f"{host_path}.part{i:04d}" for i in range(len(starts))]
        q = shlex.quote
        try:
            for part, start in zip(parts, starts):
                await files.write(part, data[start : start + _CHUNK])
            chmod = f" && chmod {mode:o} {q(host_path)}" if mode is not None else ""
            await self._host_checked(
                f"cat {' '.join(q(p) for p in parts)} > {q(host_path)}{chmod} "
                f"|| {{ rm -f {q(host_path)}; exit 1; }}",
                f"Failed to assemble {host_path}",
            )
        finally:
            await _despite_cancel(self._host_rm(" ".join(q(p) for p in parts)))

    async def _get(self, host_path: str) -> bytes:
        """Reads a file from the sandbox, in parts if it is too big for one request."""
        files = self._require_sandbox().files
        try:
            return await files.read_bytes(host_path)
        except PrizedError as exc:
            if exc.code != "file_too_large":
                raise
        q = shlex.quote(host_path)
        try:
            listing = await self._host_checked(
                f"split -b {_CHUNK} -d -a 4 {q} {q}.part && ls {q}.part*",
                f"Failed to split {host_path}",
            )
            parts = (listing.stdout or "").split()
            return b"".join([await files.read_bytes(part) for part in parts])
        finally:
            await _despite_cancel(self._host_rm(f"{q}.part*"))

    async def _host_rm(self, paths: str) -> None:
        """Best-effort ``rm -f`` on the sandbox (paths already quoted or globbed)."""
        try:
            await self._host_exec(f"rm -f {paths}", timeout_sec=60)
        except (PrizedError, RuntimeError) as exc:
            self.logger.debug(f"Could not remove {paths}: {exc}")

    # -- lifecycle --------------------------------------------------------

    def _storage_gb(self) -> int:
        return -(-int(self._effective_storage_mb or 10240) // 1024)

    def _shape(self) -> tuple[int, int, int]:
        """vCPUs, memory (MB) and disk (GB) for the trial's sandbox.

        vCPUs are the task's, rounded up (2 if it names none), or ``min_vcpu``
        if that is more. A container limited to one CPU still runs its threads
        in parallel across the host's cores, and a VM with few vCPUs can't:
        torch-tensor-parallelism deadlocks in gloo's teardown below 4 vCPUs.
        The image is unpacked onto the sandbox's disk, so the disk is the
        task's storage plus room for it, as far as the largest disk allows.
        """
        cpus = self._effective_cpus
        vcpu = max(self._min_vcpu, 1, math.ceil(cpus) if cpus else 2)
        memory_mb = max(1024, int(self._effective_memory_mb or 4096))
        disk_gb = min(
            _MAX_DISK_GB, max(10, self._storage_gb()) + self._image_headroom_gb
        )
        return vcpu, memory_mb, disk_gb

    @override
    async def start(self, force_build: bool) -> None:
        image = self.task_env_config.docker_image
        if image is None:
            raise ValueError("Prized needs [environment].docker_image.")
        if force_build and (self.environment_dir / "Dockerfile").exists():
            self.logger.warning(
                "Prized runs the prebuilt docker_image; force_build is ignored "
                "and environment/Dockerfile is not built."
            )
        try:
            await self._start(image)
        except PrizedError as exc:
            # A request that got no clear answer leaves the sandbox in doubt:
            # it was deleted, so start once more on a fresh one.
            if exc.code != "network_error":
                raise
            self.logger.warning(f"Prized sandbox start failed ({exc}); retrying once")
            await self._start(image)

    def _pull_credentials(self, image: str) -> tuple[str, bytes] | None:
        """registry_auth's entry for the image's registry, read before a sandbox starts."""
        if self._registry_auth is None:
            return None
        registry = image_registry(image)
        entry = self._registry_auth.for_registry(registry)
        if entry is None:
            key = (self._registry_auth.source, registry)
            if key not in _WARNED_NO_CREDENTIALS:  # once a run, not once a trial
                _WARNED_NO_CREDENTIALS.add(key)
                self.logger.warning(
                    f"registry_auth={self._registry_auth.source} has no "
                    f"credentials for {registry}; its images are pulled anonymously."
                )
            return None
        self.logger.debug(
            f"Pulling {image} with the credentials for {registry} from "
            f"{self._registry_auth.describe()}"
        )
        return registry, docker_config(image, entry)

    async def _start(self, image: str) -> None:
        if self._sandbox is not None:  # a second start replaces the first
            await self.stop(delete=True)
        credentials = await asyncio.to_thread(self._pull_credentials, image)
        if self._fork_mode and await self._start_forked(image, credentials):
            return
        await self._start_fresh(image, credentials, self._labels)

    async def _create(self, labels: dict[str, str]) -> AsyncSandbox:
        vcpu, memory_mb, disk_gb = self._shape()
        try:
            return await AsyncSandbox.create(
                self._template,
                vcpu=vcpu,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                labels=labels,
                max_runtime_min=self._max_runtime_min,
                auto_pause_min=_IDLE_PAUSE_MIN,
                auto_delete_min=self._paused_ttl_min,
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
                # The SDK's message often carries a sign-up pointer already.
                message = str(exc).rstrip(".")
                if "auth.md" not in message:
                    message = f"{message}. {_SIGN_UP_HINT}"
                raise RuntimeError(message) from exc
            if exc.code == "sandbox_quota_exceeded":
                raise RuntimeError(f"{str(exc).rstrip('.')}. {_QUOTA_HINT}") from exc
            raise

    async def _start_fresh(
        self,
        image: str,
        credentials: tuple[str, bytes] | None,
        labels: dict[str, str],
    ) -> None:
        """Creates a sandbox and unpacks the image into it, as ``self._sandbox``."""
        self._sandbox = await self._create(labels)
        self._heartbeat = asyncio.create_task(self._keep_alive(self._sandbox))
        self.logger.debug(f"Prized sandbox {self._sandbox.id} for {image}")
        try:
            await self._prepare_image(image, credentials)
            workdir = self.task_env_config.workdir or self._image_workdir
            await self._checked(
                f"mkdir -p {shlex.quote(workdir)}", f"Failed to create {workdir}"
            )
            await self.ensure_dirs(self._mount_targets(writable_only=True))
            await self._upload_environment_dir_after_start()
        except BaseException:  # a cancelled start must not leave the sandbox behind
            if credentials is not None:
                # The setup's trap can't run if the setup was killed (its
                # timeout), and a delete that fails leaves the sandbox up.
                await _despite_cancel(self._remove_pull_auth())
            await _despite_cancel(self.stop(delete=True))
            raise

    # -- fork mode (see _fork.py) -------------------------------------------

    def _fork_key(self, image: str, task: str | None) -> str:
        """Everything a start depends on: trials with the same key can share a parent."""
        environment = getattr(self, "environment_id", None)
        if not isinstance(environment, str):  # Harbor 0.24 has no environment_id
            environment = str(self.environment_dir.resolve())
        parts = {
            "template": self._template,
            "shape": list(self._shape()),
            # Copies inherit the timers. environment_id leaves out the image
            # when environment/ has files.
            "timers": [self._max_runtime_min, self._paused_ttl_min],
            "image": image,
            "environment": environment,
            "task": task or self.environment_name,
            "workdir": self.task_env_config.workdir,
            "mounts": sorted(self._mount_targets(writable_only=True)),
            "upload": getattr(self, "_enable_environment_dir_upload", True),
        }
        blob = json.dumps(parts, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    async def _start_forked(
        self, image: str, credentials: tuple[str, bytes] | None
    ) -> bool:
        """Starts the trial in a fork of its task's parent, building the
        parent first if this trial is the first to need it. False: start fresh."""
        trial_dir = self.trial_paths.trial_dir
        task = _fork.task_of(trial_dir)
        cache = _fork.cache(self._fork_disk_budget_gb)
        lease, evicted = cache.acquire(
            self._fork_key(image, task),
            task,
            cache.plan(trial_dir.parent),
            self._shape()[2],
            count=not self._fork_counted,
        )
        self._fork_counted = True
        try:
            if evicted:
                await _despite_cancel(_fork.delete_parents(evicted))
            if lease is None:
                return False
            parent = lease.parent
            if lease.builder:
                if not await self._build_parent(cache, parent, image, credentials):
                    return False
            elif not await _fork.wait_ready(parent):
                return False
            return await self._fork_from(cache, parent)
        finally:
            if lease is not None:
                doomed = cache.release(lease)
                if doomed is not None:
                    await _despite_cancel(_fork.delete_parent(doomed))

    async def _build_parent(
        self,
        cache: _fork.ParentCache,
        parent: _fork.Parent,
        image: str,
        credentials: tuple[str, bytes] | None,
    ) -> bool:
        """Builds the task's parent: a fresh start under the parent's labels,
        then paused, with no credentials left on it. False: start fresh. A
        failure fresh mode would meet too (a failed pull) is raised."""
        labels = {
            **self._labels,
            "harbor-session": f"fork-parent-{parent.key}",
            "harbor-task": self.environment_name[:256],
            _fork.PARENT_LABEL: "1",
            _fork.RUN_LABEL: _fork.RUN_ID,
        }
        _fork.note_build()
        try:
            await self._start_fresh(image, credentials, labels)
        except RuntimeError as exc:
            cause = exc.__cause__
            if not (
                isinstance(cause, PrizedError)
                and cause.code == "sandbox_quota_exceeded"
            ):
                raise
            self.logger.warning(
                f"Prized fork mode: no room for a parent of {self.environment_name} "
                f"({cause.reason or cause.code}); starting fresh"
            )
            victim = cache.build_failed(parent, quota=True)
            await _despite_cancel(_fork.delete_parent(victim, wait=True))
            return False
        sandbox, heartbeat = self._sandbox, self._heartbeat
        self._sandbox = self._heartbeat = None
        if heartbeat is not None:
            heartbeat.cancel()
        assert sandbox is not None
        try:
            if credentials is not None:
                # The setup's trap removed them; every copy would carry them, so check.
                gone = shlex.quote(_PULL_AUTH_DIR)
                result = await sandbox.exec(
                    f"rm -rf {gone} && ! test -e {gone}", timeout=60, full_output=True
                )
                if result.exit_code != 0:
                    raise RuntimeError(f"{_PULL_AUTH_DIR} is still there")
            await sandbox.pause()
        except BaseException as exc:
            await _despite_cancel(self._stop_sandbox(sandbox, delete=True))
            if not isinstance(exc, (PrizedError, RuntimeError)):
                raise
            self.logger.warning(
                f"Prized fork mode: could not make {sandbox.id} a parent ({exc}); "
                "starting fresh"
            )
            cache.build_failed(parent)
            return False
        facts = _fork.ImageFacts(
            dict(self._image_env), self._image_workdir, self._image_user
        )
        if not cache.mark_ready(parent, sandbox, facts):
            await _despite_cancel(_fork.delete_parent(parent))
            return False
        self.logger.debug(
            f"Prized sandbox {sandbox.id} is the paused parent of {self.environment_name}"
        )
        return True

    async def _fork_from(self, cache: _fork.ParentCache, parent: _fork.Parent) -> bool:
        """Forks this trial's copy of the parent. False: start fresh."""
        source, facts = parent.sandbox, parent.facts
        assert source is not None and facts is not None
        copy = await self._fork_copy(cache, parent, source)
        if copy is None:
            return False
        self._sandbox = copy
        self._image_env = dict(facts.env)
        self._image_workdir, self._image_user = facts.workdir, facts.user
        self._heartbeat = asyncio.create_task(self._keep_alive(copy))
        try:
            await self._host_checked(
                "set -e; " + _IDENTITY, "Could not write the copy's hostname"
            )
        except BaseException as exc:
            await _despite_cancel(self.stop(delete=True))
            if not isinstance(exc, (PrizedError, RuntimeError)):
                raise
            self.logger.warning(
                f"Prized fork mode: copy {copy.id} of {source.id} failed ({exc}); "
                "starting fresh"
            )
            return False
        self.logger.debug(f"Prized sandbox {copy.id} forked from {source.id}")
        return True

    async def _fork_copy(
        self, cache: _fork.ParentCache, parent: _fork.Parent, source: AsyncSandbox
    ) -> AsyncSandbox | None:
        """One running copy of ``source`` with this trial's labels, or None
        (see ``_fork.fork_refusal`` for what each refusal does)."""
        labels = {**self._labels, _fork.COPY_LABEL: source.id}
        for attempt in (1, 2):
            try:
                copies = await source.fork(1, labels=labels)
            except PrizedError as exc:
                action, delay = _fork.fork_refusal(exc)
                if attempt == 1 and action in ("retry", "evict"):
                    self.logger.debug(f"Fork of {source.id} refused ({exc}); again")
                    if action == "evict":
                        victim = cache.evict_one(exclude=parent)
                        if victim is None:
                            self.logger.warning(
                                f"Prized fork mode: no room to fork {source.id} "
                                f"({exc}); starting fresh"
                            )
                            return None
                        await _despite_cancel(_fork.delete_parent(victim, wait=True))
                    elif delay:
                        await asyncio.sleep(delay)
                    continue
                if action in ("drop", "never", "gone"):
                    cache.drop(parent, failed=action != "gone", never=action == "never")
                self.logger.warning(
                    f"Prized fork mode: could not fork {source.id} ({exc}); "
                    "starting fresh"
                )
                return None
            return copies[0]
        return None

    async def _remove_pull_auth(self) -> None:
        """Best-effort removal of registry_auth's credentials from the sandbox."""
        try:
            await self._host_exec(
                f"rm -rf {shlex.quote(_PULL_AUTH_DIR)}", timeout_sec=60
            )
        except (PrizedError, RuntimeError) as exc:
            self.logger.debug(f"Could not remove {_PULL_AUTH_DIR}: {exc}")

    async def _prepare_image(
        self, image: str, credentials: tuple[str, bytes] | None = None
    ) -> None:
        q = shlex.quote
        auth = ""
        if credentials is not None:
            # Through the files API, so the secret is in no command line. The
            # trap removes it when the setup's shell exits, pass or fail; a
            # setup killed at its timeout can't run the trap, so a failed start
            # also removes it (_start) before deleting the sandbox.
            await self._require_sandbox().files.write(
                f"{_PULL_AUTH_DIR}/config.json", credentials[1], mode=0o600
            )
            auth = pull_auth_prefix(_PULL_AUTH_DIR)
        setup = (
            auth  # first, so nothing can fail before the trap is set
            + "set -e; cd ~; umask 022; "
            + f"[ -x ./crane ] || {{ curl -fsSL --retry 3 -o crane.tgz {_CRANE_URL} && "
            f"echo '{_CRANE_SHA256}  crane.tgz' | sha256sum -c --quiet && "
            "tar -xzf crane.tgz crane && rm crane.tgz; }; "
            f"mkdir -p {_ROOT}; "
            + unpack_command(image, _ROOT)
            + f"mkdir -p {_ROOT}{_XFER} {_ROOT}/proc {_ROOT}/dev {_ROOT}/sys {_ROOT}/tmp {_ROOT}/etc; "
            f"chmod 1777 {_ROOT}/tmp; chmod 700 {_ROOT}{_XFER}; "
            + "".join(f"cp /bin/sleep {holder}; " for holder in _HOLDERS)
            + _IDENTITY
            + f"if [ -d {_ROOT}/etc/apt/apt.conf.d ]; then echo 'APT::Sandbox::User \"root\";' "
            f"> {_ROOT}/etc/apt/apt.conf.d/99runtime-sandbox-user; fi; "
            # dpkg installs files with mode 0 and then sets the mode, which the
            # seccomp filter skips for setuid/setgid files: open those up after.
            f"if [ -d {_ROOT}/etc/dpkg ]; then mkdir -p {_ROOT}/etc/dpkg/dpkg.cfg.d && "
            f"echo {q(_DPKG_HOOK)} > {_ROOT}/etc/dpkg/dpkg.cfg.d/99runtime-modes; fi; "
            f"./crane config {q(image)}"
        )
        result = await self._host_exec(setup, timeout_sec=1800)
        if result.return_code != 0:
            output = result.stderr or result.stdout or ""
            hint = self._pull_hint(image, output, credentials is not None)
            raise RuntimeError(
                f"Could not unpack {image} in the sandbox: {output}{hint}"
            )
        config = json.loads(result.stdout or "{}").get("config") or {}
        for pair in config.get("Env") or []:
            name, _, value = pair.partition("=")
            self._image_env[name] = value
        self._image_workdir = config.get("WorkingDir") or "/"
        self._image_user = config.get("User") or None
        await self._put(_RUNTIME, _RUNTIME_SCRIPT.encode())

    def _pull_hint(self, image: str, output: str, with_credentials: bool) -> str:
        """What to do about a failed pull, when the output says why."""
        auth = self._registry_auth
        if "TOOMANYREQUESTS" in output and image_registry(image) == HUB:
            if not with_credentials:
                return f" {_PULL_LIMIT_HINT}"
            return (
                " Docker Hub's pull limit for the account in registry_auth was "
                "reached: a free Personal account has a limit of its own, and paid "
                "plans don't. Run the trials again later."
            )
        if not with_credentials or auth is None:
            return ""
        where = f"registry_auth={auth.source} ({auth.describe()})"
        if "incorrect username or password" in output:
            return (
                f" {image_registry(image)} refused the credentials from {where}: "
                "check the token."
            )
        if re.search(_REFUSED, output):
            return (
                f" {image_registry(image)} refused the pull with the credentials from "
                f"{where}: check that they can read {image}, and that it exists."
            )
        return ""

    async def _keep_alive(self, sandbox: AsyncSandbox) -> None:
        """Counts as use every few minutes, so a trial between commands never
        reaches ``_IDLE_PAUSE_MIN`` (commands and file requests count too)."""
        while True:
            await asyncio.sleep(_HEARTBEAT_SEC)
            try:
                await sandbox.keep_alive()
            except PrizedError as exc:
                if exc.status in (404, 410) or exc.code in (
                    "not_found",
                    "sandbox_deleted",
                    "handle_closed",
                ):
                    return  # deleted under us: nothing left to keep alive
                self.logger.warning(f"Prized sandbox {sandbox.id} keep-alive: {exc}")
            except Exception as exc:  # noqa: BLE001 (the next beat tries again)
                self.logger.warning(f"Prized sandbox {sandbox.id} keep-alive: {exc}")

    @override
    async def stop(self, delete: bool) -> None:
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None:
            heartbeat.cancel()
        sandbox = self._sandbox
        if sandbox is None:
            return
        self._sandbox = None
        await _despite_cancel(self._stop_sandbox(sandbox, delete))

    async def _stop_sandbox(self, sandbox: AsyncSandbox, delete: bool) -> None:
        try:
            if delete:
                await sandbox.delete()
            else:
                # Kept for inspection: paused, it costs only its storage.
                await sandbox.pause()
                self.logger.info(
                    f"Prized sandbox {sandbox.id} is paused; it is deleted after "
                    f"{self._paused_ttl_min} minutes paused, or with "
                    f"`prized sandbox rm {sandbox.id}`."
                )
        except PrizedError as exc:
            if delete:
                self.logger.error(f"Error deleting Prized sandbox {sandbox.id}: {exc}")
            else:
                # A sandbox that won't pause would run until max_runtime_min.
                self.logger.error(
                    f"Error pausing Prized sandbox {sandbox.id}, deleting it: {exc}"
                )
                try:
                    await sandbox.delete()
                except PrizedError as again:
                    self.logger.error(
                        f"Error deleting Prized sandbox {sandbox.id}: {again}"
                    )
        finally:
            await sandbox.close()

    # -- users ------------------------------------------------------------

    async def _image_entries(self, path: str) -> list[list[str]]:
        """Entries of an /etc/passwd-style file in the image whose ids are numbers."""
        try:
            text = (await self._get(_ROOT + path)).decode(errors="replace")
        except (PrizedError, RuntimeError, ValueError):
            return []
        entries = [line.split(":") for line in text.splitlines()]
        return [e for e in entries if len(e) >= 4 and e[2].isdigit()]

    async def _user_identity(self, user: str | int) -> tuple[int, int, str, str] | None:
        """uid, gid, home and name for ``user`` (as ``docker exec -u`` takes it:
        a name or uid, optionally ``:group``); None for root."""
        name, _, group = str(user).partition(":")
        if name in ("0", "root"):
            return None  # only uid 0 and gid 0 are mapped for root
        passwd = await self._image_entries("/etc/passwd")
        if name.isdigit():
            entry = next((e for e in passwd if e[2] == name), None)
        else:
            entry = next((e for e in passwd if e[0] == name), None)
            if entry is None:
                raise RuntimeError(
                    f"unable to find user {name}: no matching entries in passwd file"
                )
        uid = int(name) if entry is None else int(entry[2])
        gid = int(entry[3]) if entry is not None and entry[3].isdigit() else 0
        if group:
            if group.isdigit():
                gid = int(group)
            else:
                groups = await self._image_entries("/etc/group")
                match = next((g for g in groups if g[0] == group), None)
                if match is None:
                    raise RuntimeError(
                        f"unable to find group {group}: no matching entries in group file"
                    )
                gid = int(match[2])
        if uid == 0:
            return None
        home = entry[5] if entry is not None and len(entry) > 5 and entry[5] else "/"
        return uid, gid, home, entry[0] if entry is not None else name

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
        # As docker exec: the requested user, else the image's USER, else root.
        user = self._resolve_user(user)
        if user is None:
            user = self._image_user
        identity = None if user is None else await self._user_identity(user)
        uid, gid, home, name = identity or (0, 0, "/root", "root")
        if uid != 0 and not self._warned_one_id:
            self._warned_one_id = True
            self.logger.warning(
                f"Prized runs {name} as uid {uid}, but every user is one id "
                "underneath: it can read and write root's files, and su or sudo "
                "-u inside a command stay root. User separation is not enforced."
            )
        merged = {
            "HOME": home,
            "USER": name,
            "LOGNAME": name,
            **self._image_env,
            **self._startup_env(),
            **(self._merge_env(env) or {}),
        }
        skipped = [k for k in merged if not _ENV_NAME.fullmatch(k)]
        if skipped:
            self.logger.debug(f"Prized can't export env names {skipped}; skipped")
        workdir = cwd or self.task_env_config.workdir or self._image_workdir
        # The script holds the command's env: bash keeps it open, so it goes first.
        lines = ['rm -f -- "$0"', "umask 022"]  # Docker's umask, not the sandbox's
        lines += [
            f"export {k}={shlex.quote(v)}"
            for k, v in merged.items()
            if _ENV_NAME.fullmatch(k)
        ]
        lines.append(f"cd -- {shlex.quote(workdir)} >/dev/null || exit 1")
        lines.append(command)
        script = "\n".join(lines).encode()
        user_arg = f"--user {uid}:{gid} " if identity else ""
        if len(script) <= _INLINE_LIMIT:
            return await self._run(
                f"python3 {_RUNTIME} {user_arg}- {base64.b64encode(script).decode()}",
                timeout_sec,
            )
        staged = f"{_XFER}/cmd-{uuid.uuid4().hex[:8]}.sh"
        try:
            await self._put(_ROOT + staged, script, mode=0o644)
            return await self._run(
                f"python3 {_RUNTIME} {user_arg}{staged}", timeout_sec
            )
        except BaseException:
            # The script removes itself when it starts; one that never started
            # still holds the command's env.
            await _despite_cancel(self._discard(staged))
            raise

    async def _run(self, command: str, timeout_sec: int | None) -> ExecResult:
        """Runs a runtime.py command; its own failures raise, the command's don't."""
        result = await self._host_exec(command, timeout_sec=timeout_sec)
        if result.return_code == _RUNTIME_FAILED and "prized runtime:" in (
            result.stderr or ""
        ):
            raise RuntimeError(
                f"Prized could not run the command: {(result.stderr or '').strip()}"
            )
        return result

    async def _discard(self, staged: str) -> None:
        await self._host_rm(shlex.quote(_ROOT + staged))

    async def _checked(self, command: str, what: str) -> None:
        result = await self.exec(command, cwd="/", timeout_sec=600, user=0)
        if result.return_code != 0:
            raise RuntimeError(f"{what}: {result.stderr or result.stdout}")

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        staged = f"{_XFER}/up-{uuid.uuid4().hex[:8]}"
        target = shlex.quote(target_path)
        # Setuid/setgid bits can't be set in the sandbox: keep the rest. A
        # Windows host has no mode to keep: the file keeps 0644.
        chmod = ""
        if not _WINDOWS_HOST:
            chmod = f" && chmod {source.stat().st_mode & 0o1777:o} {target}"
        try:
            await self._put(_ROOT + staged, source.read_bytes())
            await self._checked(
                f"if [ -d {target} ]; then echo {target} is a directory >&2; exit 1; fi"
                f' && mkdir -p "$(dirname {target})" && mv {staged} {target}{chmod}',
                f"Failed to upload {source} to {target_path}",
            )
        except BaseException:
            await _despite_cancel(self._discard(staged))
            raise

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        staged = f"{_XFER}/up-{uuid.uuid4().hex[:8]}.tar.gz"
        buffer = await asyncio.to_thread(pack_dir_to_bytes, source_dir, compress=True)
        try:
            await self._put(_ROOT + staged, buffer.getvalue())
            await self._checked(
                # Only uid 0 exists in the namespace, so never restore owners.
                f"{remote_unpack_command(staged, target_dir)} --no-same-owner",
                f"Failed to extract upload archive into {target_dir!r}",
            )
        finally:
            await _despite_cancel(self._discard(staged))

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        staged = f"{_XFER}/down-{uuid.uuid4().hex[:8]}"
        try:
            await self._checked(
                f"cp -L {shlex.quote(source_path)} {staged} && chmod 600 {staged}",
                f"Failed to read {source_path}",
            )
            data = await self._get(_ROOT + staged)
        finally:
            await _despite_cancel(self._discard(staged))
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        staged = f"{_XFER}/down-{uuid.uuid4().hex[:8]}.tar.gz"
        try:
            result = await self.exec(
                remote_pack_command(source_dir, staged),
                cwd="/",
                timeout_sec=600,
                user=0,
            )
            # Logs still being written make tar warn "file changed as we read it".
            if not tar_pack_succeeded(
                result.return_code,
                stdout=result.stdout,
                stderr=result.stderr,
                logger=self.logger,
            ):
                raise RuntimeError(
                    f"Failed to archive {source_dir!r}: {result.stderr or result.stdout}"
                )
            data = await self._get(_ROOT + staged)
        finally:
            await _despite_cancel(self._discard(staged))
        await asyncio.to_thread(extract_dir_from_bytes, data, target_dir)


__all__ = ["PrizedEnvironment"]
