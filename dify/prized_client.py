"""Calls to the Prized Sandbox API (https://prized.dev/docs/sandboxes-api).

Two hosts take the key: the API at api.prized.dev and each sandbox's command
endpoint (its ``toolboxBase``, on term.prized.dev). The key goes in the
Authorization header to those hosts only, over HTTPS, and never into a
sandbox, a tool result, an error message, or a log line.

The plugin only acts on sandboxes it created: every create carries the label
``integration=dify``, and every other call reads the sandbox first and refuses
one without that label.
"""

from __future__ import annotations

import logging
import math
import mimetypes
import posixpath
import re
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

# httpx logs each request's URL at INFO, file paths included; keep that out of Dify's plugin logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

VERSION = "0.1.0"
API_BASE = "https://api.prized.dev/api/v1"
EDGE_BASE = "https://term.prized.dev"
EDGE_HOST = "term.prized.dev"

LABEL_KEY = "integration"
LABEL_VALUE = "dify"
HOME = "/home/prized"
WORKDIR = "/home/prized/work"
TEMPLATES = ("prized-base-v1", "prized-agent-v1")

# What a tool hands back to Dify. The API keeps 4 MiB per output stream; an
# agent's context and a workflow variable want far less.
OUTPUT_LIMIT = 64 * 1024
READ_TEXT_LIMIT = 256 * 1024
READ_FILE_LIMIT = 15 * 1024 * 1024
WRITE_LIMIT = 5 * 1024 * 1024
UPLOAD_LIMIT = 50 * 1024 * 1024
CODE_LIMIT = 512 * 1024
COMMAND_LIMIT = 256 * 1024
# A command's answer comes when it ends, on one open connection; past about
# four minutes an idle connection is not reliable. Longer jobs go in the
# background (nohup) and are read with later commands.
MAX_COMMAND_TIMEOUT = 240
DEFAULT_COMMAND_TIMEOUT = 60
CREATE_WAIT_SECONDS = 150

MAX_ATTEMPTS = 3
RETRY_AFTER_CAP = 10.0
REQUEST_TIMEOUT = 30.0
FILE_TIMEOUT = 300.0

SANDBOX_ID_RE = re.compile(r"^sbx_[A-Za-z0-9]{4,64}$")
LABEL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
KEY_LIKE_RE = re.compile(r"dcp_[A-Za-z0-9_\-]+")

IDEMPOTENT = "idempotent"  # safe to send again: reads, state changes, keyed creates
EXEC = "exec"  # the sandbox may have acted on it: commands and uploads

NEVER_RETRY = frozenset(
    {
        "sandbox_quota_exceeded",
        "sandboxes_disabled",
        "sandbox_create_disabled",
        "idempotency_conflict",
        "paid_balance_required",
        "insufficient_balance",
    }
)
# Answers that prove a command or upload never reached the sandbox.
EXEC_UNDELIVERED = frozenset(
    {
        "rate_limited",
        "resolve_failed",
        "sandbox_credentials_unavailable",
        "control_plane_unavailable",
        "sandbox_not_ready",
    }
)

# A command or upload that got no clear answer (a lost connection, a timeout, or a 5xx other
# than the ones above) may still have run, so the plugin never sends it again and says so.
ANSWER_LOST = {
    "command": "No clear answer came back from the sandbox, so the command may or may not have run. "
    "Check before running it again.",
    "upload": "No clear answer came back from the sandbox, so the file may or may not have been written.",
}
# A 2xx whose body cannot be read: the sandbox did act on it.
ANSWER_UNREADABLE = {
    "command": "The command ran, but its answer could not be read.",
    "upload": "The file was written, but the answer could not be read.",
}
NO_SANDBOX_ACCESS = (
    "This API key cannot reach sandboxes. A workspace contractor's key cannot; use a key made by "
    "an owner or member, with Sandboxes: Write."
)
NO_SANDBOX_CREATE = (
    "This API key cannot create sandboxes: it is a workspace contractor's key, or sandboxes are "
    "switched off for this workspace (email team@prized.dev)."
)

