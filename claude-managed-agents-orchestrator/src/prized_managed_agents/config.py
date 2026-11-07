"""Settings: what the orchestrator serves, where its sandboxes come from, and its clocks.

Everything has a default except the environment's id and key. ``Settings.from_env`` reads the
same names the README lists; tests build ``Settings`` directly.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

ANT_VERSION = "2.7.1"
# From the release's ant_2.7.1_checksums.txt (github.com/anthropics/anthropic-cli/releases/tag/v2.7.1).
ANT_SHA256 = {
    "amd64": "7c4bf5081532d994201bc3678657c8f314e487c521ec53937d15c0aca25c2bf0",
    "arm64": "a75b73c89a14fd0f8231b03e0b7655e10f45044808d4c58abfbf7ee6810944cb",
}
ANT_URL = "https://github.com/anthropics/anthropic-cli/releases/download/v{version}/ant_{version}_linux_{arch}.tar.gz"
DEFAULT_BASE_URL = "https://api.anthropic.com"
# The only variable that moves the environment key to another API host. ANTHROPIC_BASE_URL is not
# read: Claude Code and Anthropic's SDKs use it for gateways and proxies, and a shell that has it
# set for them would send the environment key there.
BASE_URL_ENV = "ORCH_ANTHROPIC_BASE_URL"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
WORKDIR = "/home/prized/work"

# Prized's free tier (a workspace on its free credit alone): 10 running sandboxes at once, each
# with 60 minutes of running in all, and 100 GB of disk across the workspace's sandboxes, paused
# ones included (prized.dev/docs/sandboxes#free-tier).
FREE_SANDBOXES = 10
FREE_RUN_S = 60 * 60
FREE_MAX_IDLE_S = 10
FREE_AUTO_PAUSE_S = 2 * 60

_DURATION = re.compile(r"\s*(\d+)\s*([smhd]?)\s*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ConfigError(ValueError):
    """A setting that can't be used, with a sentence that says which and why."""


def seconds(value: str, *, name: str, unit: str = "s") -> int:
    """``"90s"``, ``"15m"``, ``"2h"``, ``"7d"``, or a bare number in ``unit``."""
    m = _DURATION.fullmatch(value)
    if not m:
        raise ConfigError(f"{name}={value!r}: write a duration such as 600s, 15m, 2h or 7d")
    n, u = int(m.group(1)), m.group(2) or unit
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]


def minutes(secs: int) -> int:
    """Prized takes whole minutes; round up so a setting never shrinks."""
    return max(1, math.ceil(secs / 60))


def label_pairs(text: str) -> dict[str, str]:
    """``"team=infra,cost=cma"`` -> ``{"team": "infra", "cost": "cma"}``."""
    out: dict[str, str] = {}
    for item in filter(None, (p.strip() for p in text.split(","))):
        k, sep, v = item.partition("=")
        if not sep or not k:
            raise ConfigError(f"PRIZED_LABELS: expected key=value, got {item!r}")
        out[k] = v
    return out


