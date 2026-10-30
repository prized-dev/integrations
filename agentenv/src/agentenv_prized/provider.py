"""Prized sandbox provider for agent-env.

Each sandbox is one Prized microVM, created through the Prized API from a template. agent-env's VM flows (an env's
docker-compose behind its gateway, an agent's ``docker run``, image loading and builds) run under Docker inside it,
on a template that has Docker (``prized-docker-v1``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Mapping
from typing import Any, ClassVar, Self

from agent_env.attribution import Attribution
from agent_env.config.errors import ConfigError
from agent_env.providers.sandbox_providers.sandbox import NetworkPolicy
from agent_env.providers.sandbox_providers.sandbox_provider import (
    SandboxNotFoundError,
    SandboxProvider,
    apply_default_attribution,
    refuse_unenforceable_policy,
)
from prized import AsyncSandbox, PrizedError

from agentenv_prized.sandbox import (
    GONE_CODES,
    GONE_STATES,
    ROOT_HELPER_IMAGE,
    PrizedSandbox,
    tunnel_urls_for,
)

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE = "prized-docker-v1"
_SECTION = "[sandbox.providers.prized.config]"
_CONFIG_KEYS = frozenset({"api_key", "template", "base_url", "edge_url", "root_helper_image"})

# The API's ceiling for any template: a request beyond it is refused before anything is created.
MAX_VCPU = 32
MAX_MEMORY_MB = 131072
MAX_DISK_GB = 128
# A sandbox's run time is set in whole minutes, up to 7 days on a paid workspace.
MAX_RUNTIME_MIN = 7 * 24 * 60
# Ports the platform keeps for itself, which never get a port URL.
RESERVED_PORTS = frozenset({22, 81})

# Labels every sandbox carries: one marking it as agent-env's, and the exposed ports, which get_sandbox reads back on a
# freshly built provider. Attribution goes in labels under ATTRIBUTION_PREFIX.
MARK_LABEL = "agentenv"
PORTS_LABEL = "agentenv-ports"
ATTRIBUTION_PREFIX = "agentenv-"
MAX_LABELS = 32
MAX_LABEL_KEY = 64
MAX_LABEL_VALUE = 256
_LABEL_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
# How long a create may take to reach running before it is given up (and its sandbox deleted).
CREATE_TIMEOUT_SEC = 180.0
# Codes of a create the request itself can't satisfy, as opposed to a platform failure.
_UNSATISFIABLE_CODES = frozenset(
    {"invalid_resources", "template_not_found", "template_inactive", "invalid_max_runtime"}
)


def sandbox_shape(cpu: float, memory_mb: int, disk_size_gb: float) -> tuple[int, int, int]:
    """``(vcpu, memory_mb, disk_gb)`` covering the request: whole vCPUs and GiB, rounded up. A request past the API's
    ceiling is refused, not shrunk; the template's own range is checked by the API."""
    vcpu = max(1, math.ceil(cpu))
    memory = max(1, math.ceil(memory_mb))
    disk = max(1, math.ceil(disk_size_gb))
    if vcpu > MAX_VCPU or memory > MAX_MEMORY_MB or disk > MAX_DISK_GB:
        raise ValueError(
            f"Prized cannot size a sandbox for cpu={cpu}, memory={memory_mb}MB, disk={disk_size_gb}GB "
            f"(at most {MAX_VCPU} vCPU, {MAX_MEMORY_MB} MiB memory, {MAX_DISK_GB} GB disk)"
        )
    return vcpu, memory, disk


def runtime_minutes(timeout: int) -> int:
    """The sandbox's run time for a lifetime of ``timeout`` seconds: whole minutes, rounded up."""
    minutes = max(1, math.ceil(timeout / 60))
    if minutes > MAX_RUNTIME_MIN:
        raise ValueError(
            f"Prized sandboxes run for at most {MAX_RUNTIME_MIN} minutes (7 days); timeout={timeout}s asks for more"
        )
    return minutes


def checked_ports(exposed_ports: list[int] | None) -> list[int]:
    ports = list(dict.fromkeys(exposed_ports or []))
    for port in ports:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"exposed port {port!r} is not a TCP port")
        if port in RESERVED_PORTS:
            raise ValueError(f"port {port} is kept by Prized and can't be exposed; use another")
    return ports