MESSAGES = {
    "unauthorized": "The Prized API key was refused. Check that it is a live key from prized.dev.",
    "token_expired": "The Prized API key has expired. Make a new one on prized.dev.",
    "key_access_denied": "This API key lacks the permission it needs: use a Custom key with Sandboxes: Write.",
    "workspace_held": "The Prized workspace is on hold, so sandboxes cannot start.",
    "workspace_closing": "The Prized workspace is being closed, so sandboxes cannot start.",
    "not_found": "No such sandbox for this API key, or it was not created by this plugin.",
    "template_not_found": "No such sandbox template.",
    "template_inactive": "That sandbox template is retired; pick another.",
    "paid_balance_required": "Sandboxes need a paid balance on this Prized workspace right now.",
    "insufficient_balance": "The Prized workspace balance is empty. Add funds, then retry.",
    "sandbox_quota_exceeded": "The Prized workspace is at a sandbox quota. Delete sandboxes "
    "you no longer need, then retry.",
    "invalid_resources": "The requested vCPU or memory is outside what the template or plan allows "
    "(the free tier allows 2 vCPU and 4 GB).",
    "invalid_max_runtime": "The run time must be a positive whole number of minutes.",
    "sandbox_capacity": "No sandbox capacity is free right now. Nothing was created; retry shortly.",
    "sandboxes_disabled": "Sandboxes are paused for maintenance. Retry later.",
    "sandbox_create_disabled": "Creating sandboxes is paused for maintenance. Retry later.",
    "sandbox_not_running": "The sandbox is not running and could not be woken (it is resizing, "
    "failed, or being deleted).",
    "sandbox_not_ready": "The sandbox did not finish starting in time. Nothing ran; retry.",
    "sandbox_deleted": "The sandbox was deleted. Create a new one.",
    "sandbox_unreachable": "The sandbox did not answer. Retry shortly.",
    "rate_limited": "Too many requests to the Prized API. Retry shortly.",
    "spawn_failed": "The command could not start. Does the working directory exist?",
    "path_outside_root": "File paths must be inside /home/prized.",
    "symlink_not_allowed": "File transfers do not follow symbolic links.",
    "not_regular_file": "Only regular files can be read.",
    "permission_denied": "Permission denied in the sandbox.",
    "file_too_large": "The file is over the size limit.",
    "body_too_large": "The request is over the size limit.",
    "network_error": "Could not reach the Prized API.",
    "timeout": "The Prized API did not answer in time.",
    "unexpected_redirect": "The Prized API answered with a redirect, which this plugin does not follow.",
    "bad_response": "The Prized API answered with something this plugin could not read.",
}


class InputError(ValueError):
    """A tool input the plugin refuses before calling the API; its text is safe to show."""


class PrizedError(Exception):
    """An error safe to show in Dify: the API's error code with a fixed sentence, or with
    the API's own short message (at most 300 characters, anything shaped like a key removed).

    It never carries the request, its headers, or a non-JSON response body.
    """

    def __init__(self, code: str, message: str | None = None, status: int | None = None):
        self.code = code
        self.status = status
        text = message or MESSAGES.get(code) or "The Prized API refused the request."
        super().__init__(scrub(f"{text} ({code})"))


def scrub(text: str) -> str:
    """Remove anything shaped like a Prized API key."""
    return KEY_LIKE_RE.sub("dcp_[redacted]", text)


# --- input checks ---------------------------------------------------------


def require_text(params: Mapping[str, Any], name: str, limit: int) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{name} is required.")
    if "\0" in value:
        raise InputError(f"{name} must not contain NUL characters.")
    if len(value.encode()) > limit:
        raise InputError(f"{name} is over {limit // 1024} KiB.")
    return value


def optional_text(params: Mapping[str, Any], name: str, limit: int) -> str | None:
    value = params.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return require_text(params, name, limit)


