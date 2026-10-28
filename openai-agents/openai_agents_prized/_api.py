"""A small async client for the Prized Sandbox API.

Two hosts answer to one API key: the control plane (`{api_url}/api/v1`, which
creates, reads, pauses, resumes, and deletes sandboxes) and each sandbox's edge,
its `toolboxBase` (`/process/execute`, `/files/upload`, `/files/download`).
Reference: https://prized.dev/docs/sandboxes-api.

A request that fails raises `PrizedAPIError`, whose `code` is the API's error
string or one of this client's own: `timeout`, `network_error`, `http_error`,
`bad_response`, `unexpected_redirect`. A sandbox that never gets where it is
going (it goes to `error`, is deleted, or a wait times out) raises
`PrizedError`, the base class of both. The wire rules (errors, URLs, where the
key may go) live in `_wire`.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from urllib.parse import quote, urlsplit

import httpx

from openai_agents_prized._version import __version__
from openai_agents_prized._wire import (
    API_KEYS_URL,
    DEFAULT_API_URL,
    DEFAULT_EDGE_URL,
    DID,
    EXEC_UNDELIVERED,
    IN_PROGRESS,
    MAYBE,
    NEVER,
    NEVER_RETRY,
    SIGN_UP_URL,
    Effect,
    PrizedAPIError,
    PrizedError,
    PrizedSandboxGoneError,
    command_effect,
    error_from_answer,
    is_loopback,
    normalize_api_url,
    outcome_unknown,
    parse_retry_after,
    toolbox_base_for,
    trusted_edge,
    with_outcome,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping

__all__ = [
    "API_KEYS_URL",
    "DEFAULT_API_URL",
    "DEFAULT_EDGE_URL",
    "EDGE_MAX_TIMEOUT",
    "EXEC_UNDELIVERED",
    "HOLD_MAX",
    "NEVER_RETRY",
    "SIGN_UP_URL",
    "AsyncPrizedClient",
    "PrizedAPIError",
    "PrizedError",
    "PrizedSandboxGoneError",
    "error_from_answer",
    "is_loopback",
    "new_idempotency_key",
    "normalize_api_url",
    "outcome_unknown",
    "parse_retry_after",
    "toolbox_base_for",
    "trusted_edge",
]

_T = TypeVar("_T")

RetryMode = Literal["safe", "exec"]
"""Which failures a request may retry.

- `safe`: repeating it has no second effect (reads, state verbs, uploads of the
  same bytes, creates carrying an `Idempotency-Key`). Network failures and 429,
  502, 503, and 504 answers are retried.
- `exec`: a command. Only answers proving it never reached the sandbox are
  retried; a lost answer is not, since the command may already have run.
"""

HOLD_MAX = 8.0
"""The longest the API holds a `?wait=` answer, in seconds."""

EDGE_MAX_TIMEOUT = 3600
"""The longest one command may run, in seconds."""

_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_MAX_BACKOFF = 8.0
_MAX_RETRY_AFTER = 30.0
_EXEC_ANSWER_SLACK = 30.0
"""Seconds on top of a command's own timeout before the client stops waiting."""
_TRANSFER_TIMEOUT = 11 * 60.0
"""The edge's own limit on one transfer is ten minutes."""
_REQUEST_TIMEOUT = 60.0
_CONNECT_TIMEOUT = 10.0
_READ_CHUNK = 64 * 1024
_CLEANUP_GRACE_S = 120.0
"""How long `aclose` waits for cleanup still running in the background."""
_NO_WAKE = {"Prized-Auto-Resume": "false"}
"""On an edge request that must not wake a paused sandbox: it answers 409 sandbox_not_running."""
_NOT_CONNECTED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
"""Failures that prove a request never left: no connection opened."""


def _wake_headers(*, wake: bool) -> dict[str, str] | None:
    return None if wake else dict(_NO_WAKE)


def _sandbox_path(sandbox_id: str) -> str:
    return f"/sandboxes/{quote(sandbox_id, safe='')}"


def new_idempotency_key() -> str:
    """Return a fresh `Idempotency-Key` for one logical create."""
    return f"openai-agents-prized-{uuid.uuid4()}"


def _hold_params(want: str, hold: float) -> dict[str, str]:
    """The `?wait=` query that holds an answer until the sandbox is `want`.

    An API that predates `wait` ignores it and answers at once; the caller then
    reads the sandbox until it settles, as without a hold.
    """
    ms = int(min(hold, HOLD_MAX) * 1000)
    return {"wait": want, "waitTimeoutMs": str(ms)} if ms > 0 else {}


