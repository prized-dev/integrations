"""The async API client: retries, refusals, and where the key may go."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx
import pytest

from openai_agents_prized._api import (
    DEFAULT_EDGE_URL,
    AsyncPrizedClient,
    PrizedAPIError,
    PrizedError,
    normalize_api_url,
    parse_retry_after,
    toolbox_base_for,
)
from tests.fake_prized import API_KEY, API_ORIGIN, EDGE_ORIGIN

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from openai_agents_prized import PrizedSandboxClient
    from tests.fake_prized import FakeClock, FakePrized

CREATE = r"^/api/v1/sandboxes$"
EXEC = r"/process/execute$"


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("https://api.prized.dev", "https://api.prized.dev"),
        ("https://api.prized.dev/", "https://api.prized.dev"),
        ("https://api.prized.dev/api/v1", "https://api.prized.dev"),
        ("http://localhost:8080/api/v1/", "http://localhost:8080"),
    ],
)
def test_api_url_is_normalized(raw: str, normalized: str) -> None:
    assert normalize_api_url(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    ["http://api.prized.dev", "ftp://x", "https://u:p@api.prized.dev", "https://a.dev/?q=1"],
)
def test_unsafe_api_urls_are_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="PRIZED_API_URL"):
        normalize_api_url(raw)


def test_toolbox_base_of_an_unexpected_shape_is_not_followed() -> None:
    good = f"{EDGE_ORIGIN}/v1/sandbox/sbx_1"
    api = "https://api.prized.dev"
    assert toolbox_base_for("sbx_1", good, api_url=api) == good
    assert toolbox_base_for("sbx_1", f"{good}/", api_url=api) == good
    loopback = "http://localhost:9/v1/sandbox/sbx_1"
    assert toolbox_base_for("sbx_1", loopback, api_url="http://127.0.0.1:8") == loopback
    for odd in (
        None,
        "",
        loopback,  # loopback only beside a loopback API
        "http://edge.prized.dev/v1/sandbox/sbx_1",
        "https://edge.example/v1/sandbox/sbx_1",  # not Prized's host
        "https://prized.dev.example/v1/sandbox/sbx_1",
        "https://edge.prized.dev/v1/sandbox/sbx_2",
        "https://edge.prized.dev/v1/sandbox/sbx_1?x=1",
        "https://u:p@edge.prized.dev/v1/sandbox/sbx_1",
    ):
        assert toolbox_base_for("sbx_1", odd, api_url=api) == f"{DEFAULT_EDGE_URL}/v1/sandbox/sbx_1"


def test_another_deployment_names_its_own_edge() -> None:
    api = "https://sandboxes.example.com"
    edge = "https://sandboxes.example.com/v1/sandbox/sbx_1"
    assert toolbox_base_for("sbx_1", edge, api_url=api) == edge
    # Its key never goes to Prized's edge, nor to another host it names.
    for odd in (None, "https://term.example.com/v1/sandbox/sbx_1"):
        with pytest.raises(PrizedError, match="no command endpoint"):
            toolbox_base_for("sbx_1", odd, api_url=api)


def test_retry_after() -> None:
    assert parse_retry_after("3", 0) == 3.0
    assert parse_retry_after("-1", 0) is None
    assert parse_retry_after("soon", 0) is None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", 1792567670.0) == 10.0


async def test_a_create_waiting_for_capacity_is_resent_with_the_same_key(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized, clock: FakeClock
) -> None:
    fake.script(
        "POST",
        CREATE,
        httpx.Response(
            503,
            json={"error": "sandbox_capacity", "retryAfterSec": 3},
            headers={"Retry-After": "3"},
        ),
    )
    await make_client().create()
    first, second = fake.calls("POST", CREATE)
    assert first.headers["idempotency-key"] == second.headers["idempotency-key"]
    assert 3.0 in clock.sleeps
    assert len(fake.sandboxes) == 1


async def test_a_quota_refusal_is_raised_at_once(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.script(
        "POST",
        CREATE,
        httpx.Response(
            429,
            json={
                "error": "sandbox_quota_exceeded",
                "reason": "sandbox_limit",
                "tier": "free",
                "limit": 4,
            },
        ),
    )
    with pytest.raises(PrizedAPIError) as caught:
        await make_client().create()
    assert caught.value.body["tier"] == "free"
    assert str(caught.value) == (
        "sandbox quota exceeded (reason sandbox_limit, limit 4). It is a free-tier limit: a top-up "
        "or a plan lifts it (https://prized.dev/docs/sandboxes#free-tier) "
        "(sandbox_quota_exceeded, HTTP 429)"
    )
    assert len(fake.calls("POST", CREATE)) == 1


async def test_a_redirect_is_not_followed_with_the_key(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.script(
        "POST", CREATE, httpx.Response(302, headers={"Location": "https://elsewhere.test/"})
    )
    with pytest.raises(PrizedAPIError) as caught:
        await make_client().create()
    assert caught.value.code == "unexpected_redirect"
    assert len(fake.requests) == 1


async def test_an_answer_that_is_not_json(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.script("POST", CREATE, httpx.Response(500, text="<html><body>Bad gateway</body></html>"))
    with pytest.raises(PrizedAPIError) as caught:
        await make_client(max_retries=0).create()
    assert caught.value.code == "http_error"
    assert "Bad gateway" in caught.value.message


async def test_a_wrong_key(fake: FakePrized) -> None:
    from openai_agents_prized import PrizedSandboxClient  # noqa: PLC0415

    async with fake.client() as http:
        client = PrizedSandboxClient(api_key="dcp_wrong", api_url=API_ORIGIN, http_client=http)
        with pytest.raises(PrizedAPIError) as caught:
            await client.create()
        assert caught.value.code == "unauthorized"
        # The refusal says where a live key comes from.
        assert "https://prized.dev/dashboard/workspace/api-keys" in str(caught.value)


async def test_a_lost_command_answer_is_not_resent(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    session = await make_client().create()
    async with session:
        before = len(fake.calls("POST", EXEC))
        fake.script("POST", EXEC, httpx.ReadError("connection reset"))
        with pytest.raises(Exception, match="connection reset"):
            await session.exec("echo maybe")
        assert len(fake.calls("POST", EXEC)) == before + 1


# --- what a failed command says about whether it ran ----------------------------
# The Prized Python and TypeScript SDKs say the same sentences (their
# test_sandbox.py and outcome.test.ts).

CHECK = (
    "It was not sent again: check for its effects (the files it writes, the processes it "
    "starts) before you run it again."
)
EDGE = f"{EDGE_ORIGIN}/v1/sandbox/sbx_1"
EXEC_PATH = "POST /v1/sandbox/sbx_1/process/execute"


class _CutBody(httpx.AsyncByteStream):
    """A body that breaks off after its first bytes, as a dropped connection does."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"stdout": "par'
        reset = "connection reset"
        raise httpx.ReadError(reset)