def integer(params: Mapping[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = params.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        raise InputError(f"{name} must be a whole number from {low} to {high}.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise InputError(f"{name} must be a whole number from {low} to {high}.") from None
    if not math.isfinite(number) or number != int(number) or not low <= number <= high:
        raise InputError(f"{name} must be a whole number from {low} to {high}.")
    return int(number)


def choice(params: Mapping[str, Any], name: str, options: tuple[str, ...], default: str) -> str:
    value = params.get(name)
    if value is None or value == "":
        return default
    if value not in options:
        raise InputError(f"{name} must be one of: {', '.join(options)}.")
    return value


def sandbox_id(params: Mapping[str, Any]) -> str:
    value = params.get("sandbox_id")
    value = value.strip() if isinstance(value, str) else value
    if not isinstance(value, str) or not SANDBOX_ID_RE.match(value):
        raise InputError("sandbox_id must be a sandbox id from Create sandbox, such as sbx_3f9c0a1b2d4e5f60.")
    return value


def guest_path(value: str) -> str:
    """A path in the sandbox: relative paths start in /home/prized/work."""
    value = value.strip()
    path = posixpath.normpath(value if value.startswith("/") else posixpath.join(WORKDIR, value))
    if path != HOME and not path.startswith(HOME + "/"):
        raise InputError("Paths must be inside /home/prized (relative paths start in /home/prized/work).")
    return path


def parse_labels(value: str | None) -> dict[str, str]:
    """``team=data, app=report`` -> a dict, at most 8 pairs."""
    labels: dict[str, str] = {}
    if not value or not value.strip():
        return labels
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, val = part.partition("=")
        key, val = key.strip(), val.strip()
        if not sep or not LABEL_KEY_RE.match(key) or not val or len(val) > 256:
            raise InputError("labels must look like key=value, key=value (keys up to 64 characters).")
        if key == LABEL_KEY:
            raise InputError(f"The label {LABEL_KEY} is set by the plugin.")
        labels[key] = val
    if len(labels) > 8:
        raise InputError("At most 8 labels.")
    return labels


def clip(text: str, limit: int = OUTPUT_LIMIT) -> tuple[str, bool]:
    """Keep the start and the end (where errors usually are) of long output."""
    data = text.encode()
    if len(data) <= limit:
        return text, False
    head = data[: limit // 4].decode(errors="ignore")
    tail = data[-(limit - limit // 4) :].decode(errors="ignore")
    dropped = len(data) - len(head.encode()) - len(tail.encode())
    return f"{head}\n[... {dropped} bytes cut ...]\n{tail}", True


def summarize(sandbox: Mapping[str, Any]) -> dict[str, Any]:
    resources = sandbox.get("resources") or {}
    return {
        "sandbox_id": sandbox.get("id"),
        "state": sandbox.get("state"),
        "template": sandbox.get("template"),
        "vcpu": resources.get("vcpu"),
        "memory_mb": resources.get("memoryMb"),
        "disk_gb": resources.get("diskGb"),
        "expires_at": sandbox.get("expiresAt"),
        "auto_pause_minutes": sandbox.get("autoPauseMin"),
        "auto_delete_minutes": sandbox.get("autoDeleteMin"),
        "error_reason": sandbox.get("errorReason"),
        "created_at": sandbox.get("createdAt"),
    }


def edge_url(sandbox: Mapping[str, Any], op: str) -> str:
    """The sandbox's command endpoint: always https://term.prized.dev (default port)
    for this very sandbox. A toolboxBase naming anything else, other prized.dev hosts
    included (customers' shared ports live under prized.dev), is ignored, so the key
    goes nowhere else."""
    path = f"/v1/sandbox/{quote(str(sandbox['id']), safe='')}"
    base = sandbox.get("toolboxBase")
    if isinstance(base, str) and base != f"{EDGE_BASE}{path}":
        try:
            parts = urlsplit(base)
            port = parts.port
        except ValueError:
            parts, port = None, None
        if not (
            parts
            and parts.scheme == "https"
            and (parts.hostname or "").lower() == EDGE_HOST
            and port in (None, 443)
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and parts.path.rstrip("/") == path
        ):
            logging.getLogger(__name__).warning("ignoring an unexpected toolboxBase for %s", sandbox["id"])
    return f"{EDGE_BASE}{path}{op}"


# --- the client -----------------------------------------------------------


class PrizedClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or len(api_key) > 512
            or not api_key.strip().isascii()
            or any(c.isspace() or not c.isprintable() for c in api_key.strip())
        ):
            raise PrizedError("invalid_api_key", "The Prized API key is missing or malformed.")
        self._auth = {"Authorization": f"Bearer {api_key.strip()}"}
        self._sleep = sleep
        self._http = httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": f"prized-dify-plugin/{VERSION}"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PrizedClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # One request. Reads, state changes and keyed creates are retried on transient
    # failures; a command or upload only when the answer proves it never arrived.
    def _send(
        self,
        method: str,
        url: str,
        *,
        mode: str = IDEMPOTENT,
        what: str = "command",
        timeout: float = REQUEST_TIMEOUT,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """``what`` ("command" or "upload") names an exec call in its errors."""
        for attempt in range(MAX_ATTEMPTS):
            last = attempt == MAX_ATTEMPTS - 1
            try:
                response = self._http.request(
                    method, url, headers={**self._auth, **(headers or {})}, timeout=timeout, **kwargs
                )
            except httpx.HTTPError as exc:
                never_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                retryable = never_sent or (
                    mode == IDEMPOTENT
                    and isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))
                )
                if last or not retryable:
                    code = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"
                    if mode == EXEC and not never_sent:
                        raise PrizedError(code, ANSWER_LOST[what]) from None
                    raise PrizedError(code) from None
                self._sleep(_backoff(attempt))
                continue
            if 300 <= response.status_code < 400:
                raise PrizedError("unexpected_redirect", status=response.status_code)
            if response.status_code < 300:
                return response
            error = _error_from(response)
            if mode == EXEC and response.status_code >= 500 and error.code not in EXEC_UNDELIVERED:
                # Past the edge, a 5xx does not say whether the sandbox acted on the request.
                raise PrizedError(error.code, ANSWER_LOST[what], status=response.status_code)
            if last or not _retryable(error, mode, method):
                raise error
            delay = _backoff(attempt)
            retry_after = _retry_after(response)
            if retry_after is not None:
                delay = max(delay, min(retry_after, RETRY_AFTER_CAP))
            self._sleep(delay)
        raise PrizedError("network_error")  # not reached

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise PrizedError("bad_response", status=response.status_code) from None
        if not isinstance(data, dict):
            raise PrizedError("bad_response", status=response.status_code)
        return data

    def _exec_json(self, response: httpx.Response, what: str) -> dict[str, Any]:
        try:
            return self._json(response)
        except PrizedError:
            raise PrizedError("bad_response", ANSWER_UNREADABLE[what], status=response.status_code) from None

    def _sandbox_doc(self, response: httpx.Response) -> dict[str, Any]:
        sandbox = self._json(response).get("sandbox")
        if not isinstance(sandbox, dict) or not isinstance(sandbox.get("id"), str):
            raise PrizedError("bad_response", status=response.status_code)
        return sandbox

    # --- control plane ---

    def validate(self) -> None:
        """A read that needs a working key and creates nothing."""
        try:
            self._send("GET", f"{API_BASE}/sandboxes", params={"label": f"{LABEL_KEY}={LABEL_VALUE}"})
        except PrizedError as exc:
            if exc.code == "not_found":  # the list itself is missing: the key sees no sandboxes
                raise PrizedError("not_found", NO_SANDBOX_ACCESS, status=exc.status) from None
            raise

    def get(self, sid: str) -> dict[str, Any]:
        """The sandbox, if it exists for this key and this plugin created it."""
        sandbox = self._sandbox_doc(self._send("GET", f"{API_BASE}/sandboxes/{quote(sid, safe='')}"))
        if (sandbox.get("labels") or {}).get(LABEL_KEY) != LABEL_VALUE or sandbox.get("id") != sid:
            raise PrizedError("not_found")
        return sandbox

    def create(
        self,
        *,
        template: str,
        vcpu: int,
        memory_mb: int,
        max_runtime_min: int,
        auto_pause_min: int,
        auto_delete_min: int,
        labels: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        body = {
            "template": template,
            "resources": {"vcpu": vcpu, "memoryMb": memory_mb},
            "labels": {**(labels or {}), LABEL_KEY: LABEL_VALUE},
            "maxRuntimeMin": max_runtime_min,
            "autoPauseMin": auto_pause_min,
            "autoDeleteMin": auto_delete_min,
        }
        # One key per create: a retry after a lost answer returns the same
        # sandbox instead of making a second one.
        headers = {"Idempotency-Key": f"dify-{uuid.uuid4().hex}", "Content-Type": "application/json"}
        try:
            response = self._send(
                "POST", f"{API_BASE}/sandboxes", params={"wait": "running"}, json=body, headers=headers, timeout=40.0
            )
        except PrizedError as exc:
            if exc.code == "not_found":  # a create names no sandbox: no sandboxes for this key or workspace
                raise PrizedError("not_found", NO_SANDBOX_CREATE, status=exc.status) from None
            raise
        sandbox = self._sandbox_doc(response)
        deadline = time.monotonic() + CREATE_WAIT_SECONDS
        while sandbox.get("state") == "starting" and time.monotonic() < deadline:
            self._sleep(2.0)
            sandbox = self._sandbox_doc(self._send("GET", f"{API_BASE}/sandboxes/{quote(sandbox['id'], safe='')}"))
        if sandbox.get("state") == "error":
            raise PrizedError("sandbox_error", f"The sandbox {sandbox['id']} failed to start.")
        return sandbox

    def _lifecycle(self, sid: str, action: str, wait: str | None) -> dict[str, Any]:
        self.get(sid)
        params = {"wait": wait} if wait else None
        response = self._send(
            "POST", f"{API_BASE}/sandboxes/{quote(sid, safe='')}/{action}", params=params, timeout=20.0
        )
        return self._sandbox_doc(response)

    def pause(self, sid: str) -> dict[str, Any]:
        return self._lifecycle(sid, "pause", "paused")

    def resume(self, sid: str) -> dict[str, Any]:
        return self._lifecycle(sid, "resume", "running")

    def delete(self, sid: str) -> dict[str, Any]:
        self.get(sid)
        return self._sandbox_doc(self._send("DELETE", f"{API_BASE}/sandboxes/{quote(sid, safe='')}"))

    # --- the sandbox's command endpoint ---

    def run_command(
        self, sid: str, command: str, *, cwd: str, timeout: int, sandbox: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        sandbox = sandbox or self.get(sid)
        response = self._send(
            "POST",
            edge_url(sandbox, "/process/execute"),
            mode=EXEC,
            what="command",
            json={"command": command, "cwd": cwd, "timeout": timeout},
            timeout=timeout + 30.0,
        )
        data = self._exec_json(response, "command")
        stdout, cut_out = clip(str(data.get("stdout") or ""))
        stderr, cut_err = clip(str(data.get("stderr") or ""))
        return {
            "sandbox_id": sid,
            "exit_code": data.get("exitCode"),
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": bool(data.get("timedOut")),
            "truncated": cut_out or cut_err,
            "duration_ms": data.get("durationMs"),
        }

    def upload(self, sid: str, path: str, content: bytes, *, sandbox: Mapping[str, Any] | None = None) -> dict:
        sandbox = sandbox or self.get(sid)
        response = self._send(
            "POST",
            edge_url(sandbox, "/files/upload"),
            mode=EXEC,
            what="upload",
            params={"path": path},
            content=content,
            headers={"Content-Type": "application/octet-stream"},
            timeout=FILE_TIMEOUT,
        )
        data = self._exec_json(response, "upload")
        return {"sandbox_id": sid, "path": data.get("path", path), "size": data.get("size", len(content))}

    def download(self, sid: str, path: str, limit: int) -> tuple[bytes, int | None, bool]:
        """Up to ``limit`` bytes of the file, its size if known, and whether it was cut."""
        sandbox = self.get(sid)
        url = edge_url(sandbox, "/files/download")
        for attempt in range(MAX_ATTEMPTS):
            try:
                with self._http.stream(
                    "GET", url, params={"path": path}, headers=self._auth, timeout=FILE_TIMEOUT
                ) as response:
                    if response.status_code >= 300:
                        response.read()
                        if 300 <= response.status_code < 400:
                            raise PrizedError("unexpected_redirect", status=response.status_code)
                        error = _error_from(response)
                        if attempt < MAX_ATTEMPTS - 1 and _retryable(error, IDEMPOTENT, "GET"):
                            delay = _retry_after(response)
                            self._sleep(min(delay, RETRY_AFTER_CAP) if delay is not None else _backoff(attempt))
                            continue
                        if error.code == "not_found":  # the sandbox was found above: it is the file
                            raise PrizedError("not_found", f"No such file in the sandbox: {path}", status=404)
                        raise error
                    size = response.headers.get("content-length")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > limit:
                            break
                    data = b"".join(chunks)
                    return data[:limit], int(size) if size and size.isdigit() else None, len(data) > limit
            except httpx.HTTPError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    code = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"
                    raise PrizedError(code) from None
                self._sleep(_backoff(attempt))
        raise PrizedError("network_error")  # not reached


def guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _backoff(attempt: int) -> float:
    return min(0.5 * (2**attempt), 4.0)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def _retryable(error: PrizedError, mode: str, method: str) -> bool:
    if error.code in NEVER_RETRY or error.status is None:
        return False
    if error.code in EXEC_UNDELIVERED:
        return True
    if mode == EXEC:
        return False
    return error.status in (429, 502, 503, 504) or (error.status == 500 and method == "GET")


def _error_from(response: httpx.Response) -> PrizedError:
    """The API's error code, and its own explanation where it gives one."""
    code, detail = f"http_{response.status_code}", None
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), str) and re.fullmatch(r"[a-z_]{1,64}", data["error"]):
        code = data["error"]
        message = data.get("message")
        reason = data.get("reason")
        if code not in MESSAGES and isinstance(message, str) and message.strip():
            detail = message.strip()[:300]
        elif isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason):
            detail = f"{MESSAGES.get(code, 'The Prized API refused the request.')} Reason: {reason}."
    if code == "http_404":
        code = "not_found"
    return PrizedError(code, detail, status=response.status_code)
