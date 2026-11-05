"""Options, session state, and the checks made before a sandbox is paid for."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from agents.sandbox.errors import ConfigurationError, ErrorCode
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import BaseSandboxClientOptions, SandboxSessionState
from pydantic import BaseModel, ConfigDict, Field, field_validator

from openai_agents_prized._api import EDGE_MAX_TIMEOUT

DEFAULT_PRIZED_WORKSPACE_ROOT = "/home/prized/work"
"""The workspace root a session uses when its manifest names none of its own."""

DEFAULT_PRIZED_TEMPLATE = "prized-base-v1"
"""The template a new sandbox boots from unless the options name another."""

PRIZED_HOME = "/home/prized"
"""The sandbox user's home; the file API reaches only paths under it."""

PRIZED_USER = "prized"
"""The user every command runs as."""

_SDK_DEFAULT_ROOT = "/workspace"
"""`Manifest().root`: the user `prized` cannot create it, so it maps to ours."""

RESERVED_PORTS = frozenset({22, 81})
"""The sandbox's own ports: they have no URL."""

ExposedPortAuth = Literal["token", "key"]
"""How a request reaches an exposed port's URL.

- `token`: the URL carries a short-lived token (`prized_token`) for that one
  port, so any client (a program, `fetch`, a WebSocket, a browser tab) opens it
  with nothing else. Needs a workspace that has paid in.
- `key`: the URL carries nothing; the request sends the API key in the
  `Prized-Key` header. Works on the free tier, never expires, and wakes a
  paused sandbox.
"""


def check_exposed_ports(value: object) -> tuple[int, ...]:
    """Normalize `exposed_ports` and refuse ports a Prized sandbox has no URL for.

    Args:
        value: An int, an iterable of ints, or `None`.

    Returns:
        The ports, in order, without repeats.

    Raises:
        ValueError: If it is not ints, or a port is not 1 to 65535, or is 22
            or 81 (pydantic reports it as a `ValidationError`).
    """
    if value is None:
        return ()
    if isinstance(value, int) and not isinstance(value, bool):
        ports: Iterable[object] = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        ports = value
    else:
        msg = "exposed_ports must be an iterable of TCP port integers"
        raise ValueError(msg)  # noqa: TRY004  # pydantic reports only a ValueError as invalid
    normalized: list[int] = []
    for port in ports:
        if isinstance(port, bool) or not isinstance(port, int):
            msg = "exposed_ports must contain integers"
            raise ValueError(msg)  # noqa: TRY004  # as above
        if not 1 <= port <= 65535:  # noqa: PLR2004  # the TCP port range
            msg = "exposed_ports entries must be between 1 and 65535"
            raise ValueError(msg)
        if port in RESERVED_PORTS:
            msg = f"port {port} is the sandbox's own (22 and 81 have no URL)"
            raise ValueError(msg)
        if port not in normalized:
            normalized.append(port)
    return tuple(normalized)


class PrizedSandboxTimeouts(BaseModel):
    """Timeouts, in seconds, for Prized sandbox operations."""

    model_config = ConfigDict(frozen=True)

    exec_timeout_unbounded_s: int = Field(default=EDGE_MAX_TIMEOUT, ge=1, le=EDGE_MAX_TIMEOUT)
    """An `exec` without a timeout gets this one; an `exec` runs an hour at most.

    Processes started with `pty_exec_start` (the `exec_command` tool) run in the
    background without either limit unless they are given a `timeout`; the
    session ends them when it stops.
    """

    start_s: float = Field(default=300.0, gt=0)
    """How long a create or resume may take to reach `running`."""

    fast_op_s: int = Field(default=30, ge=1, le=EDGE_MAX_TIMEOUT)
    """Small housekeeping commands, such as creating the workspace root."""

    workspace_tar_s: int = Field(default=900, ge=1, le=EDGE_MAX_TIMEOUT)
    """Archiving or restoring the workspace, and staged file copies."""