class AsyncPrizedClient:
    """Calls the Prized Sandbox API with one key, retrying what is safe to retry."""

    def __init__(  # noqa: PLR0913  # the test seams are keyword-only
        self,
        *,
        api_key: str,
        api_url: str = DEFAULT_API_URL,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
    ) -> None:
        """Create a client.

        Args:
            api_key: A Prized API key (`dcp_...`).
            api_url: The API origin, already normalized (`normalize_api_url`).
            http_client: An `httpx.AsyncClient` to send requests with. The
                client never closes one it was given.
            max_retries: Retries after the first attempt for failures that may
                pass.
            sleep: Seam for tests: how to wait between attempts and polls.
            monotonic: Seam for tests: the clock deadlines are measured on.
            rand: Seam for tests: jitter in `[0, 1)`.
        """
        self._api_key = api_key
        self._api_url = api_url
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient()
        self._max_retries = max_retries
        self.sleep = sleep
        self.monotonic = monotonic
        self._rand = rand
        self._background: set[asyncio.Future[Any]] = set()

    @property
    def api_url(self) -> str:
        """The API origin."""
        return self._api_url

    async def aclose(self) -> None:
        """Let cleanup still running in the background finish, then close the HTTP client.

        Cleanup that a cancellation left running (deleting a sandbox whose
        create was cancelled, pausing one whose resume failed) gets up to two
        minutes; the HTTP client is closed only if this client made it.
        """
        pending = [task for task in self._background if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=_CLEANUP_GRACE_S)
        if self._owns_http:
            await self._http.aclose()

    def background(self, work: Coroutine[Any, Any, _T]) -> asyncio.Future[_T]:
        """Start `work` as a task that a cancelled caller cannot stop.

        `aclose` waits for it. Await the result through `asyncio.shield`: a
        cancellation then reaches the caller at once while the work goes on.

        Args:
            work: Work that must not stop halfway, such as cleanup.

        Returns:
            The task.
        """
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(_settled)
        task.add_done_callback(self._background.discard)
        return task

    async def finish(self, cleanup: Coroutine[Any, Any, None]) -> None:
        """Run `cleanup` to the end even if the caller is cancelled meanwhile.

        A cancellation of the caller is raised to it at once, while `cleanup`
        goes on in the background; `aclose` waits for it. Cleanup's own errors
        are the cleanup's to log: they are not raised here.

        Args:
            cleanup: The coroutine that undoes what a failed operation made.
        """
        with contextlib.suppress(Exception):
            await asyncio.shield(self.background(cleanup))

    # --- control plane -------------------------------------------------------

    async def create_sandbox(
        self, body: Mapping[str, Any], *, idempotency_key: str, hold: float = 0
    ) -> dict[str, Any]:
        """Create a sandbox (`POST /sandboxes`).

        Args:
            body: The create document (`template`, `resources`, `envVars`, ...).
            idempotency_key: One key per logical create, kept by the caller:
                the same key and body answer with the first sandbox instead of
                making a second, so a create whose answer was lost can be asked
                again (`new_idempotency_key`).
            hold: Seconds the API may hold the answer until the sandbox is
                running (`?wait=running`), at most `HOLD_MAX`; 0 answers at once.

        Returns:
            The sandbox object: `running` when the hold saw it get there,
            otherwise usually `starting`.
        """
        answer = await self._request(
            "POST",
            self._api("/sandboxes"),
            json_body=dict(body),
            params=_hold_params("running", hold),
            # Keyed, so a resend after a lost answer returns the first sandbox.
            headers={"Idempotency-Key": idempotency_key},
            retry="safe",
        )
        return _sandbox_from(answer)

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Read one sandbox (`GET /sandboxes/{id}`).

        Args:
            sandbox_id: The sandbox's id.

        Returns:
            The sandbox object.
        """
        answer = await self._request("GET", self._api(_sandbox_path(sandbox_id)), retry="safe")
        return _sandbox_from(answer)

    async def pause_sandbox(self, sandbox_id: str, *, hold: float = 0) -> dict[str, Any]:
        """Pause a running sandbox (`POST /sandboxes/{id}/pause`).

        Args:
            sandbox_id: The sandbox's id.
            hold: Seconds the API may hold the answer until the sandbox is
                paused (`?wait=paused`), at most `HOLD_MAX`; 0 answers at once.

        Returns:
            The sandbox object: `paused` when the hold saw it get there,
            otherwise usually `pausing`.
        """
        path = f"{_sandbox_path(sandbox_id)}/pause"
        params = _hold_params("paused", hold)
        answer = await self._request("POST", self._api(path), params=params, retry="safe")
        return _sandbox_from(answer)

    async def resume_sandbox(self, sandbox_id: str, *, hold: float = 0) -> dict[str, Any]:
        """Resume a paused sandbox (`POST /sandboxes/{id}/resume`).

        Args:
            sandbox_id: The sandbox's id.
            hold: Seconds the API may hold the answer until the sandbox is
                running (`?wait=running`), at most `HOLD_MAX`; 0 answers at once.

        Returns:
            The sandbox object: `running` when the hold saw it get there,
            otherwise usually `starting`.
        """
        path = f"{_sandbox_path(sandbox_id)}/resume"
        params = _hold_params("running", hold)
        answer = await self._request("POST", self._api(path), params=params, retry="safe")
        return _sandbox_from(answer)

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """Delete a sandbox (`DELETE /sandboxes/{id}`); a no-op once it is gone.

        Args:
            sandbox_id: The sandbox's id.

        Raises:
            PrizedAPIError: If the API refuses for any reason but `not_found`.
        """
        try:
            await self._request("DELETE", self._api(_sandbox_path(sandbox_id)), retry="safe")
        except PrizedAPIError as exc:
            if exc.code != "not_found":
                raise

    # --- the edge ------------------------------------------------------------

    async def execute(
        self,
        toolbox: str,
        body: Mapping[str, Any],
        *,
        timeout: int,
        wake: bool = True,
        effect: Effect | None = None,
    ) -> dict[str, Any]:
        """Run one command (`POST {toolboxBase}/process/execute`).

        Args:
            toolbox: The sandbox's edge base URL.
            body: The exec document (`command`, `cwd`, `env`, `timeout`).
            timeout: The command's timeout in seconds; the client waits that
                long plus some slack for the answer.
            wake: Whether the edge wakes a paused sandbox to run it. The
                session passes `False` (`Prized-Auto-Resume: false`) on every
                request: a sandbox paused since answers `409
                sandbox_not_running` and stays paused, and the session wakes
                it itself when it should.

            effect: What the command does, in the words its errors use; the
                caller's own command (`command_effect`) when `None`.

        Returns:
            The exec answer: `stdout`, `stderr`, `exitCode`, `timedOut`.

        Raises:
            PrizedAPIError: If the edge refuses, or answers without an exit code.
                When the command may have reached the sandbox, the message says
                whether it ran: never, may or may not have, or did.
        """
        try:
            answer = await self._request(
                "POST",
                f"{toolbox}/process/execute",
                json_body=dict(body),
                retry="exec",
                headers=_wake_headers(wake=wake),
                timeout=timeout + _EXEC_ANSWER_SLACK,
            )
            return _exec_answer(answer)
        except PrizedAPIError as exc:
            # Never sent again once it may have arrived: say whether it ran.
            said = effect or command_effect()
            raise with_outcome(exc, said, exec_outcome(exc)) from exc.__cause__

    async def upload(self, toolbox: str, path: str, content: bytes, *, wake: bool = True) -> None:
        """Write one file whole (`POST {toolboxBase}/files/upload`).

        Args:
            toolbox: The sandbox's edge base URL.
            path: An absolute path under `/home/prized`.
            content: The file's bytes.
            wake: Whether the edge wakes a paused sandbox for it (`execute`).
        """
        await self._request(
            "POST",
            f"{toolbox}/files/upload",
            params={"path": path},
            content=content,
            headers=_wake_headers(wake=wake),
            # The same bytes to the same path: a resend replaces the file whole
            # with what it already holds.
            retry="safe",
            timeout=_TRANSFER_TIMEOUT,
        )

    async def download(
        self, toolbox: str, path: str, *, max_bytes: int | None = None, wake: bool = True
    ) -> bytes:
        """Read one file (`GET {toolboxBase}/files/download`).

        Args:
            toolbox: The sandbox's edge base URL.
            path: An absolute path under `/home/prized`.
            max_bytes: Stop reading after this many bytes, so a bounded read of
                a large file never pulls the rest of it; `None` reads it all.
            wake: Whether the edge wakes a paused sandbox for it (`execute`).

        Returns:
            The file's bytes, or its first `max_bytes` bytes.
        """
        return await self._request_body(
            f"{toolbox}/files/download",
            params={"path": path},
            max_bytes=max_bytes,
            headers=_wake_headers(wake=wake),
        )

    # --- internals -----------------------------------------------------------

    def _api(self, path: str) -> str:
        return f"{self._api_url}/api/v1{path}"

    def _retryable(self, error: PrizedAPIError, mode: RetryMode) -> bool:
        if error.code in NEVER_RETRY:
            return False
        if mode == "exec":
            return error.code in EXEC_UNDELIVERED
        if error.code == IN_PROGRESS:
            return True  # the first request with this key is still running: ask again
        return error.status is None or error.status in _RETRYABLE_STATUS

    async def _backoff(self, failure: PrizedAPIError, attempt: int) -> None:
        base = min(_MAX_BACKOFF, 0.5 * 2**attempt)
        backoff = base / 2 + self._rand() * base / 2
        asked = min(_MAX_RETRY_AFTER, failure.retry_after or 0.0)
        await self.sleep(max(backoff, asked))

    def _should_retry(self, failure: PrizedAPIError, retry: RetryMode, attempt: int) -> bool:
        if failure.code in {"unexpected_redirect", "bad_response"}:
            return False
        # A network failure or a timeout under `exec` may mean the command ran.
        if failure.status is None and retry != "safe":
            return False
        return self._retryable(failure, retry) and attempt < self._max_retries

    async def _request(  # noqa: PLR0913  # one keyword per request part keeps call sites explicit
        self,
        method: str,
        url: str,
        *,
        retry: RetryMode,
        json_body: Any = None,  # noqa: ANN401  # any JSON document
        content: bytes | None = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> httpx.Response:
        attempt = 0
        uncertain = False
        while True:
            try:  # the retry loop
                return await self._attempt(
                    method,
                    url,
                    json_body=json_body,
                    content=content,
                    params=params,
                    headers=headers,
                    timeout=timeout,
                    retry=retry,
                )
            except PrizedAPIError as failure:  # noqa: PERF203  # the retry loop
                # A refusal after a try whose answer was lost does not undo
                # that try: the error carries it, so callers re-read.
                failure.uncertain = uncertain
                undelivered = retry == "exec" and failure.code in EXEC_UNDELIVERED
                uncertain = uncertain or (outcome_unknown(failure) and not undelivered)
                if not self._should_retry(failure, retry, attempt):
                    raise
                await self._backoff(failure, attempt)
                attempt += 1

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": f"openai-agents-prized/{__version__}",
            **(extra or {}),
        }

    async def _attempt(  # noqa: PLR0913
        self,
        method: str,
        url: str,
        *,
        json_body: Any,  # noqa: ANN401
        content: bytes | None,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        timeout: float,
        retry: RetryMode,
    ) -> httpx.Response:
        all_headers = self._headers(headers)
        if content is not None:
            all_headers["Content-Type"] = "application/octet-stream"
        request = self._http.build_request(
            method,
            url,
            params=dict(params) if params else None,
            json=json_body,
            content=content,
            headers=all_headers,
            timeout=httpx.Timeout(timeout, connect=_CONNECT_TIMEOUT),
        )
        response: httpx.Response | None = None
        try:
            # The status first, then the body: for a command, a body that
            # breaks after a 2xx status is an answer that came, so it ran.
            # Never follow a redirect with the key attached.
            response = await self._http.send(request, stream=True, follow_redirects=False)
            try:
                await response.aread()
            finally:
                await response.aclose()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _lost(method, url, exc, timeout, response, retry=retry) from exc
        except httpx.DecodingError as exc:
            # A body that cannot be decoded: for a command, an answer that
            # came; anything else fails as it always has.
            if retry != "exec":
                raise
            raise _lost(method, url, exc, timeout, response, retry=retry) from exc
        answered = response
        _raise_for_status(answered.status_code, answered.headers, lambda: answered.text)
        return answered

    async def _request_body(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_bytes: int | None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """GET a file body, retrying what is safe, reading at most `max_bytes`."""
        attempt = 0
        while True:
            try:  # the retry loop
                return await self._stream_once(
                    url, params=params, max_bytes=max_bytes, headers=headers
                )
            except PrizedAPIError as failure:  # noqa: PERF203  # the retry loop
                if not self._should_retry(failure, "safe", attempt):
                    raise
                await self._backoff(failure, attempt)
                attempt += 1

    async def _stream_once(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_bytes: int | None,
        headers: Mapping[str, str] | None,
    ) -> bytes:
        timeout = httpx.Timeout(_TRANSFER_TIMEOUT, connect=_CONNECT_TIMEOUT)
        try:
            async with self._http.stream(
                "GET",
                url,
                params=dict(params),
                headers=self._headers(headers),
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code >= 300:  # noqa: PLR2004
                    text = (await response.aread()).decode("utf-8", "replace")
                    _raise_for_status(response.status_code, response.headers, lambda: text)
                data = bytearray()
                async for chunk in response.aiter_bytes(_READ_CHUNK):
                    data.extend(chunk)
                    if max_bytes is not None and len(data) >= max_bytes:
                        del data[max_bytes:]
                        break
                return bytes(data)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _lost("GET", url, exc, _TRANSFER_TIMEOUT, None, retry="safe") from exc


def _settled(task: asyncio.Future[Any]) -> None:
    """Retrieve a background task's error, so an unawaited one is not reported as lost."""
    if not task.cancelled():
        task.exception()


