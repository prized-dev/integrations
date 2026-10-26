"""Prized Sandboxes execution environment (standalone Hermes plugin).

Runs Hermes terminal, file and ``execute_code`` commands in a Prized sandbox: a
Firecracker microVM (Ubuntu 24.04, user ``prized`` without sudo, home
``/home/prized``) driven through the Prized HTTP API
(https://prized.dev/docs/sandboxes-api). It uses httpx, which Hermes already
ships, and no vendor SDK.

Persistence (``terminal.container_persistent: true``, the default) is pause and
resume, not snapshots. When Hermes releases the environment (the idle reaper,
session end, exit) the sandbox is PAUSED: its memory and disk are kept, so
background processes (a dev server, a watcher, a REPL) freeze and carry on when
the next command arrives. The sandbox id is stored per task under HERMES_HOME,
so a later Hermes process resumes the same live machine.

Limits that shape this module:

- A sandbox is deleted once its run time (``maxRuntimeMin``) is used up: 24 h of
  running by default, up to 7 days; each resume restarts the clock; 60 min in all
  on the free tier. When the stored sandbox is gone or past its
  ``expiresAt``, the environment creates a fresh one and flags the result
  ``environment_recreated`` so the model knows processes and unsynced files are
  gone.
- Exec returns its output when the command ends (no streaming) and has no
  stdin, so stdin is staged as a file through ``/files/upload``.
- The file API reaches only ``/home/prized``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shlex
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from hermes_constants import get_hermes_home
from tools.environments.base import BaseEnvironment
from tools.environments.file_sync import FileSyncManager, iter_sync_files, quoted_rm_command

try:
    from tools.environments.base_output import _ThreadedProcessHandle
except ModuleNotFoundError as exc:  # Hermes before the September 2026 module split
    if exc.name != "tools.environments.base_output":
        raise
    from tools.environments.base import _ThreadedProcessHandle

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_API_URL", "DEFAULT_CWD", "PrizedAPIError", "PrizedClient", "PrizedEnvironment",
    "REMOTE_HOME", "resolve_settings",
]

DEFAULT_API_URL = "https://api.prized.dev/api/v1"
DEFAULT_TEMPLATE = "prized-base-v1"
#: 2 vCPU / 4 GB: enough for agent work, and inside the free tier's per-sandbox cap.
DEFAULT_VCPU = 2
DEFAULT_MEMORY_MB = 4096
#: Pause after this many idle minutes even if Hermes never releases the sandbox
#: (a crashed host), so a forgotten sandbox stops paying for compute.
DEFAULT_AUTO_PAUSE_MIN = 10

REMOTE_HOME = "/home/prized"
DEFAULT_CWD = f"{REMOTE_HOME}/work"
_REMOTE_SCRATCH = f"{REMOTE_HOME}/.cache/hermes"

_STORE_NAME = "prized_sandboxes.json"
_USER_AGENT = "hermes-plugin-prized/0.1.0"

#: States from which a sandbox never runs again.
_GONE_STATES = frozenset({"deleting", "deleted", "error"})
_PAUSED_STATES = frozenset({"paused", "pausing"})

#: The edge holds an exec at most this long (seconds).
_MAX_EXEC_TIMEOUT = 3600
#: Extra seconds the edge deadline gets past Hermes' own deadline; Hermes
#: enforces the command timeout itself and cancels through ``_cancel``.
_EXEC_TIMEOUT_SLACK = 30
#: How long to keep retrying answers where nothing ran (503 not_ready, 429).
_NOT_RUN_RETRY_S = 30.0
#: How long a fresh or resumed sandbox may answer 502/503/409 to the first probe.
_READY_PROBE_S = 20.0
_CONTROL_RETRIES = 3
#: One upload or download through the file API is at most 100 MiB.
_MAX_TRANSFER_BYTES = 100 * 1024 * 1024
#: Exec answers where the command certainly did not run.
_NOT_RUN = frozenset({429, 503})

# Bounds from the create route (https://prized.dev/docs/sandboxes-api#create).
_VCPU_RANGE = (1, 32)
_MEMORY_MB_RANGE = (1024, 131072)
_TIMER_MIN_RANGE = (0, 40320)
_MAX_RUNTIME_MIN = 10080  # 7 days, the API maximum for maxRuntimeMin
_MAX_LABELS = 32
#: ``instance``: every persistent task id on a Hermes home shares one sandbox (one machine
#: per personal agent; also keeps a free-tier workspace well inside its 10-sandbox limit).
#: ``session``: one sandbox per task id.
_SCOPES = ("instance", "session")
_INSTANCE_KEY = "instance"


#: Added to a 401 (the key is missing or refused): an agent can make its person an account and a key.
_SIGN_UP_HINT = ("No Prized account yet? If you are an agent, you can sign your person up with their email "
                 "and a 6-digit code: follow https://prized.dev/auth.md?via=hermes")


class PrizedAPIError(RuntimeError):
    """A Prized API answer that is not success. ``code`` is the API's ``error`` field."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _error_of(resp: httpx.Response) -> tuple[str | None, str]:
    """``(error code, human detail)`` from an API error document; never echoes headers."""
    try:
        doc = resp.json()
    except ValueError:
        return None, (resp.text or "")[:200]
    if not isinstance(doc, dict):
        return None, ""
    code = doc.get("error") if isinstance(doc.get("error"), str) else None
    detail = " ".join(str(doc[k]) for k in ("reason", "field", "message", "detail") if doc.get(k))
    return code, detail


