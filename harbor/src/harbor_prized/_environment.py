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
  ``registry_auth`` is set (see below). A task built from a Dockerfile (the
  task's, or a verifier image in ``tests/Dockerfile``) or using Docker Compose
  runs in Docker mode instead (below). The image's ``ENTRYPOINT`` and ``CMD``
  don't run.
- One id: every user is the same sandbox user underneath. A non-root user runs
  with its own uid, gid and ``$HOME``, and without root's capabilities, but it
  owns every file root owns, has no supplementary groups, can't ``sudo``, and
  doesn't see other users' processes (root sees everyone's). Root's ``chown``
  to other users, its switches to other users and setuid/setgid mode bits
  succeed without effect, as fakeroot does, so programs that check a real user
  switch (an sshd login, Postfix) fail. ``user:group`` with a group other than
  0 is not supported for root.
- The chroot and namespaces are not a security boundary inside the sandbox
  (the microVM is). Network policies are enforced on the microVM, by Prized:
  ``no-network`` and an ``allowlist`` with no hosts turn the sandbox's network
  off, ``public`` turns it on, and Harbor may switch between them while the
  trial runs (``dynamic_network_policy``: setup with the network, the agent
  or the verifier without). The sandbox starts with the network, to pull the
  image, and is switched before the task's first command. Where Prized cannot
  switch a sandbox's network yet, its API says so (409
  ``network_none_unavailable``) and such tasks are refused, as before. An
  allowlist that names hosts is refused, in both modes. No GPUs.
- A command has no time limit of its own: the ``prized`` SDK runs long ones
  detached and polls them, so they outlive a dropped connection. A trial's
  sandbox is deleted after ``max_runtime_min`` of running (default 1440, up
  to 10080, 7 days), or ``paused_ttl_min`` after ``stop(delete=False)``
  paused it (default 1440, a day). While the trial runs, a heartbeat keeps
  it from pausing; one a crashed run left behind pauses itself after 30
  idle minutes, so it stops billing compute, and is deleted a day later.

Docker mode (``_docker.py``) runs what the approach above can't: a task
whose environment is built from a Dockerfile (no ``docker_image``, or a
verifier image in ``tests/Dockerfile``) or uses Docker Compose. Its sandbox
starts from ``prized-docker-v1`` and the task runs under ``docker compose``
inside it, as Harbor's own Docker provider runs it: built there, with its
sidecars and real users. Network policies are enforced by Docker inside the
sandbox there (the project's networks are internal, and one egress network is
attached or detached), so they don't need Prized's network switch. It is
chosen by itself for such tasks (``docker=auto``); where Prized refuses the
template (not active yet, or a workspace on free credit alone), such tasks
are refused as before, with the reason, and later trials of the run are
refused before a sandbox is made for ten minutes.

Credentials: ``PRIZED_API_KEY`` (a key with Sandboxes: Write). Extra constructor parameters
(``--ek``): ``docker`` (``auto``, the default: Docker mode for Dockerfile and
Compose tasks, and for tasks with a no-network phase; ``true`` for every task;
``false`` never), ``template`` (default ``prized-base-v1``; a
``prized-docker-*`` one means ``docker=true``), ``docker_template`` (default
``prized-docker-v1``), ``dockerd_wait_sec`` (how long a Docker-mode start
waits for the daemon, default 120), ``max_runtime_min``, ``paused_ttl_min``,
``min_vcpu`` (default 1, see ``_shape``), ``image_headroom_gb`` (disk added
for the image, default 20), ``labels`` (a dict, or ``k=v,k=v``),
``registry_auth`` (where to find credentials for the image's registry:
``env``, ``docker`` or a path to a Docker ``config.json``; see
``_registry.py``), ``fork_mode`` (default false: each trial forks a paused
parent instead of pulling or building again; see ``_fork.py``) and
``fork_disk_budget_gb``. Docker Hub limits anonymous pulls per IP address, and
sandboxes share their address, so a large run should pull with its own Docker
Hub token. The credentials are on the sandbox only while crane pulls the image
(or, in Docker mode, while Compose builds and pulls), never in a command or a
log.

No Prized account yet? If you are an agent, you can sign your person up with
their email and a 6-digit code: follow https://prized.dev/auth.md?via=harbor
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import math
import os
import re
import shlex
import time
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, override

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.compose_service_ops import (
    ComposeServiceOpsMixin,
    ComposeServiceTransport,
)
from harbor.environments.definition import require_agent_environment_definition
from harbor.environments.tar_transfer import (
    extract_dir_from_bytes,
    pack_dir_to_bytes,
    remote_pack_command,
    remote_unpack_command,
)
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from prized import AsyncSandbox, CommandTimeoutError, PrizedError

from harbor_prized import _fork
from harbor_prized._docker import (
    DOCKER_TEMPLATE,
    PULL_AUTH_DIR,
    PrizedDocker,
    image_references,
)
from harbor_prized._registry import (
    HUB,
    RegistryAuth,
    docker_cli_config,
    docker_config,
    image_registry,
)

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
# dockerd, containerd and compose run beside the task's containers.
_DOCKER_MEMORY_HEADROOM_MB = 512
# What a task that needs Docker mode was told before it existed, and still is
# where Prized refuses the Docker template.
_DOCKERFILE_REFUSED = (
    "Prized runs a task's prebuilt image: set [environment].docker_image "
    "in task.toml. Building from a Dockerfile is not supported."
)
_COMPOSE_REFUSED = (
    "Prized runs one container per trial: Docker Compose tasks "
    "(environment/docker-compose.yaml) are not supported."
)
# A create from the Docker template answered with one of these: this
# workspace can't use it (not listed, not active yet, a runtime this Prized
# doesn't run, or a paid-only template on free credit alone).
_TEMPLATE_REFUSAL_CODES = (
    "template_not_found",
    "template_inactive",
    "template_runtime_incompatible",
    "template_needs_payment",
)
# After a refusal, later Docker-mode trials of this process are refused
# before a sandbox is made, for this long; then the next one asks again.
_DOCKER_REFUSAL_TTL_SEC = 600.0
_docker_refusal: tuple[float, str, str] | None = None  # (when, template, why)
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
_PULL_AUTH_DIR = PULL_AUTH_DIR  # Docker mode's build and pulls use it too
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

# A sandbox's network, changed while it runs: PUT /api/v1/sandboxes/{id}/network.
# The API's 409 network_none_unavailable (or, while the feature is off, 404 on
# the route) means it can't be done here: such tasks are refused.
_NETWORK_UNAVAILABLE_CODES = ("network_none_unavailable",)
_NETWORK_REFUSED = (
    "Prized can't turn a sandbox's network off here yet, so this task, whose "
    "no-network or allowlist phases need it, is refused (Prized enforces a "
    "task's network policy or refuses the task). Run tasks whose network is "
    "public, or use Harbor's -e docker on a Prized box"
)
# After a refusal, later trials of this process are refused before a sandbox
# is made, for this long; then the next one asks again.
_NETWORK_REFUSAL_TTL_SEC = 600.0
# How long one switch may take, end to end, before the trial fails.
_NETWORK_SWITCH_SEC = 120.0
# Between asks after a refusal the API says is passing (another change, or
# one that failed and kept the old network).
_NETWORK_RETRY_SEC = 1.0
_network_refused_at: float | None = None


def _network_refused_recently() -> bool:
    return (
        _network_refused_at is not None
        and time.monotonic() - _network_refused_at < _NETWORK_REFUSAL_TTL_SEC
    )


def _note_network_refused() -> None:
    global _network_refused_at
    _network_refused_at = time.monotonic()


def _docker_refused_recently(template: str) -> str | None:
    """Why Prized refused ``template``, if it did in the last ten minutes."""
    if _docker_refusal is None:
        return None
    when, refused, why = _docker_refusal
    if refused != template or time.monotonic() - when >= _DOCKER_REFUSAL_TTL_SEC:
        return None
    return why


def _note_docker_refused(template: str, why: str) -> None:
    global _docker_refusal
    _docker_refusal = (time.monotonic(), template, why)


def _docker_choice(value: bool | str | None) -> bool | None:
    """``--ek docker``: True, False, or None for auto (the default)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):  # Harbor may coerce --ek docker=1
        return bool(value)
    text = str(value).strip().lower()
    if text in ("", "auto"):
        return None
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"docker: expected auto, true or false, got {value!r}")


def _closes_network(policy: object) -> bool:
    """A policy Prized enforces by closing the network: no-network, or an
    allowlist with no hosts."""
    if not isinstance(policy, NetworkPolicy):
        return False
    return policy.network_mode == NetworkMode.NO_NETWORK or (
        policy.network_mode == NetworkMode.ALLOWLIST and not policy.allowed_hosts
    )


def api_network(policy: NetworkPolicy) -> str:
    """The Prized network for a Harbor network policy: "default" for public,
    "none" for no-network and for an allowlist with no hosts (nothing is
    reachable either way). An allowlist with hosts can't be enforced."""
    if policy.network_mode == NetworkMode.PUBLIC:
        return "default"
    if policy.network_mode == NetworkMode.NO_NETWORK or not policy.allowed_hosts:
        return "none"
    raise ValueError(
        "Prized turns a sandbox's network on or off, but can't limit it to "
        f"hosts: allowlist {policy.allowed_hosts} is not supported"
    )


async def put_network(sandbox: AsyncSandbox, network: str) -> dict[str, Any]:
    """PUT /api/v1/sandboxes/{id}/network?wait=applied: the API answers once
    the network is in force (200, ``networkChange.status`` "applied"), or
    after its 8 s hold with the change still "pending" (202). Through the
    SDK's transport (its key, retries and errors), which has no method for it
    yet."""
    transport = sandbox._t
    answer = await transport.request(
        "PUT",
        transport.api(f"/sandboxes/{sandbox.id}/network"),
        json={"network": network},
        params={"wait": "applied"},
        sandbox_id=sandbox.id,
        deadline=transport.clock() + 60,
    )
    return answer.document or {}


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


class PrizedEnvironment(ComposeServiceOpsMixin, BaseEnvironment):
    """Runs a task's image inside a Prized sandbox (see module doc)."""

    @classmethod
    @override
    def preflight(cls, kwargs: dict[str, Any] | None = None) -> None:
        # Harbor after 0.25 passes the --ek values and validates them in the
        # base class; 0.24 and 0.25 call preflight() with none.
        if "kwargs" in inspect.signature(BaseEnvironment.preflight).parameters:
            super().preflight(kwargs)
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
        template: str | None = None,
        docker: bool | str | None = None,
        docker_template: str = DOCKER_TEMPLATE,
        dockerd_wait_sec: float = 120,
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
        # Docker mode: asked for, implied by a Docker template, or (auto) for
        # what the image unpacking can't run. Decided before
        # BaseEnvironment.__init__, whose validators read the capabilities.
        choice = _docker_choice(docker)
        if choice is None and template and template.startswith("prized-docker"):
            choice = True
        self._docker_auto = choice is None
        # Auto also takes a prebuilt task with a closed phase (no-network, or
        # an allowlist with no hosts): Docker closes it inside the sandbox,
        # without waiting for Prized's network switch.
        self._docker_for_network = (
            choice is None
            and not self._needs_docker(
                environment_dir, task_env_config, kwargs.get("extra_docker_compose")
            )
            and any(
                _closes_network(p)
                for p in (
                    kwargs.get("network_policy"),
                    *(kwargs.get("phase_network_policies") or []),
                )
            )
        )
        self._docker = (
            self._needs_docker(
                environment_dir,
                task_env_config,
                kwargs.get("extra_docker_compose"),
            )
            or self._docker_for_network
            if choice is None
            else choice
        )
        if self._docker:
            self._template = (
                template
                if template and template.startswith("prized-docker")
                else docker_template
            )
        else:
            self._template = template or "prized-base-v1"
        self._dockerd_wait_sec = float(dockerd_wait_sec)
        self._restricted: bool | None = None
        # Before BaseEnvironment.__init__: its validators run the early check.
        self._compose: PrizedDocker | None = (
            PrizedDocker(self) if self._docker else None
        )
        self._max_runtime_min = int(max_runtime_min)
        self._paused_ttl_min = int(paused_ttl_min)
        self._min_vcpu = int(min_vcpu)
        self._image_headroom_gb = int(image_headroom_gb)
        self._registry_auth = _registry_auth(registry_auth)
        self._fork_mode = _flag("fork_mode", fork_mode)
        self._fork_disk_budget_gb = int(fork_disk_budget_gb)
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
        for policy in self._network_policies:
            api_network(policy)  # an allowlist with hosts: refused here
        if (
            not self._docker
            and self._needs_network_switch
            and _network_refused_recently()
        ):
            # The API said so a moment ago: refused before a sandbox is made.
            raise ValueError(f"{_NETWORK_REFUSED}.")

    @staticmethod
    def _needs_docker(
        environment_dir: Path,
        task_env_config: EnvironmentConfig,
        extra_docker_compose: Any = None,
    ) -> bool:
        """True for what unpacking a prebuilt image can't run: a task with no
        ``docker_image`` (its environment is built from a Dockerfile), or one
        that uses Docker Compose."""
        return (
            not task_env_config.docker_image
            or (environment_dir / "docker-compose.yaml").exists()
            or bool(extra_docker_compose)
        )

    @staticmethod
    @override
    def type() -> str:
        return "prized"

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        # No network, an allowlist with no hosts (Harbor refuses one with
        # hosts: no entry type is declared), and switching while the trial
        # runs. Whether this Prized can, the API says at start (_start).
        # Harbor versions without these fields get none of them.
        # In Docker mode Docker enforces them inside the sandbox, and the
        # task's Compose services run too.
        fields = EnvironmentCapabilities.model_fields
        network = ("disable_internet", "network_allowlist", "dynamic_network_policy")
        flags = {name: True for name in network if name in fields}
        if self._docker:
            flags["docker_compose"] = True
        return EnvironmentCapabilities(**flags)

    @property
    @override
    def _uses_compose(self) -> bool:
        return self._docker

    @override
    def _compose_service_transport(
        self, service: str | None
    ) -> ComposeServiceTransport:
        if self._compose is None:
            raise self._compose_unsupported(service)
        return self._compose

    @property
    def _network_policies(self) -> list[NetworkPolicy]:
        """The startup policy and every phase's (Harbor's base class keeps them)."""
        policy = getattr(self, "_network_policy", None)
        phases = getattr(self, "_phase_network_policies", None) or []
        return [p for p in (policy, *phases) if p is not None]

    @property
    def _docker_restricted(self) -> bool:
        """Docker mode: some policy of this trial closes the network, so the
        project's networks are internal and egress is a network attached
        while a phase is public (``_docker.py``). Decided once, from the
        policies the trial starts with: each switch replaces the current one."""
        if self._restricted is None:
            self._restricted = any(
                api_network(p) == "none" for p in self._network_policies
            )
        return self._restricted

    @property
    def _docker_startup_network(self) -> str:
        policy = getattr(self, "_network_policy", None)
        return "default" if policy is None else api_network(policy)

    @property
    def _needs_network_switch(self) -> bool:
        """Some policy of this trial is not public: Prized must turn the
        sandbox's network off at some point."""
        return any(p.network_mode != NetworkMode.PUBLIC for p in self._network_policies)

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
        if self._docker:
            require_agent_environment_definition(
                self.environment_dir,
                docker_image=self.task_env_config.docker_image,
                extra_docker_compose_paths=self.extra_docker_compose_paths,
            )
            why = _docker_refused_recently(self._template)
            if why is not None:
                # Prized refused the Docker template a moment ago.
                raise ValueError(self._docker_refused_message(why))
            if self._docker_restricted and self._compose is not None:
                # Before a sandbox is made, from the files as written; the
                # sandbox checks the project as Compose resolves it.
                self._compose.precheck()
        elif not self.task_env_config.docker_image:
            raise ValueError(_DOCKERFILE_REFUSED)
        elif (self.environment_dir / "docker-compose.yaml").exists():
            raise ValueError(_COMPOSE_REFUSED)
        storage_gb = self._storage_gb()
        if storage_gb > _MAX_DISK_GB:
            raise ValueError(
                f"The task asks for {storage_gb} GB of storage; a Prized sandbox "
                f"has at most {_MAX_DISK_GB} GB of disk."
            )

    def _docker_refused_message(self, why: str) -> str:
        """The refusal for a Docker-mode task where Prized refused the template:
        what such a task was told before Docker mode, and why it still is."""
        if self._docker_for_network:
            return (
                f"{_NETWORK_REFUSED}. Prized closes it with Docker in "
                f"{self._template} sandboxes, which this workspace can't use "
                f"yet ({why})."
            )
        if self._docker_auto:
            # As before Docker mode: a missing image first, then Compose.
            if not self.task_env_config.docker_image:
                before = _DOCKERFILE_REFUSED
            elif (self.environment_dir / "docker-compose.yaml").exists():
                before = _COMPOSE_REFUSED
            else:
                before = (
                    f"{self.type()} environment does not support "
                    "--extra-docker-compose."
                )
            return (
                f"{before} Prized builds Dockerfiles and runs Compose in "
                f"{self._template} sandboxes, which this workspace can't use "
                f"yet ({why})."
            )
        return (
            f"Docker mode needs the {self._template} sandbox template, which "
            f"this workspace can't use yet ({why}). Until it can, run Docker "
            "and Compose tasks with Harbor's own `-e docker` on a Prized box: "
            "https://github.com/prized-dev/integrations/tree/main/harbor#dockerfile-and-compose-tasks"
        )

    # -- the sandbox ------------------------------------------------------

    def _require_sandbox(self) -> AsyncSandbox:
        if self._sandbox is None:
            raise RuntimeError("Prized sandbox is not running; call start() first.")
        return self._sandbox

    async def _host_exec(
        self,
        command: str,
        timeout_sec: int | None = 600,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Runs a command on the sandbox itself, outside the image.

        ``timeout_sec`` None or 0 is no limit. The SDK runs a command that may
        last more than a few minutes detached and polls it, so a long agent run
        outlives a dropped connection, and cancelling the call stops it.
        """
        sandbox = self._require_sandbox()
        try:
            extra = {"env": env} if env else {}
            result = await sandbox.exec(
                command, timeout=timeout_sec or 0, full_output=True, **extra
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
        In Docker mode the daemons get 512 MB of memory on top of the task's.
        """
        cpus = self._effective_cpus
        vcpu = max(self._min_vcpu, 1, math.ceil(cpus) if cpus else 2)
        memory_mb = max(1024, int(self._effective_memory_mb or 4096))
        if self._docker:
            memory_mb += _DOCKER_MEMORY_HEADROOM_MB
        disk_gb = min(
            _MAX_DISK_GB, max(10, self._storage_gb()) + self._image_headroom_gb
        )
        return vcpu, memory_mb, disk_gb

    @override
    async def start(self, force_build: bool) -> None:
        image = self.task_env_config.docker_image
        if image is None and not self._docker:
            raise ValueError("Prized needs [environment].docker_image.")
        if (
            force_build
            and not self._docker
            and (self.environment_dir / "Dockerfile").exists()
        ):
            self.logger.warning(
                "Prized runs the prebuilt docker_image; force_build is ignored "
                "and environment/Dockerfile is not built."
            )
        try:
            await self._start(image, force_build)
        except PrizedError as exc:
            # A request that got no clear answer leaves the sandbox in doubt:
            # it was deleted, so start once more on a fresh one.
            if exc.code != "network_error":
                raise
            self.logger.warning(f"Prized sandbox start failed ({exc}); retrying once")
            await self._start(image, force_build)

    def _entry(self, registry: str) -> dict[str, str] | None:
        """registry_auth's entry for ``registry``; a missing one is said once a run."""
        assert self._registry_auth is not None
        entry = self._registry_auth.for_registry(registry)
        if entry is None:
            key = (self._registry_auth.source, registry)
            if key not in _WARNED_NO_CREDENTIALS:  # once a run, not once a trial
                _WARNED_NO_CREDENTIALS.add(key)
                self.logger.warning(
                    f"registry_auth={self._registry_auth.source} has no "
                    f"credentials for {registry}; its images are pulled anonymously."
                )
        return entry

    def _pull_credentials(self, image: str | None) -> tuple[str, bytes] | None:
        """registry_auth's credentials, read before a sandbox starts: the
        image's registry's entry, or in Docker mode one for each registry
        the build and pulls name (Docker Hub always)."""
        if self._registry_auth is None:
            return None
        if self._compose is not None:
            references = image_references(
                self.environment_dir, self.task_env_config.docker_image
            )
            registries = sorted({HUB, *map(image_registry, references)})
            entries = {r: e for r in registries if (e := self._entry(r)) is not None}
            if not entries:
                return None
            names = ", ".join(entries)
            self.logger.debug(
                f"Docker mode builds and pulls with the credentials for {names} "
                f"from {self._registry_auth.describe()}"
            )
            return names, docker_cli_config(entries)
        assert image is not None
        registry = image_registry(image)
        entry = self._entry(registry)
        if entry is None:
            return None
        self.logger.debug(
            f"Pulling {image} with the credentials for {registry} from "
            f"{self._registry_auth.describe()}"
        )
        return registry, docker_config(image, entry)

    async def _start(self, image: str | None, force_build: bool = False) -> None:
        if self._sandbox is not None:  # a second start replaces the first
            await self.stop(delete=True)
        credentials = (
            None  # no thread hop when there is nothing to read
            if self._registry_auth is None
            else await asyncio.to_thread(self._pull_credentials, image)
        )
        if self._fork_mode and await self._start_forked(
            image, credentials, force_build
        ):
            return
        try:
            await self._start_fresh(image, credentials, self._labels, force_build)
        except RuntimeError as exc:
            # Fork mode's parents hold disk a fresh start would have had.
            if not (
                self._fork_mode
                and _fork.disk_quota(exc.__cause__)
                and await _fork.make_way()
            ):
                raise
            self.logger.warning(
                "Prized fork mode: a quota refused the start, so the fork parents "
                "were deleted and the rest of the run starts fresh; starting again"
            )
            await self._start_fresh(image, credentials, self._labels, force_build)

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
            if self._docker and exc.code in _TEMPLATE_REFUSAL_CODES:
                # Not this workspace's yet: such tasks are refused as before.
                why = f"{exc.status} {exc.code}"
                if exc.code == "template_needs_payment" and exc.message:
                    why = f"{why}: {exc.message.rstrip('.')}"
                _note_docker_refused(self._template, why)
                raise RuntimeError(self._docker_refused_message(why)) from exc
            raise

    async def _start_fresh(
        self,
        image: str | None,
        credentials: tuple[str, bytes] | None,
        labels: dict[str, str],
        force_build: bool = False,
        parent: bool = False,
    ) -> None:
        """Creates a sandbox and unpacks the image into it (Docker mode:
        builds and pulls, then brings the project up), as ``self._sandbox``.
        A fork ``parent`` stops before anything of the trial's own: no
        container (Docker mode), no switch of the network."""
        self._sandbox = await self._create(labels)
        self._heartbeat = asyncio.create_task(self._keep_alive(self._sandbox))
        self.logger.debug(
            f"Prized sandbox {self._sandbox.id} ({self._template}) for "
            f"{image or self.environment_dir}"
        )
        try:
            if self._compose is not None:
                # Built or pulled with the sandbox's network; Docker puts the
                # task's containers under its policy (_docker.py).
                await self._compose.prepare(
                    force_build, None if credentials is None else credentials[1]
                )
                if not parent:
                    await self._compose.run()
                    await self._upload_environment_dir_after_start()
                return
            assert image is not None
            if self._needs_network_switch:
                # Asks for the network it has: answered only where Prized can
                # switch it, before the image is pulled for nothing.
                await self._set_network("default", probe=True)
            await self._prepare_image(image, credentials)
            workdir = self.task_env_config.workdir or self._image_workdir
            await self._checked(
                f"mkdir -p {shlex.quote(workdir)}", f"Failed to create {workdir}"
            )
            await self.ensure_dirs(self._mount_targets(writable_only=True))
            await self._upload_environment_dir_after_start()
            if not parent:  # each copy of a parent switches its own
                await self._set_startup_network()
        except BaseException:  # a cancelled start must not leave the sandbox behind
            if credentials is not None:
                # The setup's trap can't run if the setup was killed (its
                # timeout), and a delete that fails leaves the sandbox up.
                await _despite_cancel(self._remove_pull_auth())
            await _despite_cancel(self.stop(delete=True))
            raise

    # -- network ------------------------------------------------------------

    async def _set_startup_network(self) -> None:
        """Pulled with the network; the task starts under its own policy."""
        startup = api_network(self.network_policy)
        if startup != "default":
            await self._set_network(startup)

    @override
    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        # Harbor's set_network_policy, between a trial's phases.
        if self._compose is not None:
            await self._compose.set_egress(api_network(network_policy) == "default")
            return
        await self._set_network(api_network(network_policy))

    async def _set_network(self, network: str, probe: bool = False) -> None:
        """Puts the sandbox's network to ``network`` and returns once Prized
        enforces it. Where Prized can't (``probe``: also the route missing),
        refuses the task; a change that failed is asked again, twice."""
        sandbox = self._require_sandbox()
        deadline = time.monotonic() + _NETWORK_SWITCH_SEC
        failures = 0
        while True:
            try:
                answer = await put_network(sandbox, network)
            except PrizedError as exc:
                unavailable = exc.code in _NETWORK_UNAVAILABLE_CODES or (
                    probe and exc.status == 404
                )
                if unavailable:
                    _note_network_refused()
                    raise RuntimeError(
                        f"{_NETWORK_REFUSED} (the API answered {exc.status} {exc.code})."
                    ) from exc
                retry = exc.code == "network_change_pending" or (
                    exc.code == "network_change_failed" and failures < 2
                )
                if not retry or time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Prized could not set sandbox {sandbox.id}'s network to "
                        f'"{network}": {exc}'
                    ) from exc
                failures += exc.code == "network_change_failed"
                await asyncio.sleep(_NETWORK_RETRY_SEC)
                continue
            change = answer.get("networkChange") or {}
            if change.get("status") == "applied":
                self.logger.debug(f"Prized sandbox {sandbox.id} network: {network}")
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Prized sandbox {sandbox.id}'s network did not change to "
                    f'"{network}" within {_NETWORK_SWITCH_SEC:.0f} s (still '
                    f"{change.get('status') or 'unknown'})"
                )
            # Still pending after the API's hold: the same request joins it.

    # -- fork mode (see _fork.py) -------------------------------------------

    def _fork_key(
        self, image: str | None, task: str | None, force_build: bool = False
    ) -> str:
        """Everything a start depends on: trials with the same key can share a parent."""
        environment = getattr(self, "environment_id", None)
        if not isinstance(environment, str):  # Harbor 0.24 has no environment_id
            environment = str(self.environment_dir.resolve())
        if self._compose is not None:
            # Docker mode: what the build and pulls read, not the task, so
            # tasks with the same environment share one build. Each copy
            # writes the trial's own compose files (resources, env, mounts,
            # networks) before it starts any container.
            parts: dict[str, Any] = {
                "mode": "docker",
                "template": self._template,
                "shape": list(self._shape()),
                "timers": [self._max_runtime_min, self._paused_ttl_min],
                "environment": environment,
                "image": self.task_env_config.docker_image,
                "force_build": bool(force_build),
                "build": self._compose.build_digest(),
            }
            blob = json.dumps(parts, sort_keys=True, default=str).encode()
            return hashlib.sha256(blob).hexdigest()[:16]
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
        self,
        image: str | None,
        credentials: tuple[str, bytes] | None,
        force_build: bool = False,
    ) -> bool:
        """Starts the trial in a fork of its parent (its task's; in Docker
        mode, its environment's), building the parent first if this trial is
        the first to need it. False: start fresh."""
        vcpu, memory_mb, disk_gb = self._shape()
        if not _fork.forkable(vcpu, memory_mb):
            return False
        trial_dir = self.trial_paths.trial_dir
        task = _fork.task_of(trial_dir)
        cache = _fork.cache(self._fork_disk_budget_gb)
        lease, evicted = cache.acquire(
            self._fork_key(image, task, force_build),
            task,
            cache.plan(trial_dir.parent),
            disk_gb,
            trial_dir.name,
            task_free=self._compose is not None,
        )
        copy: AsyncSandbox | None = None
        try:
            if evicted:  # gone before the new parent counts toward the quota
                await _despite_cancel(_fork.delete_parents(evicted, wait=True))
            if lease is None:
                return False
            parent = lease.parent
            if lease.builder:
                if not await self._build_parent(
                    cache, parent, image, credentials, force_build
                ):
                    return False
            elif not await _fork.wait_ready(parent):
                return False
            source, facts = parent.sandbox, parent.facts
            assert source is not None and facts is not None
            began = time.monotonic()
            copy = await self._fork_copy(cache, parent, source)
            if copy is None:
                return False
            self._sandbox = copy  # from here, a failed start deletes it
            forked = time.monotonic() - began
        finally:
            # The lease goes back as soon as the copy runs: the parent is
            # idle (and can make way) while the copy is set up.
            if lease is not None:
                doomed = cache.release(lease)
                if doomed is not None:
                    try:
                        await _despite_cancel(
                            _fork.delete_parent(doomed, wait=cache.quota_refused)
                        )
                    except BaseException:
                        if copy is not None:
                            await _despite_cancel(self.stop(delete=True))
                        raise
        assert copy is not None
        set_up = await self._set_up_copy(
            copy, source.id, facts, began, forked, force_build
        )
        if not set_up:
            # Fail closed: this trial builds fresh, and a parent whose copies
            # keep failing is deleted (its key builds fresh from then on).
            doomed = cache.copy_failed(parent)
            if doomed is not None:
                await _despite_cancel(_fork.delete_parent(doomed))
        return set_up

    async def _build_parent(
        self,
        cache: _fork.ParentCache,
        parent: _fork.Parent,
        image: str | None,
        credentials: tuple[str, bytes] | None,
        force_build: bool = False,
    ) -> bool:
        """Builds the parent: a fresh start under the parent's labels (Docker
        mode: up to the build and pulls, no container), then paused, with no
        credentials left on it. False: start fresh. A failure fresh mode would
        meet too (a failed pull or build) is raised."""
        labels = {
            **self._labels,
            "harbor-session": f"fork-parent-{parent.key}",
            "harbor-task": self.environment_name[:256],
            _fork.PARENT_LABEL: "1",
            _fork.RUN_LABEL: _fork.RUN_ID,
        }
        if self._compose is not None:
            labels["harbor-session"] = f"fork-parent-docker-{parent.key}"
        _fork.note_build()
        began = time.monotonic()
        try:
            await self._start_fresh(
                image, credentials, labels, force_build, parent=True
            )
        except RuntimeError as exc:
            if not _fork.disk_quota(exc.__cause__):
                raise
            self.logger.warning(
                f"Prized fork mode: a quota refused a parent of {self.environment_name} "
                f"({exc.__cause__}); the parents make way, and the rest of the run "
                "starts fresh"
            )
            cache.build_failed(parent)
            await _despite_cancel(_fork.delete_parents(cache.quota_hit(), wait=True))
            return False
        sandbox, heartbeat = self._sandbox, self._heartbeat
        self._sandbox = self._heartbeat = None
        if heartbeat is not None:
            heartbeat.cancel()
        assert sandbox is not None
        built = time.monotonic()
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
        what = (
            f"environment {parent.key} (Docker mode, built and pulled once)"
            if self._compose is not None
            else self.environment_name
        )
        self.logger.debug(
            f"Prized sandbox {sandbox.id} is the paused parent of {what} "
            f"(started in {built - began:.1f} s, paused in {time.monotonic() - built:.2f} s)"
        )
        return True

    async def _set_up_copy(
        self,
        copy: AsyncSandbox,
        source_id: str,
        facts: _fork.ImageFacts,
        began: float,
        forked: float,
        force_build: bool = False,
    ) -> bool:
        """Makes the trial's copy its sandbox: the image's facts, a heartbeat,
        and its own hostname in the image, then the trial's startup network
        (Docker mode: the trial's compose files and its containers, up
        without a build). False (the copy deleted): start fresh."""
        self._image_env = dict(facts.env)
        self._image_workdir, self._image_user = facts.workdir, facts.user
        self._heartbeat = asyncio.create_task(self._keep_alive(copy))
        try:
            if self._compose is not None:
                await self._compose.resume_copy(force_build)
                await self._upload_environment_dir_after_start()
            else:
                await self._host_checked(
                    "set -e; " + _IDENTITY, "Could not write the copy's hostname"
                )
                await self._set_startup_network()
        except BaseException as exc:
            await _despite_cancel(self.stop(delete=True))
            if not isinstance(exc, (PrizedError, RuntimeError)):
                raise
            again = (
                "building it fresh" if self._compose is not None else "starting fresh"
            )
            self.logger.warning(
                f"Prized fork mode: copy {copy.id} of {source_id} failed ({exc}); "
                f"{again} for this trial"
            )
            return False
        self.logger.debug(
            f"Prized sandbox {copy.id} forked from {source_id} in {forked:.2f} s "
            f"(ready in {time.monotonic() - began:.2f} s)"
        )
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
                if action == "quota" and _fork.disk_quota(exc):
                    # Parents make way: the idle ones go now, this one with
                    # its last lease (before this trial's fresh start).
                    idle = cache.quota_hit()
                    if attempt == 1 and idle:
                        await _despite_cancel(_fork.delete_parents(idle, wait=True))
                        continue
                elif attempt == 1 and action == "retry":
                    self.logger.debug(f"Fork of {source.id} refused ({exc}); again")
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                if action in ("drop", "never", "gone"):
                    cache.drop(parent, failed=action != "gone", never=action == "never")
                again = (
                    "building it fresh"
                    if self._compose is not None
                    else "starting fresh"
                )
                self.logger.warning(
                    f"Prized fork mode: could not fork {source.id} ({exc}); "
                    f"{again} for this trial"
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
        if self._compose is not None:
            return await self._compose.exec(
                command,
                cwd=cwd or self.task_env_config.workdir,
                env=self._merge_env(env),
                timeout_sec=timeout_sec,
                user=self._resolve_user(user),
            )
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
    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        if self._compose is not None:
            return await self._compose.is_dir(path, user=self._resolve_user(user))
        return await super().is_dir(path, user=user)

    @override
    async def is_file(self, path: str, user: str | int | None = None) -> bool:
        if self._compose is not None:
            return await self._compose.is_file(path, user=self._resolve_user(user))
        return await super().is_file(path, user=user)

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        if self._compose is not None:
            return await self._compose.upload_file(source_path, target_path)
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
        if self._compose is not None:
            return await self._compose.upload_dir(source_dir, target_dir)
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
        if self._compose is not None:
            return await self._compose.download_file(source_path, target_path)
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
        if self._compose is not None:
            return await self._compose.download_dir(source_dir, target_dir)
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