def _raise_for_status(status: int, headers: httpx.Headers, text: Callable[[], str]) -> None:
    if 300 <= status < 400:  # noqa: PLR2004
        msg = (
            f"The API answered {status} (a redirect), which this client does "
            "not follow with your key"
        )
        raise PrizedAPIError("unexpected_redirect", msg, status=status)
    if status >= 400:  # noqa: PLR2004
        retry_after = parse_retry_after(headers.get("Retry-After"), time.time())
        raise error_from_answer(status, text(), retry_after)


def exec_outcome(error: PrizedAPIError) -> str | None:
    """Return how a command that failed ended, as `with_outcome` says it.

    Args:
        error: What the command's request raised.

    Returns:
        `NEVER` when no connection opened or the answer proves it never
        arrived, `DID` for a 2xx that could not be read, `MAYBE` for no clear
        answer (a lost connection, a timeout, any other 5xx), and `None` for a
        refusal, whose own words say what happened.
    """
    if error.status is None:
        return NEVER if isinstance(error.__cause__, _NOT_CONNECTED) else MAYBE
    if 200 <= error.status < 300:  # noqa: PLR2004
        return DID
    if error.code in EXEC_UNDELIVERED:
        return NEVER
    return MAYBE if error.status >= 500 else None  # noqa: PLR2004