def sandbox_labels(attribution: Mapping[str, Any], ports: list[int]) -> dict[str, str]:
    """The sandbox's labels: the agent-env mark, its exposed ports and each attribution dimension, within the API's
    limits (32 labels, keys up to 64 characters, values up to 256). Attribution that doesn't fit is dropped with a
    warning, never the mark or the ports."""
    labels = {MARK_LABEL: "true"}
    if ports:
        labels[PORTS_LABEL] = ",".join(str(port) for port in ports)
    dropped: list[str] = []
    for key, value in attribution.items():
        if value is None:
            continue
        label = (ATTRIBUTION_PREFIX + _LABEL_KEY_UNSAFE.sub("-", str(key)))[:MAX_LABEL_KEY]
        if label in labels or len(labels) >= MAX_LABELS:
            dropped.append(str(key))
            continue
        labels[label] = str(value)[:MAX_LABEL_VALUE]
    if dropped:
        logger.warning("Attribution %s left off the Prized sandbox's labels (at most %s labels)", dropped, MAX_LABELS)
    return labels


def ports_from_labels(labels: Mapping[str, str]) -> list[int]:
    raw = labels.get(PORTS_LABEL, "")
    return [int(part) for part in raw.split(",") if part.strip().isdigit()]


class PrizedSandboxProvider(SandboxProvider):
    """Create Prized sandboxes from a template with Docker, and reconnect to them by id.

    ``api_key`` comes from resolved provider config, never from the process environment: configure it with an ``env:``
    or ``secret:`` reference in ``[sandbox.providers.prized.config]``. Workloads never see it.
    """

    # Prized gives no egress controls yet, so only unrestricted egress is supported (the base class's default).
    EGRESS_HOSTS: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        *,
        api_key: str,
        template: str = DEFAULT_TEMPLATE,
        base_url: str | None = None,
        edge_url: str | None = None,
        root_helper_image: str = ROOT_HELPER_IMAGE,
        sandbox_cls: Any = AsyncSandbox,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Prized api_key must be a non-empty resolved secret")
        if not isinstance(template, str) or not template.strip():
            raise ValueError("Prized template must be a non-empty string")
        self._api_key = api_key
        self._template = template
        self._base_url = base_url
        self._edge_url = edge_url
        self._root_helper_image = root_helper_image
        # Injectable so unit tests never reach the API.
        self._sandbox_cls = sandbox_cls
        self._made: list[PrizedSandbox] = []

    @classmethod
    def from_config(cls, **config: Any) -> Self:
        unknown = set(config) - _CONFIG_KEYS
        if unknown:
            raise ConfigError(f"{_SECTION} has unknown key(s): {sorted(unknown)}")
        if not config.get("api_key"):
            raise ConfigError(f"{_SECTION} requires a non-empty 'api_key' (an env: or secret: reference)")
        for key in _CONFIG_KEYS:
            value = config.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ConfigError(f"{_SECTION} '{key}' must be a non-empty string")
        return cls(**config)

    def _auth(self) -> dict[str, Any]:
        return {"api_key": self._api_key, "base_url": self._base_url, "edge_url": self._edge_url}

    async def _connect(self, sandbox_id: str) -> AsyncSandbox:
        return await self._sandbox_cls.connect(sandbox_id, **self._auth())

    def _handle(self, sdk: Any, tunnel_urls: Mapping[int, str]) -> PrizedSandbox:
        """A handle on ``sdk``'s sandbox, which keeps ``sdk`` for this event loop."""
        sandbox = PrizedSandbox(
            sdk.id,
            connect=self._connect,
            tunnel_urls=tunnel_urls,
            # Every Prized sandbox has unrestricted egress.
            network_policy=NetworkPolicy(),
            sdk=sdk,
            root_helper_image=self._root_helper_image,
        )
        self._made.append(sandbox)
        return sandbox

    async def close(self) -> None:
        """Release the SDK handles of the sandboxes this provider made, on this event loop; the sandboxes keep
        running."""
        made, self._made = self._made, []
        for sandbox in made:
            await sandbox.close()

    async def create_vm(
        self,
        *,
        image: str | None = None,
        boot_mode: str | None = None,
        cpu: float = 1.0,
        memory: int = 8192,
        disk_size_gb: float = 10,
        timeout: int = 3600 * 2,
        exposed_ports: list[int] | None = None,
        setup_for_gateway: bool = True,
        attribution: Attribution | None = None,
        network_policy: NetworkPolicy | None = None,
    ) -> PrizedSandbox:
        """Create a sandbox from the configured template. ``timeout`` is its run time, after which Prized deletes it
        even if a run never tears it down. A per-call ``image`` is refused rather than ignored (sandboxes boot the
        template, and OCI images run under Docker inside); ``boot_mode`` has no Prized equivalent and is ignored."""
        refuse_unenforceable_policy(self, network_policy)
        if image is not None:
            raise ValueError("Prized sandboxes boot the configured template; a per-call image is unsupported")
        if boot_mode is not None:
            logger.warning("Ignoring boot_mode=%s: Prized sandboxes have no boot modes", boot_mode)
        vcpu, memory_mb, disk_gb = sandbox_shape(cpu, memory, disk_size_gb)
        minutes = runtime_minutes(timeout)
        ports = checked_ports(exposed_ports)
        resolved_attribution = apply_default_attribution(dict(attribution or {}))
        labels = sandbox_labels(resolved_attribution, ports)
        try:
            sdk = await self._sandbox_cls.create(
                self._template,
                vcpu=vcpu,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                labels=labels,
                max_runtime_min=minutes,
                timeout=CREATE_TIMEOUT_SEC,
                **self._auth(),
            )
        except PrizedError as exc:
            # The SDK deletes a sandbox its create made but didn't hand back (cancelled, lost answer, never running).
            if exc.code in _UNSATISFIABLE_CODES:
                raise ValueError(f"Prized refused the sandbox: {exc}") from exc
            raise
        sandbox = self._handle(sdk, {})
        try:
            sandbox.tunnel_urls = tunnel_urls_for(sdk.info.raw, ports)
            logger.info(
                "Prized sandbox started: sandbox_id=%s template=%s vcpu=%s memory_mb=%s disk_gb=%s ports=%s "
                "attribution=%s",
                sdk.id,
                self._template,
                vcpu,
                memory_mb,
                disk_gb,
                ports,
                resolved_attribution,
            )
            if setup_for_gateway:
                await sandbox.setup_vm_for_gateway(ports)
        except BaseException:
            # Deleted before the create's own error or cancellation is raised; shielded so a second cancellation
            # can't leave it running until its run time ends.
            await asyncio.shield(_delete_quietly(sandbox))
            raise
        return sandbox

    async def create_sandbox(
        self,
        *,
        image_name: str,
        port: int,
        env: dict[str, str],
        cpu: float = 1.0,
        memory: int = 8192,
        disk_size_gb: float = 10,
        timeout: int = 3600 * 2,
        attribution: Attribution | None = None,
        network_policy: NetworkPolicy | None = None,
    ) -> PrizedSandbox:
        # A VM backend: the agent paths start image_name inside the sandbox afterwards.
        del image_name, env
        return await self.create_vm(
            cpu=cpu,
            memory=memory,
            disk_size_gb=disk_size_gb,
            timeout=timeout,
            exposed_ports=[port],
            attribution=attribution,
            network_policy=network_policy,
        )

    async def get_sandbox(self, sandbox_id: str) -> PrizedSandbox:
        """Reconnect by id, restoring the exposed ports from the sandbox's labels."""
        try:
            sdk = await self._connect(sandbox_id)
        except PrizedError as exc:
            if exc.code in GONE_CODES or exc.status == 404:
                raise SandboxNotFoundError(f"Prized sandbox {sandbox_id!r} was not found") from exc
            raise
        if sdk.state in GONE_STATES:
            await sdk.close()
            raise SandboxNotFoundError(f"Prized sandbox {sandbox_id!r} is {sdk.state}")
        ports = ports_from_labels(sdk.labels)
        try:
            tunnel_urls = tunnel_urls_for(sdk.info.raw, ports)
        except ValueError:
            logger.warning("Prized sandbox %s has exposed ports %s but no port URLs", sandbox_id, ports)
            tunnel_urls = {}
        return self._handle(sdk, tunnel_urls)

    async def terminate_sandbox(self, sandbox_id: str) -> None:
        """Delete by id; one already gone is terminated. A sandbox in ``error`` is deleted too."""
        try:
            sdk = await self._connect(sandbox_id)
        except PrizedError as exc:
            if exc.code in GONE_CODES or exc.status == 404:
                return
            raise
        try:
            await sdk.delete()
        except PrizedError as exc:
            if exc.code not in GONE_CODES:
                raise
        finally:
            await sdk.close()


async def _delete_quietly(sandbox: PrizedSandbox) -> None:
    try:
        await sandbox.terminate()
    except Exception as exc:  # noqa: BLE001 - logged; its run time still ends it
        logger.error(
            "Prized sandbox %s from a failed create could not be deleted (%s); its run time ends it",
            sandbox.sandbox_id,
            exc,
        )