def check_base_url(url: str) -> str:
    """The Anthropic API the environment key goes to: https, or plain http to this machine."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    ok = (
        bool(host)
        and not parts.username
        and not parts.password
        and (parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK))
    )
    if not ok:
        raise ConfigError(
            f"{BASE_URL_ENV}={url!r}: the environment key goes only to an https URL (or http on this machine)"
        )
    return url.rstrip("/")


@dataclass(frozen=True)
class Settings:
    environment_id: str
    environment_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    # ANTHROPIC_BASE_URL when it was set (and differs): never used, only named in a warning.
    ignored_base_url: str | None = None
    # With a signing key the orchestrator takes webhooks (and polls now and then as a backstop);
    # without one it long-polls the queue.
    webhook_key: str | None = field(default=None, repr=False)
    # After rotating the environment key: the old one, so runs started under it are still known
    # (run ids are signed with a key derived from it). Used for nothing else.
    previous_environment_key: str | None = field(default=None, repr=False)

    # The sandboxes.
    template: str = "prized-base-v1"
    vcpu: int | None = None
    memory_gb: float | None = None
    disk_gb: int | None = None
    warm_parent: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    keep_paused_s: int = 7 * 86400  # a paused session's sandbox waits this long for its next run
    # A warm parent is kept until it is retired (`prized-managed-agents retire`, or deleted), unless
    # this is set: then it is deleted after this long paused. Its copies would inherit that timer,
    # so the sweep holds them to KEEP_PAUSED instead.
    warm_parent_keep_s: int = 0
    max_runtime_s: int | None = None  # Prized's default (24h, restarting on each resume) when None
    # Our probes count as activity, so a served sandbox never idles out; if the orchestrator goes
    # away, its sandboxes pause themselves after this long (and their runs freeze, not die).
    auto_pause_s: int = 15 * 60
    # Session metadata may pick the template (prized_template) or a warm parent to fork
    # (prized_fork_from, which must carry this orchestrator's warm-parent label).
    session_overrides: bool = True

    # One run (one work item) of a session.
    max_idle_s: int = 60  # ant's --max-idle: how long the worker stays after the session goes idle
    run_timeout_s: int = 6 * 3600
    stop_grace_s: int = 30  # SIGTERM to SIGKILL; the docs ask for at least 30 s

    # Handing a work item to a sandbox. The queue's reclaim window is twice the handoff timeout,
    # so an item we fail to hand off comes back only after this attempt has given up.
    handoff_timeout_s: int = 120
    max_handoffs: int = 8  # sandboxes being prepared at once; no polling while all are busy
    max_sessions: int = 50  # runs served at once (Prized's default quota is 50 running sandboxes)
    max_handoff_attempts: int = 3  # then the work item is stopped, so it doesn't come back forever

    # Clocks.
    probe_every_s: float = 15.0
    sweep_every_s: float = 300.0
    reconcile_every_s: float = 60.0  # webhook mode: a backstop drain, since webhooks can be lost

    # The worker that runs in each sandbox: Anthropic's ant CLI, checked against its sha256.
    ant_version: str = ANT_VERSION
    ant_sha256: Mapping[str, str] = field(default_factory=lambda: dict(ANT_SHA256))
    ant_url: str = ANT_URL
    workdir: str = WORKDIR

    # Prized's free tier: "auto" asks Prized at start, "1" or "0" says. ``free`` is what was decided.
    free_tier: str = "auto"
    free: bool = False
    # On the free tier, when the workspace's disk is full, delete the session sandbox paused longest
    # so a new session can start (the alternative is the new session failing).
    evict_paused: bool = True
    # The settings named in the environment, which the free tier's defaults leave alone.
    explicit: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"env_[A-Za-z0-9]+", self.environment_id or ""):
            raise ConfigError("ANTHROPIC_ENVIRONMENT_ID must look like env_…")
        if not self.environment_key:
            raise ConfigError("ANTHROPIC_ENVIRONMENT_KEY is empty")
        if not re.fullmatch(r"[0-9A-Za-z.+-]+", self.ant_version):
            raise ConfigError(f"ANT_VERSION={self.ant_version!r} is not a version")
        for arch, digest in self.ant_sha256.items():
            if arch not in ("amd64", "arm64") or not _SHA256.fullmatch(digest):
                raise ConfigError(f"the sha256 for ant on {arch} is not a sha256")
        if self.handoff_timeout_s < 1:
            raise ConfigError("HANDOFF_TIMEOUT must be at least 1s")
        if self.max_handoffs < 1 or self.max_sessions < 1:
            raise ConfigError("MAX_HANDOFFS and MAX_SESSIONS must be at least 1")
        check_base_url(self.base_url)
        if self.free_tier not in ("auto", "1", "0"):
            raise ConfigError(f"FREE_TIER={self.free_tier!r}: use auto, 1 or 0")
        if self.webhook_key:
            from .webhooks import decode_key

            try:
                decode_key(self.webhook_key)
            except ValueError as err:
                raise ConfigError(str(err)) from err

    @property
    def webhooks(self) -> bool:
        return bool(self.webhook_key)

    @property
    def reclaim_ms(self) -> int:
        return self.handoff_timeout_s * 2 * 1000

    def on_free_tier(self) -> tuple[Settings, list[str]]:
        """These settings for a workspace on Prized's free tier, and what changed. Each sandbox has
        60 minutes of running in all, so the worker leaves soon after the session goes idle and an
        orchestrator that went away stops spending them sooner. At most 10 sandboxes run at once.
        A run time over 60 minutes would be refused. Settings named in the environment stay, except
        those the free tier refuses."""
        changes: dict[str, object] = {}
        notes: list[str] = []

        def put(name: str, attr: str, value: object, shown: str, *, force: bool = False) -> None:
            if (force or name not in self.explicit) and getattr(self, attr) != value:
                notes.append(f"{name} {shown}")
                changes[attr] = value

        if self.max_sessions > FREE_SANDBOXES:
            put("MAX_SESSIONS", "max_sessions", FREE_SANDBOXES, f"{FREE_SANDBOXES}", force=True)
        if self.max_idle_s > FREE_MAX_IDLE_S:
            put("MAX_IDLE", "max_idle_s", FREE_MAX_IDLE_S, f"{FREE_MAX_IDLE_S}s")
        # Probes keep a served sandbox awake, so they must come well within AUTO_PAUSE.
        if self.auto_pause_s > FREE_AUTO_PAUSE_S and self.probe_every_s * 4 <= FREE_AUTO_PAUSE_S:
            put("AUTO_PAUSE", "auto_pause_s", FREE_AUTO_PAUSE_S, f"{FREE_AUTO_PAUSE_S // 60}m")
        if self.max_runtime_s is not None and self.max_runtime_s > FREE_RUN_S:
            put("MAX_RUNTIME", "max_runtime_s", None, "left to Prized (60 minutes in all)", force=True)
        return replace(self, free=True, **changes), notes  # type: ignore[arg-type]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)

        def get(name: str) -> str | None:
            v = e.get(name)
            return v if v not in (None, "") else None

        def dur(name: str, default: int, unit: str = "s") -> int:
            v = get(name)
            return default if v is None else seconds(v, name=name, unit=unit)

        def num(name: str, kind: type, default: object = None) -> object:
            v = get(name)
            if v is None:
                return default
            try:
                return kind(v)
            except ValueError as err:
                raise ConfigError(f"{name}={v!r} is not a number") from err

        missing = [n for n in ("ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_ENVIRONMENT_KEY") if not get(n)]
        if missing:
            raise ConfigError(
                f"{' and '.join(missing)} not set: create a self-hosted environment in the Claude Console "
                "and generate its environment key"
            )
        sha = dict(ANT_SHA256)
        version = get("ANT_VERSION") or ANT_VERSION
        if version != ANT_VERSION:
            sha = {}
        for arch in ("amd64", "arm64"):
            v = get(f"ANT_SHA256_{arch.upper()}")
            if v:
                sha[arch] = v
        if not sha:
            raise ConfigError(
                f"ANT_VERSION={version}: set ANT_SHA256_AMD64 (and ANT_SHA256_ARM64) from that release's checksums file"
            )
        max_runtime = get("MAX_RUNTIME")
        base_url = check_base_url(get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        other = get("ANTHROPIC_BASE_URL")
        tunables = (
            "KEEP_PAUSED",
            "MAX_RUNTIME",
            "AUTO_PAUSE",
            "MAX_IDLE",
            "MAX_SESSIONS",
            "WARM_PARENT_KEEP",
        )
        return cls(
            environment_id=get("ANTHROPIC_ENVIRONMENT_ID") or "",
            environment_key=get("ANTHROPIC_ENVIRONMENT_KEY") or "",
            base_url=base_url,
            ignored_base_url=other.rstrip("/") if other and other.rstrip("/") != base_url else None,
            free_tier=(get("FREE_TIER") or "auto").lower(),
            evict_paused=(get("EVICT_PAUSED") or "1") not in ("0", "false", "no"),
            explicit=frozenset(n for n in tunables if get(n)),
            warm_parent_keep_s=dur("WARM_PARENT_KEEP", 0),
            webhook_key=get("ANTHROPIC_WEBHOOK_SIGNING_KEY"),
            previous_environment_key=get("ANTHROPIC_ENVIRONMENT_KEY_PREVIOUS"),
            template=get("PRIZED_TEMPLATE") or "prized-base-v1",
            vcpu=num("PRIZED_VCPU", int),  # type: ignore[arg-type]
            memory_gb=num("PRIZED_MEMORY_GB", float),  # type: ignore[arg-type]
            disk_gb=num("PRIZED_DISK_GB", int),  # type: ignore[arg-type]
            warm_parent=get("WARM_PARENT"),
            labels=label_pairs(get("PRIZED_LABELS") or ""),
            keep_paused_s=dur("KEEP_PAUSED", 7 * 86400),
            max_runtime_s=seconds(max_runtime, name="MAX_RUNTIME") if max_runtime else None,
            auto_pause_s=dur("AUTO_PAUSE", 15 * 60),
            session_overrides=(get("SESSION_OVERRIDES") or "1") not in ("0", "false", "no"),
            max_idle_s=dur("MAX_IDLE", 60),
            run_timeout_s=dur("RUN_TIMEOUT", 6 * 3600),
            handoff_timeout_s=dur("HANDOFF_TIMEOUT", 120),
            max_handoffs=num("MAX_HANDOFFS", int, 8),  # type: ignore[arg-type]
            max_sessions=num("MAX_SESSIONS", int, 50),  # type: ignore[arg-type]
            reconcile_every_s=float(dur("RECONCILE_EVERY", 60)),
            probe_every_s=float(dur("PROBE_EVERY", 15)),
            sweep_every_s=float(dur("SWEEP_EVERY", 300)),
            ant_version=version,
            ant_sha256=sha,
            ant_url=get("ANT_URL") or ANT_URL,
        )