def _lost(  # noqa: PLR0913
    method: str,
    url: str,
    exc: Exception,
    timeout: float,
    response: httpx.Response | None,
    *,
    retry: RetryMode,
) -> PrizedAPIError:
    """Return the error for a request that got no whole answer.

    "Could not reach" only when no connection opened; once one did, the request
    may have arrived, so the message names the request and the failure (never
    the query, which can hold a path). For a command, a 2xx status that came
    before the body broke off is `bad_response` with that status: it ran.
    """
    # h11's text for a header it refused to send can quote the header, the
    # key's included: only its name is said (the error stays in __cause__).
    quotes = isinstance(exc, httpx.LocalProtocolError)
    detail = type(exc).__name__ if quotes else (str(exc) or type(exc).__name__).rstrip(".")
    if isinstance(exc, _NOT_CONNECTED):
        msg = f"Could not reach {_origin(url)}: {detail}"
    elif isinstance(exc, httpx.TimeoutException):
        msg = f"No answer from {_origin(url)} within {timeout:g} s"
    elif quotes:
        msg = f"{method} {urlsplit(url).path}: {detail}"
    else:
        msg = f"{method} {urlsplit(url).path}: {type(exc).__name__}: {detail}"
    status = None if response is None else response.status_code
    if retry == "exec" and status is not None and 200 <= status < 300:  # noqa: PLR2004
        return PrizedAPIError("bad_response", msg, status=status)
    code = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"
    return PrizedAPIError(code, msg)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _json(answer: httpx.Response) -> dict[str, Any]:
    try:
        doc = answer.json()
    except ValueError as exc:
        msg = f"HTTP {answer.status_code} with a body that is not JSON"
        raise PrizedAPIError("bad_response", msg, status=answer.status_code) from exc
    if not isinstance(doc, dict):
        msg = f"HTTP {answer.status_code} with a body that is not a JSON object"
        raise PrizedAPIError("bad_response", msg, status=answer.status_code)
    return doc


def _exec_answer(answer: httpx.Response) -> dict[str, Any]:
    doc = _json(answer)
    if not isinstance(doc.get("exitCode"), int):
        msg = "The exec answer carried no exit code."
        raise PrizedAPIError("bad_response", msg, status=answer.status_code)
    return doc


def _sandbox_from(answer: httpx.Response) -> dict[str, Any]:
    sandbox = _json(answer).get("sandbox")
    if not isinstance(sandbox, dict) or not isinstance(sandbox.get("id"), str):
        msg = "The API answered without a sandbox document."
        raise PrizedAPIError("bad_response", msg, status=answer.status_code)
    return sandbox