async def _no_sleep(_seconds: float) -> None:
    return None


async def _failed_command(*answers: httpx.Response | Exception) -> tuple[PrizedAPIError, int]:
    """Run one command against an edge that gives `answers` in turn; the error and the sends."""
    sent = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        answer = answers[min(sent, len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AsyncPrizedClient(
            api_key=API_KEY, api_url=API_ORIGIN, http_client=http, sleep=_no_sleep
        )
        with pytest.raises(PrizedAPIError) as caught:
            await client.execute(EDGE, {"command": "make deploy", "timeout": 5}, timeout=5)
    return caught.value, sent


async def test_a_command_whose_connection_drops_never_reads_as_could_not_reach(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    from agents.sandbox.errors import ExecTransportError  # noqa: PLC0415

    session = await make_client().create()
    async with session:
        before = len(fake.calls("POST", EXEC))
        # It runs; the answer is lost on its way back.
        fake.lose("POST", EXEC, command="make deploy", answer=httpx.ReadError("connection reset"))
        with pytest.raises(ExecTransportError) as caught:
            await session.exec("make", "deploy")
        assert len(fake.calls("POST", EXEC)) == before + 1
    cause = caught.value.__cause__
    assert isinstance(cause, PrizedAPIError)
    assert (cause.code, cause.status) == ("network_error", None)
    assert re.fullmatch(
        r"No clear answer came back \(POST /v1/sandbox/sbx_\w+/process/execute: ReadError: "
        rf"connection reset\), so the command may or may not have run\. {re.escape(CHECK)}",
        cause.message,
    ), cause.message
    assert "Could not reach" not in str(caught.value)
    assert cause.message in str(caught.value)
    assert caught.value.retryable is None


@pytest.mark.parametrize(
    ("answer", "code", "detail"),
    [
        (httpx.Response(500, json={"error": "internal"}), "internal", "HTTP 500 internal"),
        (
            httpx.Response(502, json={"error": "worker_failed"}),
            "worker_failed",
            "HTTP 502 worker_failed",
        ),
        (
            httpx.Response(503, json={"error": "worker_unavailable"}),
            "worker_unavailable",
            "HTTP 503 worker_unavailable",
        ),
        (
            httpx.Response(502, json={"error": "sandbox_unreachable"}),
            "sandbox_unreachable",
            "HTTP 502 sandbox_unreachable",
        ),
        (
            httpx.Response(504, text="<html><body>504 Gateway Time-out</body></html>"),
            "http_error",
            "HTTP 504: <html><body>504 Gateway Time-out</body></html>",
        ),
    ],
)
async def test_a_5xx_says_the_command_may_or_may_not_have_run(
    answer: httpx.Response, code: str, detail: str
) -> None:
    err, sent = await _failed_command(answer)
    assert (err.code, err.status) == (code, answer.status_code)
    assert err.message == (
        f"No clear answer came back ({detail}), so the command may or may not have run. {CHECK}"
    )
    assert sent == 1


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (httpx.Response(200, text="<html>ok</html>"), "HTTP 200 with a body that is not JSON"),
        (httpx.Response(200, json={"stdout": "x"}), "The exec answer carried no exit code"),
        (
            httpx.Response(200, headers={"content-type": "application/json"}, stream=_CutBody()),
            f"{EXEC_PATH}: ReadError: connection reset",
        ),
    ],
)
async def test_a_2xx_it_could_not_read_says_the_command_ran(
    answer: httpx.Response, detail: str
) -> None:
    # The edge answers a command only once it has ended: a 2xx means it ran.
    err, sent = await _failed_command(answer)
    assert (err.code, err.status) == ("bad_response", 200)
    assert err.message == f"The command ran, but the answer could not be read ({detail})."
    assert sent == 1


async def test_a_2xx_whose_body_cannot_be_decoded_says_the_command_ran() -> None:
    garbled = httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip")
    )
    err, sent = await _failed_command(garbled)
    assert (err.code, err.status) == ("bad_response", 200)
    assert err.message.startswith(
        f"The command ran, but the answer could not be read ({EXEC_PATH}: DecodingError: "
    ), err.message
    assert sent == 1