def _api_error(resp: httpx.Response, what: str) -> PrizedAPIError:
    code, detail = _error_of(resp)
    text = f"Prized {what} failed: HTTP {resp.status_code}"
    if code:
        text += f" {code}"
    if detail:
        text += f" ({detail})"
    if resp.status_code == 401:
        text += f". {_SIGN_UP_HINT}"
    return PrizedAPIError(text, status=resp.status_code, code=code)


def _retry_after(resp: httpx.Response, default: float = 0.5) -> float:
    try:
        return min(max(float(resp.headers.get("Retry-After", default)), 0.1), 5.0)
    except ValueError:
        return default


class PrizedClient:
    """The few Prized routes this backend needs: the control plane and one sandbox's edge.

    Control-plane calls retry network errors, 429 and 5xx (a create carries an
    ``Idempotency-Key``, so its retry can never make a second sandbox). Exec is
    never retried here: the caller decides, because a lost answer may mean the
    command already ran.
    """

    def __init__(self, api_key: str, base_url: str = DEFAULT_API_URL, *,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def _control(self, method: str, url: str, what: str, *, ok=(200, 201, 202, 204),
                 allow_404: bool = False, **kwargs) -> httpx.Response | None:
        last_exc: Exception | None = None
        for attempt in range(1, _CONTROL_RETRIES + 1):
            try:
                resp = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == _CONTROL_RETRIES:
                    raise PrizedAPIError(f"Prized {what} failed: {exc}") from exc
                self._sleep(0.5 * attempt)
                continue
            if resp.status_code in ok:
                return resp
            if resp.status_code == 404 and allow_404:
                return None
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < _CONTROL_RETRIES:
                self._sleep(_retry_after(resp, 0.5 * attempt))
                continue
            raise _api_error(resp, what)
        raise PrizedAPIError(f"Prized {what} failed: {last_exc}")  # pragma: no cover

    # --- control plane ---
    def create(self, body: dict, idempotency_key: str) -> dict:
        resp = self._control("POST", "/sandboxes", "sandbox create", params={"wait": "running"},
                             json=body, headers={"Idempotency-Key": idempotency_key},
                             timeout=httpx.Timeout(60.0, connect=10.0))
        return resp.json()["sandbox"]

    def get(self, sandbox_id: str) -> dict | None:
        resp = self._control("GET", f"/sandboxes/{sandbox_id}", "sandbox read", allow_404=True)
        return None if resp is None else resp.json()["sandbox"]

    def resume(self, sandbox_id: str) -> dict:
        resp = self._control("POST", f"/sandboxes/{sandbox_id}/resume", "sandbox resume",
                             params={"wait": "running"})
        return resp.json()["sandbox"]

    def pause(self, sandbox_id: str, *, wait: bool = False) -> dict | None:
        resp = self._control("POST", f"/sandboxes/{sandbox_id}/pause", "sandbox pause",
                             params={"wait": "paused"} if wait else None, allow_404=True)
        return None if resp is None else resp.json().get("sandbox")

    def delete(self, sandbox_id: str) -> None:
        self._control("DELETE", f"/sandboxes/{sandbox_id}", "sandbox delete", allow_404=True)

    # --- edge ---
    def execute(self, toolbox: str, command: str, *, timeout: float) -> httpx.Response:
        """One ``POST /process/execute``; the raw answer, whatever its status."""
        edge_timeout = int(min(max(timeout, 1), _MAX_EXEC_TIMEOUT))
        return self._http.post(f"{toolbox}/process/execute", json={"command": command, "timeout": edge_timeout},
                               timeout=httpx.Timeout(edge_timeout + 30.0, connect=10.0))

    def upload(self, toolbox: str, path: str, content: bytes | BinaryIO) -> None:
        """Write one file (whole or not at all); retried, since an upload is idempotent."""
        for attempt in range(1, _CONTROL_RETRIES + 1):
            if hasattr(content, "seek"):
                content.seek(0)
            try:
                resp = self._http.post(f"{toolbox}/files/upload", params={"path": path}, content=content,
                                       timeout=httpx.Timeout(600.0, connect=10.0))
            except httpx.TransportError as exc:
                if attempt == _CONTROL_RETRIES:
                    raise PrizedAPIError(f"Prized upload of {path} failed: {exc}") from exc
                self._sleep(0.5 * attempt)
                continue
            if resp.status_code == 200:
                return
            if resp.status_code in (429, 502, 503) and attempt < _CONTROL_RETRIES:
                self._sleep(_retry_after(resp, 0.5 * attempt))
                continue
            raise _api_error(resp, f"upload of {path}")

    def download(self, toolbox: str, path: str, dest: Path) -> None:
        with self._http.stream("GET", f"{toolbox}/files/download", params={"path": path},
                               timeout=httpx.Timeout(600.0, connect=10.0)) as resp:
            if resp.status_code != 200:
                resp.read()
                raise _api_error(resp, f"download of {path}")
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)