class PrizedSandboxClientOptions(BaseSandboxClientOptions):
    """Options for a new Prized sandbox.

    Every field maps to a field of the Sandbox API's create
    (https://prized.dev/docs/sandboxes-api#create); `None` keeps the API's
    default.
    """

    type: Literal["prized"] = "prized"
    template: str = DEFAULT_PRIZED_TEMPLATE
    vcpu: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    env_vars: dict[str, str] | None = None
    labels: dict[str, str] | None = None
    auto_pause_min: int | None = None
    auto_delete_min: int | None = None
    max_runtime_min: int | None = None
    pause_on_exit: bool = False
    timeouts: PrizedSandboxTimeouts | dict[str, object] | None = None
    exposed_ports: tuple[int, ...] = ()
    exposed_port_url_ttl_s: int = Field(default=3600, ge=60, le=86400)
    exposed_port_auth: ExposedPortAuth = "token"

    @field_validator("exposed_ports", mode="before")
    @classmethod
    def _check_exposed_ports(cls, value: object) -> tuple[int, ...]:
        return check_exposed_ports(value)

    def __init__(  # noqa: PLR0913, PLR0917  # positional like the built-in options
        self,
        template: str = DEFAULT_PRIZED_TEMPLATE,
        vcpu: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        auto_pause_min: int | None = None,
        auto_delete_min: int | None = None,
        max_runtime_min: int | None = None,
        pause_on_exit: bool = False,  # noqa: FBT001, FBT002  # positional like the built-in options
        timeouts: PrizedSandboxTimeouts | dict[str, object] | None = None,
        exposed_ports: Iterable[int] = (),
        exposed_port_url_ttl_s: int = 3600,
        exposed_port_auth: ExposedPortAuth = "token",
        *,
        type: Literal["prized"] = "prized",  # noqa: A002  # the SDK's discriminator field
    ) -> None:
        """Create options.

        Args:
            template: The template to boot (`prized-base-v1`, `prized-agent-v1`).
            vcpu: vCPUs; the template's default when `None`.
            memory_mb: Memory in MiB; the template's default when `None`.
            disk_gb: Disk in GiB; the template's default when `None`.
            env_vars: Environment variables set for every command. They are
                stored in the session state so a replacement sandbox gets them.
            labels: Labels on the sandbox, yours to filter on.
            auto_pause_min: Pause after this many idle minutes.
            auto_delete_min: Delete after this many minutes paused.
            max_runtime_min: Delete after this many minutes of running (24 hours
                when unset, up to 7 days; 60 minutes on the free tier).
            pause_on_exit: Pause the sandbox at the end of a session instead of
                deleting it, so a later `resume()` continues in it.
            timeouts: Operation timeouts.
            exposed_ports: Ports `resolve_exposed_port()` gives a URL for:
                any port but 22 and 81, on any address in the sandbox
                (127.0.0.1 included). Nothing is opened at create.
            exposed_port_url_ttl_s: How long a token URL from
                `resolve_exposed_port()` opens its port, 60 to 86400 seconds.
                Each call makes a new one.
            exposed_port_auth: `token` (the URL carries a token for that one
                port; needs a workspace that has paid in) or `key` (send
                the API key in the `Prized-Key` header).
            type: The options discriminator, always `prized`.
        """
        super().__init__(
            type=type,
            template=template,
            vcpu=vcpu,
            memory_mb=memory_mb,
            disk_gb=disk_gb,
            env_vars=env_vars,
            labels=labels,
            auto_pause_min=auto_pause_min,
            auto_delete_min=auto_delete_min,
            max_runtime_min=max_runtime_min,
            pause_on_exit=pause_on_exit,
            timeouts=timeouts,
            exposed_ports=exposed_ports,
            exposed_port_url_ttl_s=exposed_port_url_ttl_s,
            exposed_port_auth=exposed_port_auth,
        )


class PrizedSandboxSessionState(SandboxSessionState):
    """Serializable state of a Prized-backed session."""

    # The SDK registers each state class by its `type` literal, as the
    # built-in clients' states do.
    type: Literal["prized"] = "prized"  # ty: ignore[invalid-mutable-override]
    sandbox_id: str
    template: str = DEFAULT_PRIZED_TEMPLATE
    vcpu: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    base_env_vars: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    auto_pause_min: int | None = None
    auto_delete_min: int | None = None
    max_runtime_min: int | None = None
    pause_on_exit: bool = False
    timeouts: PrizedSandboxTimeouts = Field(default_factory=PrizedSandboxTimeouts)
    exposed_port_url_ttl_s: int = Field(default=3600, ge=60, le=86400)
    exposed_port_auth: ExposedPortAuth = "token"


def create_body(state: PrizedSandboxSessionState) -> dict[str, Any]:
    body: dict[str, Any] = {"template": state.template}
    resources = {
        name: value
        for name, value in (
            ("vcpu", state.vcpu),
            ("memoryMb", state.memory_mb),
            ("diskGb", state.disk_gb),
        )
        if value is not None
    }
    if resources:
        body["resources"] = resources
    if state.base_env_vars:
        body["envVars"] = dict(state.base_env_vars)
    if state.labels:
        body["labels"] = dict(state.labels)
    timers = (
        ("autoPauseMin", state.auto_pause_min),
        ("autoDeleteMin", state.auto_delete_min),
        ("maxRuntimeMin", state.max_runtime_min),
    )
    body.update({name: value for name, value in timers if value is not None})
    return body


def resolve_manifest_root(manifest: Manifest | None) -> Manifest:
    if manifest is None:
        return Manifest(root=DEFAULT_PRIZED_WORKSPACE_ROOT)
    if manifest.root == _SDK_DEFAULT_ROOT:
        return manifest.model_copy(update={"root": DEFAULT_PRIZED_WORKSPACE_ROOT})
    return manifest


def _unsupported_accounts(manifest: Manifest) -> list[str]:
    names = [f"user {user.name}" for user in manifest.users if user.name != PRIZED_USER]
    names.extend(f"group {group.name}" for group in manifest.groups)
    return names


def check_manifest(manifest: Manifest) -> None:
    """Refuse what a Prized sandbox cannot do, before a sandbox is paid for."""
    accounts = _unsupported_accounts(manifest)
    if accounts:
        raise ConfigurationError(
            message=(
                "Prized sandboxes run every command as `prized` and have no root, so "
                f"manifest users and groups are not supported: {', '.join(accounts)}"
            ),
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op="start",
            context={"backend": "prized", "accounts": accounts},
        )
    mounts = [path.as_posix() for _mount, path in manifest.mount_targets()]
    if mounts:
        raise ConfigurationError(
            message=(
                "Prized sandboxes have no root, so mounts are not supported: "
                f"{', '.join(mounts)}. Copy the data in with LocalDir or GitRepo, "
                "or fetch it with a command."
            ),
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op="start",
            context={"backend": "prized", "mounts": mounts},
        )


def coerce_timeouts(
    value: PrizedSandboxTimeouts | dict[str, object] | None,
) -> PrizedSandboxTimeouts:
    if isinstance(value, PrizedSandboxTimeouts):
        return value
    if value is None:
        return PrizedSandboxTimeouts()
    return PrizedSandboxTimeouts.model_validate(value)