async def test_a_header_h11_refused_is_never_quoted() -> None:
    # h11 quotes a header it refuses to send: a key with a stray newline.
    refused = httpx.LocalProtocolError("Illegal header value b'Bearer dcp_secret_value\\n'")
    err, _ = await _failed_command(refused)
    assert "dcp_secret_value" not in err.message
    assert "Bearer" not in err.message
    assert err.message.startswith(f"No clear answer came back ({EXEC_PATH}: LocalProtocolError), ")


async def test_a_lost_answer_to_a_command_of_its_own_never_speaks_of_the_command(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    import io  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from agents.sandbox.errors import WorkspaceArchiveWriteError  # noqa: PLC0415

    session = await make_client().create()
    async with session:
        # The staged write's move (the package's own command) loses its answer.
        fake.script(
            "POST", EXEC, httpx.Response(502, json={"error": "worker_failed"}), command="want=$5"
        )
        with pytest.raises(WorkspaceArchiveWriteError) as caught:
            await session.write(Path("f.txt"), io.BytesIO(b"data"))
    cause = caught.value.__cause__
    assert cause is not None
    said = str(cause.__cause__)
    assert (
        "No clear answer came back (HTTP 502 worker_failed), so it may or may not have taken "
        "effect. It was not sent again." in said
    ), said
    assert "the command may or may not have run" not in said


async def test_a_command_that_never_connected_says_it_had_no_effect() -> None:
    err, _ = await _failed_command(httpx.ConnectError("connection refused"))
    assert err.code == "network_error"
    assert err.message == (
        f"Could not reach {EDGE_ORIGIN}: connection refused. "
        "It never reached the sandbox, so it had no effect."
    )


async def test_a_command_that_timed_out_may_or_may_not_have_run() -> None:
    err, sent = await _failed_command(httpx.ReadTimeout("timed out"))
    assert err.code == "timeout"
    assert err.message == (
        f"No clear answer came back (No answer from {EDGE_ORIGIN} within 35 s), "
        f"so the command may or may not have run. {CHECK}"
    )
    assert sent == 1


async def test_a_command_turned_away_until_its_retries_ran_out_says_it_had_no_effect() -> None:
    err, sent = await _failed_command(httpx.Response(503, json={"error": "sandbox_not_ready"}))
    assert (err.code, err.status) == ("sandbox_not_ready", 503)
    assert err.message == "sandbox not ready. It never reached the sandbox, so it had no effect."
    assert sent == 4


async def test_a_refused_command_is_still_reported_as_refused() -> None:
    refusal = {"error": "spawn_failed", "message": "chdir /nope: no such file"}
    err, _ = await _failed_command(httpx.Response(400, json=refusal))
    assert (err.code, err.status) == ("spawn_failed", 400)
    assert err.message == "chdir /nope: no such file"


async def test_a_lost_api_answer_names_the_request_not_could_not_reach() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        reset = "connection reset"
        raise httpx.ReadError(reset)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AsyncPrizedClient(
            api_key=API_KEY, api_url=API_ORIGIN, http_client=http, sleep=_no_sleep
        )
        with pytest.raises(PrizedAPIError) as caught:
            await client.get_sandbox("sbx_1")
    assert caught.value.code == "network_error"
    assert caught.value.message == "GET /api/v1/sandboxes/sbx_1: ReadError: connection reset"


async def test_a_lost_download_is_retried(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    import io  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    session = await make_client().create()
    async with session:
        await session.write(Path("f.txt"), io.BytesIO(b"data"))
        fake.script("GET", r"/files/download$", httpx.ReadError("connection reset"))
        handle = await session.read(Path("f.txt"))
        assert handle.read() == b"data"
    assert len(fake.calls("GET", r"/files/download$")) == 2


async def test_a_key_problem_is_not_reported_as_a_missing_file(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    from pathlib import Path  # noqa: PLC0415

    from agents.sandbox.errors import WorkspaceArchiveReadError  # noqa: PLC0415

    session = await make_client().create()
    async with session:
        await session.exec("echo x > f.txt")
        fake.script(
            "GET",
            r"/files/download$",
            httpx.Response(
                403, json={"error": "key_access_denied", "message": "needs Sandboxes: Write"}
            ),
        )
        with pytest.raises(WorkspaceArchiveReadError) as caught:
            await session.read(Path("f.txt"))
        assert caught.value.context["provider_error_code"] == "key_access_denied"