# ---------------------------------------------------------------------------
# Settings (plugins.entries.<id>.settings in config.yaml)
# ---------------------------------------------------------------------------

def _int_setting(key: str, value, bounds: tuple[int, int]) -> int:
    lo, hi = bounds
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Prized plugin setting {key} must be a whole number, got {value!r}")
    if not lo <= value <= hi:
        raise ValueError(f"Prized plugin setting {key} {value} is out of range ({lo} to {hi})")
    return value


def resolve_settings(raw: dict | None) -> dict:
    """Validate plugin settings into create parameters; an invalid value raises ``ValueError``.

    ``None`` or ``""`` means unset. A typo must not create a different machine
    than the one configured, so nothing falls back silently.
    """
    raw = raw or {}
    unset = (None, "")
    out: dict[str, Any] = {
        "template": DEFAULT_TEMPLATE, "vcpu": DEFAULT_VCPU, "memory_mb": DEFAULT_MEMORY_MB,
        "auto_pause_min": DEFAULT_AUTO_PAUSE_MIN, "max_runtime_min": None, "labels": {},
        "api_url": DEFAULT_API_URL, "scope": "instance",
    }
    template = raw.get("template")
    if template not in unset:
        if not isinstance(template, str) or not template.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Prized plugin setting template {template!r} is not a template id")
        out["template"] = template
    for key, bounds in (("vcpu", _VCPU_RANGE), ("memory_mb", _MEMORY_MB_RANGE),
                        ("auto_pause_min", _TIMER_MIN_RANGE), ("max_runtime_min", (1, _MAX_RUNTIME_MIN))):
        if raw.get(key) not in unset:
            out[key] = _int_setting(key, raw[key], bounds)
    labels = raw.get("labels")
    if labels not in (None, "", {}):
        if not isinstance(labels, dict) or len(labels) > _MAX_LABELS - 2 or not all(
                isinstance(k, str) and isinstance(v, str) and 0 < len(k) <= 64 and len(v) <= 256
                for k, v in labels.items()):
            raise ValueError("Prized plugin setting labels must be up to 30 string pairs "
                             "(keys up to 64 characters, values up to 256)")
        if any(k.startswith("hermes") for k in labels):
            raise ValueError("Prized plugin setting labels must not use keys starting with 'hermes'")
        out["labels"] = dict(labels)
    scope = raw.get("scope")
    if scope not in unset:
        if scope not in _SCOPES:
            raise ValueError(f"Prized plugin setting scope {scope!r} is invalid: expected instance or session")
        out["scope"] = scope
    api_url = raw.get("api_url")
    if api_url not in unset:
        if not isinstance(api_url, str) or not api_url.startswith("https://"):
            raise ValueError(f"Prized plugin setting api_url must be an https:// URL, got {api_url!r}")
        out["api_url"] = api_url.rstrip("/")
    return out


