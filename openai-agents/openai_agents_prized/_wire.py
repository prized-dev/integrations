"""The Prized Sandbox API's wire rules: errors, URLs, and where the key may go.

`_api.AsyncPrizedClient` sends the requests; this module holds what it needs
to decide where a request may go and what an answer means.
"""

from __future__ import annotations

import email.utils
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_API_URL = "https://api.prized.dev"
"""The API origin used when neither `api_url` nor `PRIZED_API_URL` is set."""

DEFAULT_EDGE_URL = "https://term.prized.dev"
"""Where commands and files go when a sandbox's `toolboxBase` is missing or odd."""

API_KEYS_URL = "https://prized.dev/dashboard/workspace/api-keys"
"""Where a workspace member makes an API key."""

SIGN_UP_URL = "https://prized.dev/onboarding/sandboxes"
"""Signs a newcomer up (free credit, no card) and offers a sandbox key; a
signed-out member is signed in and brought back there, which a dashboard link
does not do."""

FREE_TIER_URL = "https://prized.dev/docs/sandboxes#free-tier"
"""The free tier's limits, and what lifts them."""

_HINTS: dict[str, str] = {
    "unauthorized": f"The API key was refused: check that it is a live key from {API_KEYS_URL}",
    "token_expired": f"The API key has expired: make a new one at {API_KEYS_URL}",
}
"""A sentence for the codes whose answer carries no message, and where to go."""

PRIZED_DOMAIN = "prized.dev"

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

IN_PROGRESS = "idempotency_in_progress"
"""`409` for a keyed request whose first copy is still running (`Retry-After: 1`)."""

NEVER_RETRY = frozenset(
    {
        "sandbox_quota_exceeded",
        "paid_balance_required",
        "insufficient_balance",
        "idempotency_conflict",
        "sandboxes_disabled",
        "sandbox_create_disabled",
    }
)
"""Refusals that waiting a few seconds will not change."""

EXEC_UNDELIVERED = frozenset(
    {
        "rate_limited",
        "resolve_failed",
        "sandbox_credentials_unavailable",
        "control_plane_unavailable",
        # The edge woke a paused or starting sandbox, which was not running
        # within its wait; nothing ran.
        "sandbox_not_ready",
    }
)
"""Edge answers that prove a command never started, so it is safe to resend."""


class PrizedError(Exception):
    """Base class for every error this package raises about the Prized API."""


class PrizedSandboxGoneError(PrizedError):
    """The sandbox went to `error` or was deleted while this client waited on it."""