# ---------------------------------------------------------------------------
# Sandbox ids stored per task under HERMES_HOME
# ---------------------------------------------------------------------------

def _store_path() -> Path:
    return get_hermes_home() / _STORE_NAME


def _load_store() -> dict:
    """The task -> sandbox map; a missing or damaged file reads as empty."""
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update_store(update) -> None:
    """Read-modify-write the map under an exclusive lock, replacing the file atomically.

    Gateway sessions and delegate children release environments concurrently;
    an unlocked update would let one drop the other's entry.
    """
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a+") as lock:
        with contextlib.suppress(ImportError, OSError):
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load_store()
        before = dict(data)
        update(data)
        if data != before:
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)


def _home_digest() -> str:
    """Short identity of this Hermes home (profile), stamped on every sandbox it creates."""
    return hashlib.sha256(str(get_hermes_home().resolve()).encode("utf-8")).hexdigest()[:12]


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _exec_script(cmd_string: str, *, login: bool, pid_path: str) -> str:
    """The script the edge runs for one Hermes command.

    The edge starts every exec in its own process group, so ``$$`` (recorded in
    ``pid_path``) names the group ``_cancel`` kills. Output goes through a file
    rather than the edge's pipe: a background child the command starts (Hermes'
    own ``nohup ... &`` launcher) would otherwise hold the pipe open and the edge
    would wait out its 5 s grace before answering. ``2>&1`` keeps the merged,
    interleaved stream Hermes expects.
    """
    q = shlex.quote
    out = f"{pid_path}.out"
    shell = "bash -l -c" if login else "bash -c"
    return (f"echo $$ > {q(pid_path)}\n"
            f"{shell} {q(cmd_string)} > {q(out)} 2>&1 < /dev/null\n"
            "__hermes_rc=$?\n"
            f"cat {q(out)}\n"
            f"rm -f {q(out)} {q(pid_path)}\n"
            "exit $__hermes_rc\n")


def _cancel_script(pid_path: str) -> str:
    q = shlex.quote(pid_path)
    return (f"for _ in $(seq 20); do [ -s {q} ] && break; sleep 0.1; done\n"
            f"p=$(cat {q} 2>/dev/null) || exit 0\n"
            '[ -n "$p" ] || exit 0\n'
            'kill -TERM -- "-$p" 2>/dev/null\n'
            "sleep 1\n"
            'kill -KILL -- "-$p" 2>/dev/null\n'
            f"rm -f {q} {q}.out\n"
            "exit 0\n")


#: Environments in this process attached to each sandbox id, and one sync lock per
#: sandbox: in instance scope several sessions share a sandbox, and pausing it while
#: another session's command runs would freeze that command.
_ATTACHED: dict[str, int] = {}
_SYNC_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