class PrizedAPIError(PrizedError):
    """A refusal or failure from the Prized API.

    Attributes:
        code: The API's error string (`sandbox_quota_exceeded`, `not_found`, ...)
            or one of this client's own (`timeout`, `network_error`,
            `http_error`, `bad_response`, `unexpected_redirect`).
        message: A sentence describing the failure.
        status: The HTTP status, or `None` when no answer arrived.
        body: The error document as the API sent it, when there was one.
        retry_after: Seconds the API asked the caller to wait, when it did.
        uncertain: Whether an earlier try of the same request lost its
            answer, so it may have taken effect even though this one failed.
    """

    uncertain: bool = False

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        body: Mapping[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Create an error.

        Args:
            code: The API's error string, or one of this client's own.
            message: A sentence describing the failure.
            status: The HTTP status, or `None` when no answer arrived.
            body: The error document as the API sent it.
            retry_after: Seconds the API asked the caller to wait.
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.body: dict[str, Any] = dict(body) if body else {}
        self.retry_after = retry_after

    def __str__(self) -> str:
        """Return the message with the code and status."""
        where = f", HTTP {self.status}" if self.status is not None else ""
        return f"{self.message} ({self.code}{where})"


def outcome_unknown(error: PrizedAPIError) -> bool:
    """Return whether a failed request may still have done what it asked.

    No answer (a timeout or a dropped connection), a 5xx, a success answer
    that could not be read, or `409 idempotency_in_progress` (the first
    request with that key is still running): the API may have acted before
    the answer was lost. The same holds for any failure after a retried try
    that lost its answer (`uncertain`). Any other 4xx (or a redirect) is the
    API turning the request down, so nothing happened. Callers re-read the
    sandbox before acting on an unknown outcome.

    Args:
        error: The failure.

    Returns:
        `True` when the request's effect is unknown.
    """
    if error.uncertain or error.code in {"bad_response", IN_PROGRESS}:
        return True
    return error.status is None or error.status >= 500  # noqa: PLR2004


# --- what an error says about a command that may have run --------------------
# A command is never sent again once it may have reached the sandbox, so its
# error has to say what the caller cannot see: whether it ran. The Prized
# Python and TypeScript SDKs say the same sentences (`prized` `_core.py`,
# `@prized/sdk` `outcome.ts`): keep them word for word.

_NOT_SENT_AGAIN = "It was not sent again"

NEVER = "never"
"""It never reached the sandbox: no connection opened, or an answer proves it."""
MAYBE = "maybe"
"""No clear answer came back: it may or may not have run."""
DID = "did"
"""A 2xx came back that could not be read: it ran."""


class Effect:
    """What one request does in the sandbox, in the words its errors use.

    Attributes:
        maybe: Said when no clear answer came back (a lost connection, a
            timeout, a 5xx other than the ones that prove it never arrived).
        did: Said when a 2xx came back that could not be read: it ran.
        check: How to find out which.
    """

    __slots__ = ("check", "did", "maybe")

    def __init__(self, maybe: str, did: str, check: str) -> None:
        """Create an effect.

        Args:
            maybe: Said when no clear answer came back.
            did: Said when a 2xx came back that could not be read.
            check: How to find out which.
        """
        self.maybe = maybe
        self.did = did
        self.check = check


def command_effect() -> Effect:
    """Return what a command does, in the words its errors use."""
    return Effect(
        "the command may or may not have run",
        "The command ran",
        f"{_NOT_SENT_AGAIN}: check for its effects (the files it writes, the processes it "
        "starts) before you run it again.",
    )


def helper_effect() -> Effect:
    """Return the words for a command of this package's own (a staged write, a tar)."""
    return Effect("it may or may not have taken effect", "It ran", f"{_NOT_SENT_AGAIN}.")


def _detail(error: PrizedAPIError) -> str:
    """Return the failure in a few words, for inside a sentence."""
    message = error.message.rstrip(". ")
    if error.status is None or error.code in {"http_error", "bad_response"}:
        return message
    head = f"HTTP {error.status} {error.code}"
    default = (_HINTS.get(error.code) or error.code.replace("_", " ")).rstrip(". ")
    return head if message == default else f"{head}: {message}"


def with_outcome(error: PrizedAPIError, effect: Effect, outcome: str | None) -> PrizedAPIError:
    """Return `error` (same code, status, body, and `uncertain`) saying what it means for `effect`.

    Args:
        error: The failure.
        effect: What the request does.
        outcome: `NEVER`, `MAYBE`, `DID`, or `None` to leave `error` as it is.

    Returns:
        The error to raise.
    """
    if outcome is None:
        return error
    if outcome == NEVER:
        message = (
            f"{error.message.rstrip('. ')}. It never reached the sandbox, so it had no effect."
        )
    elif outcome == DID:
        message = f"{effect.did}, but the answer could not be read ({_detail(error)})."
    else:
        message = f"No clear answer came back ({_detail(error)}), so {effect.maybe}."
        if effect.check:
            message += f" {effect.check}"
    restated = PrizedAPIError(
        error.code, message, status=error.status, body=error.body, retry_after=error.retry_after
    )
    restated.uncertain = error.uncertain
    return restated


def is_loopback(hostname: str) -> bool:
    """Return whether `hostname` names this machine.

    Args:
        hostname: A URL's host part.

    Returns:
        `True` for `localhost`, `127.0.0.1`, and `::1`.
    """
    return hostname.lower() in _LOOPBACK


def normalize_api_url(raw: str) -> str:
    """Normalize an API origin: https (http only on loopback), no `/api/v1`.

    Args:
        raw: An API URL such as `https://api.prized.dev` or
            `https://api.prized.dev/api/v1`.

    Returns:
        The origin and path with no trailing slash and no trailing `/api/v1`.

    Raises:
        ValueError: If the URL is not https (or http on loopback), or carries
            credentials, a query, or a fragment. The key is sent there, so
            nothing looser is accepted.
    """
    parts = urlsplit(raw.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        msg = f"PRIZED_API_URL is not an http(s) URL: {raw!r}"
        raise ValueError(msg)
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        msg = "PRIZED_API_URL must be an https URL (plain http only on localhost)"
        raise ValueError(msg)
    if parts.username or parts.password or parts.query or parts.fragment:
        msg = "PRIZED_API_URL must not carry credentials, a query, or a fragment"
        raise ValueError(msg)
    path = parts.path.rstrip("/").removesuffix("/api/v1")
    return f"{parts.scheme}://{parts.netloc}{path}"


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def trusted_edge(edge_host: str | None, api_host: str | None) -> bool:
    """Return whether a `toolboxBase` host may receive the key, given the API's host.

    The API's own host; prized.dev's hosts when the API is one of them; loopback
    when the API is. Never a domain guessed from the API host's name (a public
    suffix such as co.uk would trust everyone under it).

    Args:
        edge_host: The `toolboxBase` URL's host.
        api_host: The API URL's host.

    Returns:
        `True` when the key may go to `edge_host`.
    """
    if not edge_host or not api_host:
        return False
    edge, api = edge_host.lower(), api_host.lower()
    if is_loopback(api) or is_loopback(edge):
        return is_loopback(api) and is_loopback(edge)
    return edge == api or (_under(api, PRIZED_DOMAIN) and _under(edge, PRIZED_DOMAIN))


def toolbox_base_for(sandbox_id: str, toolbox_base: object, *, api_url: str) -> str:
    """Return where commands and files go for one sandbox.

    The API's own `toolboxBase` is used when it has the expected shape (https,
    or http on loopback; path exactly `/v1/sandbox/<id>`; nothing else in the
    URL) on a host trusted beside the API's (`trusted_edge`), else Prized's
    default edge for Prized's own API. The key is sent there, so nothing looser
    is followed.

    Args:
        sandbox_id: The sandbox's id.
        toolbox_base: The sandbox object's `toolboxBase` field.
        api_url: The API origin the key belongs to.

    Returns:
        The edge base URL, with no trailing slash.

    Raises:
        PrizedError: If the API is not Prized's own and named no edge it may
            send the key to.
    """
    path = f"/v1/sandbox/{quote(sandbox_id, safe='')}"
    api_host = (urlsplit(api_url).hostname or "").lower()
    if isinstance(toolbox_base, str) and toolbox_base:
        parts = urlsplit(toolbox_base)
        secure = parts.scheme == "https" or (
            parts.scheme == "http" and is_loopback(parts.hostname or "")
        )
        clean = not (parts.username or parts.password or parts.query or parts.fragment)
        trusted = trusted_edge(parts.hostname, api_host)
        if secure and clean and trusted and parts.path.rstrip("/") == path:
            return f"{parts.scheme}://{parts.netloc}{path}"
    # Prized's own edge only for Prized's own API: a key for another
    # deployment is never sent to it.
    if _under(api_host, PRIZED_DOMAIN):
        return f"{DEFAULT_EDGE_URL}{path}"
    msg = (
        f"The API at {api_url} named no command endpoint this package trusts for "
        f"{sandbox_id} (toolboxBase {toolbox_base!r})"
    )
    raise PrizedError(msg)


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Read a `Retry-After` header as seconds.

    Args:
        value: The header: delta-seconds or an HTTP date.
        now: The current Unix time, for an HTTP date.

    Returns:
        Seconds to wait, or `None` when the header is absent or unreadable.
    """
    if value is None or not value.strip():
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return seconds if seconds >= 0 else None
    try:
        at = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, at.timestamp() - now)


def error_from_answer(status: int, text: str, retry_after: float | None) -> PrizedAPIError:
    """Turn an error answer into a `PrizedAPIError`.

    Reads the flat envelope `{"error": "code", "message"?, ...}` and a nested
    `{"error": {"code", "message"}}`; anything else (a proxy's HTML page) is
    `http_error`.

    Args:
        status: The HTTP status.
        text: The answer's body.
        retry_after: The parsed `Retry-After`, if any.

    Returns:
        The error to raise.
    """
    doc: dict[str, Any] | None = None
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        doc = parsed
    code: str | None = None
    message: str | None = None
    if doc is not None:
        error = doc.get("error")
        if isinstance(error, str) and error:
            code = error
        elif isinstance(error, dict):
            code = _opt_str(error.get("code"))
            message = _opt_str(error.get("message"))
        message = message or _opt_str(doc.get("message"))
    if code is None or doc is None:
        snippet = " ".join(text.split())[:160]
        detail = f": {snippet}" if snippet else ""
        return PrizedAPIError(
            "http_error", f"HTTP {status}{detail}", status=status, retry_after=retry_after
        )
    words = message or _HINTS.get(code) or code.replace("_", " ")
    extras = [
        f"{name} {doc[name]}"
        for name in ("reason", "field", "detail", "limit", "fits", "admitted")
        if isinstance(doc.get(name), (str, int)) and doc.get(name) != ""
    ]
    if extras:
        words = f"{words} ({', '.join(extras)})"
    if doc.get("tier") == "free" and "free tier" not in words:
        words = f"{words}. It is a free-tier limit: a top-up or a plan lifts it ({FREE_TIER_URL})"
    return PrizedAPIError(code, words, status=status, body=doc, retry_after=retry_after)


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