def _hold(sandbox_id: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        _ATTACHED[sandbox_id] = _ATTACHED.get(sandbox_id, 0) + 1
        return _SYNC_LOCKS.setdefault(sandbox_id, threading.Lock())


def _release(sandbox_id: str) -> bool:
    """Drop one hold; True when no other environment in this process still holds the sandbox."""
    with _REGISTRY_LOCK:
        left = _ATTACHED.get(sandbox_id, 1) - 1
        if left > 0:
            _ATTACHED[sandbox_id] = left
            return False
        _ATTACHED.pop(sandbox_id, None)
        _SYNC_LOCKS.pop(sandbox_id, None)
        return True


class _SharedSyncManager(FileSyncManager):
    """File sync serialised per sandbox, not per environment, so sessions sharing one
    sandbox never upload an older copy of a file over a newer one."""

    def __init__(self, *args, shared_lock: threading.Lock, **kwargs):
        super().__init__(*args, **kwargs)
        self._shared_lock = shared_lock

    def sync(self, *, force: bool = False) -> None:
        with self._shared_lock:
            super().sync(force=force)


class PrizedEnvironment(BaseEnvironment):
    """Prized sandbox backend: spawn-per-call over ``/process/execute``, paused between turns."""

    _stdin_mode = "payload"

    def __init__(self, *, api_key: str, cwd: str = DEFAULT_CWD, timeout: int = 60,
                 persistent_filesystem: bool = True, task_id: str = "default",
                 settings: dict | None = None, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep, clock=time.time):
        self._settings = resolve_settings(settings)
        if cwd in ("", "~", "/root"):
            cwd = DEFAULT_CWD if cwd != "~" else REMOTE_HOME
        super().__init__(cwd=cwd, timeout=timeout)
        self._client = PrizedClient(api_key, self._settings["api_url"], transport=transport, sleep=sleep)
        self._sleep, self._clock = sleep, clock
        self._persistent, self._task_id = persistent_filesystem, task_id
        self._lock = threading.Lock()
        self._sandbox: dict | None = None
        self._expires_at: float | None = None
        self._gone = False
        self._sync_manager: FileSyncManager | None = None
        self._attach()
        self.init_session()

    # --- identity and attach ---
    @property
    def sandbox_id(self) -> str | None:
        return self._sandbox["id"] if self._sandbox else None

    @property
    def _task_label(self) -> str:
        """The store key and ``hermes_task`` label: one per home in instance scope, else the task id."""
        if self._persistent and self._settings["scope"] == "instance":
            return _INSTANCE_KEY
        return (self._task_id or "default")[:256]

    def _labels(self) -> dict:
        labels = {**self._settings["labels"], "hermes": "1", "hermes_home": _home_digest(),
                  "hermes_task": self._task_label}
        if not self._persistent:
            labels["hermes_ephemeral"] = "1"
        return labels

    def _create(self) -> dict:
        s = self._settings
        body: dict[str, Any] = {"template": s["template"], "labels": self._labels(),
                                "resources": {"vcpu": s["vcpu"], "memoryMb": s["memory_mb"]}}
        if s["auto_pause_min"]:
            body["autoPauseMin"] = s["auto_pause_min"]
        if s["max_runtime_min"]:
            body["maxRuntimeMin"] = s["max_runtime_min"]
        sandbox = self._client.create(body, idempotency_key=f"hermes-{uuid.uuid4().hex}")
        if sandbox.get("state") != "running":
            sandbox = self._wait_running(sandbox["id"])
        logger.info("Prized: created sandbox %s for task %s (expires %s)", sandbox["id"], self._task_id,
                    sandbox.get("expiresAt"))
        return sandbox

    def _wait_running(self, sandbox_id: str, deadline_s: float = 120.0) -> dict:
        """Poll until ``running``; a large or cold create can outlast the API's 8 s hold."""
        end = time.monotonic() + deadline_s
        while True:
            sandbox = self._client.get(sandbox_id)
            if sandbox is None or sandbox["state"] in _GONE_STATES:
                reason = (sandbox or {}).get("errorReason") or "deleted"
                raise PrizedAPIError(f"Prized sandbox {sandbox_id} did not start: {reason}")
            if sandbox["state"] == "running":
                return sandbox
            if sandbox["state"] in _PAUSED_STATES:
                return self._client.resume(sandbox_id)
            if time.monotonic() > end:
                raise PrizedAPIError(f"Prized sandbox {sandbox_id} is still {sandbox['state']} after "
                                     f"{int(deadline_s)}s; retry the command")
            self._sleep(0.5)

    def _usable(self, sandbox: dict | None) -> bool:
        """A stored sandbox is adopted only if it lives, is ours, and is not past its deletion time."""
        if not sandbox or sandbox.get("state") in _GONE_STATES:
            return False
        labels = sandbox.get("labels") or {}
        if labels.get("hermes_home") != _home_digest() or labels.get("hermes_task") != self._task_label:
            logger.warning("Prized: stored sandbox %s belongs to another Hermes home or task; not adopting",
                           sandbox.get("id"))
            return False
        expires = _parse_ts(sandbox.get("expiresAt"))
        return expires is None or expires > self._clock() + 60

    def _resume_stored(self) -> dict | None:
        """The stored sandbox for this task, resumed, or None (and the stale id dropped)."""
        record = _load_store().get(self._task_label)
        sandbox_id = record.get("id") if isinstance(record, dict) else None
        if not isinstance(sandbox_id, str) or not sandbox_id:
            return None
        sandbox = self._client.get(sandbox_id)
        if not self._usable(sandbox):
            logger.info("Prized: stored sandbox %s is gone or expiring; starting a fresh one", sandbox_id)
            self._forget(sandbox_id)
            # The first command's result says so: processes and files of the old sandbox are gone.
            self._mark_recreated()
            return None
        if sandbox["state"] != "running":
            sandbox = self._wait_running(sandbox_id)
        logger.info("Prized: resumed sandbox %s for task %s", sandbox_id, self._task_id)
        return sandbox

    def _remember(self, sandbox_id: str) -> None:
        if self._persistent:
            _update_store(lambda d: d.update({self._task_label: {"id": sandbox_id}}))

    def _forget(self, sandbox_id: str) -> None:
        def update(d: dict) -> None:
            rec = d.get(self._task_label)
            if isinstance(rec, dict) and rec.get("id") == sandbox_id:
                d.pop(self._task_label)
        _update_store(update)

    def _attach(self) -> None:
        """Adopt the stored sandbox for this task (persistent mode) or create one, then wire file sync."""
        sandbox = self._resume_stored() if self._persistent else None
        created = sandbox is None
        if created:
            sandbox = self._create()
            self._remember(sandbox["id"])
        self._sandbox, self._gone = sandbox, False
        self._expires_at = _parse_ts(sandbox.get("expiresAt"))
        try:
            self._probe_ready()
        except Exception:
            if created and not self._persistent:  # nothing will ever adopt it
                with contextlib.suppress(Exception):
                    self._client.delete(sandbox["id"])
            raise
        self._sync_manager = _SharedSyncManager(
            get_files_fn=lambda: iter_sync_files(f"{REMOTE_HOME}/.hermes"),
            upload_fn=self._upload_one, delete_fn=self._delete_remote,
            bulk_upload_fn=self._bulk_upload, bulk_download_fn=self._bulk_download,
            shared_lock=_hold(sandbox["id"]))
        self._sync_manager.sync(force=True)

    def _probe_ready(self) -> None:
        """Send a harmless command until the edge answers 200 (its tunnel can lag ``running``)."""
        toolbox, end = self._toolbox(), time.monotonic() + _READY_PROBE_S
        setup = f"mkdir -p {shlex.quote(DEFAULT_CWD)} && mkdir -p -m 700 {shlex.quote(_REMOTE_SCRATCH)}"
        while True:
            try:
                resp = self._client.execute(toolbox, setup, timeout=30)
            except httpx.TransportError:
                resp = None
            if resp is not None and resp.status_code == 200:
                return
            if time.monotonic() > end:
                if resp is None:
                    raise PrizedAPIError("Prized sandbox did not answer its first command")
                raise _api_error(resp, "first command")
            self._sleep(_retry_after(resp, 0.25) if resp is not None else 0.25)

    def _toolbox(self) -> str:
        if self._sandbox is None:
            raise RuntimeError("Prized sandbox is not attached")
        return self._sandbox["toolboxBase"]

    def _expired(self) -> bool:
        return self._gone or (self._expires_at is not None and self._clock() >= self._expires_at)

    def _before_execute(self) -> None:
        recreated = False
        with self._lock:
            if self._sandbox is not None and self._expired():
                old = self.sandbox_id
                logger.warning("Prized: sandbox %s is gone or past its maximum lifetime; starting a fresh one", old)
                self._forget(old)
                _release(old)
                self._attach()
                recreated = True
            sync_manager = self._sync_manager
        if recreated:
            # Outside the lock: the snapshot bootstrap runs through _run_bash, whose worker takes it.
            self._mark_recreated()
            self.init_session()
        elif sync_manager is not None:
            sync_manager.sync()

    # --- exec ---
    def _staged_stdin_path(self) -> str:
        # The file API reaches /home/prized only, so stdin is staged there, not in /tmp.
        return f"{_REMOTE_SCRATCH}/stdin-{uuid.uuid4().hex}"

    def _execute_once(self, toolbox: str, script: str, timeout: float) -> tuple[str, int]:
        """Run ``script``; retry only answers where nothing ran. Returns ``(output, exit code)``."""
        end = time.monotonic() + _NOT_RUN_RETRY_S
        while True:
            try:
                resp = self._client.execute(toolbox, script, timeout=timeout + _EXEC_TIMEOUT_SLACK)
            except httpx.TransportError as exc:
                return (f"[Prized: lost the connection to the sandbox ({exc}); the command may or may "
                        "not have run]\n", 1)
            if resp.status_code == 200:
                doc = resp.json()
                output = (doc.get("stdout") or "") + (doc.get("stderr") or "")
                if doc.get("timedOut"):
                    return output + "\n[Prized: the sandbox ended the command at its time limit]\n", 124
                return output, int(doc.get("exitCode", 1))
            code, detail = _error_of(resp)
            if resp.status_code in _NOT_RUN and time.monotonic() < end:
                self._sleep(_retry_after(resp))
                continue
            if resp.status_code == 404 or code == "sandbox_not_running":
                # Deleted (lifetime cap, a manual delete) or failed: the next command starts fresh.
                self._gone = True
                return ("[Prized: the sandbox is gone (deleted at its maximum lifetime, or failed); "
                        "nothing ran. The next command starts a fresh sandbox]\n", 1)
            # Only 502 (the sandbox did not answer) leaves it open whether the command ran.
            ran = "the command may or may not have run" if resp.status_code == 502 else "nothing ran"
            what = " ".join(x for x in (str(resp.status_code), code, f"({detail})" if detail else "") if x)
            return f"[Prized: HTTP {what}; {ran}]\n", 1

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None):
        toolbox, client, lock = self._toolbox(), self._client, self._lock
        pid_path = f"/tmp/hermes-prized-{uuid.uuid4().hex}"  # no-tmp: ok — sandbox-side path
        # Guarded by ``lock`` so cancel() and dispatch agree on whether the shell
        # has taken ownership of (opened + unlinked) the staged stdin file.
        state = {"cancelled": False, "staged": None, "dispatched": False}

        def scrub_staged() -> None:  # caller holds ``lock``
            if state["staged"] and not state["dispatched"]:
                with contextlib.suppress(Exception):
                    client.upload(toolbox, state["staged"], b"")
                state["staged"] = None

        def cancel() -> None:
            with lock:
                state["cancelled"] = True
                scrub_staged()
                dispatched = state["dispatched"]
            if dispatched:
                with contextlib.suppress(Exception):
                    client.execute(toolbox, _cancel_script(pid_path), timeout=15)

        def exec_fn() -> tuple[str, int]:
            command = cmd_string
            if stdin_data:  # empty stdin == no stdin, as on base
                with lock:
                    if state["cancelled"]:
                        return ("", 130)
                remote_stdin = self._staged_stdin_path()
                try:
                    client.upload(toolbox, remote_stdin, stdin_data.encode("utf-8", "surrogateescape"))
                except PrizedAPIError as exc:
                    return (f"[Prized: could not stage stdin: {exc}]\n", 1)
                with lock:
                    state["staged"] = remote_stdin
                command = self._redirect_stdin_from_file(cmd_string, remote_stdin)
            with lock:
                if state["cancelled"]:
                    scrub_staged()
                    return ("", 130)
                state["dispatched"] = True
            return self._execute_once(toolbox, _exec_script(command, login=login, pid_path=pid_path), timeout)

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    # --- file sync transport ---
    def _run_checked(self, script: str, label: str, timeout: int = 300) -> None:
        output, code = self._execute_once(self._toolbox(), script, timeout)
        if code != 0:
            raise RuntimeError(f"Prized {label} failed: {output.strip()[:500]}")

    def _upload_one(self, host_path: str, remote_path: str) -> None:
        with open(host_path, "rb") as fh:
            self._client.upload(self._toolbox(), remote_path, fh)

    def _bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """One tar through the file API and one exec to unpack it, instead of a request per file."""
        if not files:
            return
        if any(not remote.startswith(f"{REMOTE_HOME}/") for _, remote in files):
            raise RuntimeError(f"Prized can only sync files under {REMOTE_HOME}")
        with tempfile.TemporaryFile() as tmp:
            with tarfile.open(fileobj=tmp, mode="w:gz") as tar:
                for host_path, remote_path in files:
                    tar.add(host_path, arcname=remote_path.lstrip("/"), recursive=False)
            if tmp.tell() > _MAX_TRANSFER_BYTES:
                for host_path, remote_path in files:
                    self._upload_one(host_path, remote_path)
                return
            remote_tar = f"{_REMOTE_SCRATCH}/sync-{uuid.uuid4().hex}.tgz"
            self._client.upload(self._toolbox(), remote_tar, tmp)
        q = shlex.quote(remote_tar)
        self._run_checked(f"tar xzf {q} -C / --no-same-owner --no-overwrite-dir; rc=$?; rm -f {q}; exit $rc",
                          "bulk upload")

    def _delete_remote(self, remote_paths: list[str]) -> None:
        if remote_paths:
            self._run_checked(quoted_rm_command(remote_paths), "delete")

    def _bulk_download(self, dest_tar_path: Path) -> None:
        remote_tar = f"{_REMOTE_SCRATCH}/sync-back-{uuid.uuid4().hex}.tar"
        q = shlex.quote(remote_tar)
        member = shlex.quote(f"{REMOTE_HOME}/.hermes".lstrip("/"))
        try:
            # --exclude: live sockets cannot be archived and must not fail the download.
            self._run_checked(f"tar cf {q} --exclude='*.sock' -C / {member}", "bulk download")
            self._client.download(self._toolbox(), remote_tar, Path(dest_tar_path))
        finally:
            with contextlib.suppress(Exception):
                self._client.execute(self._toolbox(), f"rm -f {q}", timeout=30)

    # --- teardown ---
    def cleanup(self):
        with self._lock:
            sandbox, sync_manager = self._sandbox, self._sync_manager
            self._sync_manager = None
        if sandbox is None:
            return
        try:
            # Pull back what the agent changed under ~/.hermes while the sandbox is still attached.
            if sync_manager is not None and not self._gone:
                try:
                    sync_manager.sync_back()
                except Exception as exc:
                    logger.warning("Prized: sync_back failed for sandbox %s: %s", sandbox["id"], exc)
            with self._lock:
                self._sandbox = None
            last = _release(sandbox["id"])
            try:
                if not self._persistent:
                    self._client.delete(sandbox["id"])
                    logger.info("Prized: deleted sandbox %s", sandbox["id"])
                elif last and not self._gone:
                    # Memory and disk are kept: processes carry on at the next command.
                    self._client.pause(sandbox["id"])
                    logger.info("Prized: paused sandbox %s (processes and memory kept)", sandbox["id"])
            except Exception as exc:
                logger.warning("Prized: cleanup failed for sandbox %s: %s", sandbox["id"], exc)
        finally:
            self._client.close()
