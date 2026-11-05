"""Exposed ports: `resolve_exposed_port()` against the fake API, and live."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
import pytest
from agents.sandbox.errors import ExposedPortUnavailableError
from agents.sandbox.session import BaseSandboxClientOptions
from pydantic import ValidationError

from openai_agents_prized import (
    PrizedSandboxClient,
    PrizedSandboxClientOptions,
    PrizedSandboxSession,
    PrizedSandboxSessionState,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from agents.sandbox.session import SandboxSession

    from tests.fake_prized import FakePrized


def _inner(session: SandboxSession) -> PrizedSandboxSession:
    inner = session._inner
    assert isinstance(inner, PrizedSandboxSession)
    return inner


def _token_calls(fake: FakePrized) -> list[Any]:
    return fake.calls("POST", r"/ports/\d+/token$")


# --- options and state ------------------------------------------------------------


def test_exposed_port_options_validate_before_a_sandbox_is_made() -> None:
    options = PrizedSandboxClientOptions(exposed_ports=[8080, 3000, 8080])
    assert options.exposed_ports == (8080, 3000)
    assert options.exposed_port_url_ttl_s == 3600
    assert options.exposed_port_auth == "token"
    assert PrizedSandboxClientOptions().exposed_ports == ()
    parsed = BaseSandboxClientOptions.parse(
        {"type": "prized", "exposed_ports": [5173], "exposed_port_auth": "key"}
    )
    assert isinstance(parsed, PrizedSandboxClientOptions)
    assert parsed.exposed_ports == (5173,)
    assert parsed.exposed_port_auth == "key"
    assert BaseSandboxClientOptions.parse(parsed.model_dump(mode="json")) == parsed
    # The sandbox's own ports have no URL; the API would refuse them later.
    for port in (22, 81):
        with pytest.raises(ValidationError, match="sandbox's own"):
            PrizedSandboxClientOptions(exposed_ports=[port])
    for bad in ([0], [65536], [True], ["8080"], "8080"):
        with pytest.raises(ValidationError):
            PrizedSandboxClientOptions(exposed_ports=bad)
    for ttl in (59, 86401):
        with pytest.raises(ValidationError):
            PrizedSandboxClientOptions(exposed_port_url_ttl_s=ttl)
    with pytest.raises(ValidationError):
        PrizedSandboxClientOptions(exposed_port_auth="public")


async def test_exposed_ports_are_kept_in_the_session_state(
    make_client: Callable[..., PrizedSandboxClient],
) -> None:
    client = make_client()
    options = PrizedSandboxClientOptions(
        exposed_ports=[8080], exposed_port_url_ttl_s=600, exposed_port_auth="key"
    )
    session = await client.create(options=options)
    assert session.state.exposed_ports == (8080,)
    payload = client.serialize_session_state(session.state)
    restored = client.deserialize_session_state(dict(payload))
    assert isinstance(restored, PrizedSandboxSessionState)
    assert restored.exposed_ports == (8080,)
    assert restored.exposed_port_url_ttl_s == 600
    assert restored.exposed_port_auth == "key"
    # A state saved by 0.1.0 has none of the three: no ports, a token URL.
    for name in ("exposed_ports", "exposed_port_url_ttl_s", "exposed_port_auth"):
        payload.pop(name, None)
    old = client.deserialize_session_state(dict(payload))
    assert isinstance(old, PrizedSandboxSessionState)
    assert old.exposed_ports == ()
    assert old.exposed_port_auth == "token"
    await client.delete(session)


# --- token URLs ---------------------------------------------------------------------


async def test_a_token_url_opens_the_port_in_a_browser_and_for_programs(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    options = PrizedSandboxClientOptions(exposed_ports=[8080], exposed_port_url_ttl_s=900)
    session = await client.create(options=options)
    sandbox_id = _inner(session).sandbox_id
    endpoint = await session.resolve_exposed_port(8080)
    assert endpoint.host == f"8080-{sandbox_id.replace('_', '-')}.prized.link"
    assert (endpoint.port, endpoint.tls) == (443, True)
    assert parse_qs(endpoint.query) == {"prized_token": [fake.port_tokens[-1]]}
    assert endpoint.url_for("http") == (
        f"https://8080-{sandbox_id.replace('_', '-')}.prized.link/?{endpoint.query}"
    )
    assert endpoint.url_for("ws").startswith("wss://")
    (call,) = _token_calls(fake)
    assert call.path == f"/api/v1/sandboxes/{sandbox_id}/ports/8080/token"
    assert call.json == {"expiresIn": 900}
    # Each call makes a new token, so a caller whose URL expired asks again.
    again = await session.resolve_exposed_port(8080)
    assert again.query != endpoint.query
    assert len(fake.port_tokens) == 2
    await client.delete(session)


async def test_without_a_browser_domain_the_token_url_is_the_ingress_url(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.browser_domain = False
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(exposed_ports=[3000]))
    endpoint = await session.resolve_exposed_port(3000)
    assert endpoint.host == f"3000-{_inner(session).sandbox_id.replace('_', '-')}.p.prized.dev"
    assert endpoint.query.startswith("prized_token=dpt_")
    await client.delete(session)


async def test_a_port_not_in_exposed_ports_is_refused_without_a_request(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(exposed_ports=[8080]))
    with pytest.raises(ExposedPortUnavailableError) as raised:
        await session.resolve_exposed_port(9090)
    assert raised.value.context["reason"] == "not_configured"
    assert not _token_calls(fake)
    plain = await client.create()
    with pytest.raises(ExposedPortUnavailableError, match="not configured"):
        await plain.resolve_exposed_port(8080)
    await client.delete(session)
    await client.delete(plain)


async def test_a_paused_sandbox_is_resumed_before_its_token_url_is_made(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(exposed_ports=[8080]))
    sandbox_id = _inner(session).sandbox_id
    fake.sandbox(sandbox_id).state = "paused"  # it paused after auto_pause_min
    await session.resolve_exposed_port(8080)
    # A token never wakes a sandbox, so the session resumed it first.
    assert fake.calls("POST", rf"^/api/v1/sandboxes/{sandbox_id}/resume$")
    assert fake.port_token_states == ["running"]
    await client.delete(session)


async def test_no_url_for_a_sandbox_that_cannot_run(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(exposed_ports=[8080]))
    fake.sandbox(_inner(session).sandbox_id).state = "error"
    with pytest.raises(ExposedPortUnavailableError) as raised:
        await session.resolve_exposed_port(8080)
    assert raised.value.context["detail"] == "sandbox_not_running"
    assert raised.value.retryable is False
    assert not _token_calls(fake)


@pytest.mark.parametrize("auth", ["token", "key"])
async def test_no_url_once_the_session_has_released_its_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized, auth: str
) -> None:
    client = make_client()
    options = PrizedSandboxClientOptions(
        exposed_ports=[8080], pause_on_exit=True, exposed_port_auth=auth
    )
    session = await client.create(options=options)
    sandbox_id = _inner(session).sandbox_id
    await client.delete(session)  # paused
    requests = len(fake.requests)
    with pytest.raises(ExposedPortUnavailableError) as raised:
        await session.resolve_exposed_port(8080)
    # A token URL would answer 503, and the key would wake the sandbox the run paused.
    assert raised.value.context["detail"] == "session_released"
    assert len(fake.requests) == requests
    assert fake.sandbox(sandbox_id).state == "paused"


async def test_a_reserved_port_in_a_saved_state_is_refused_without_a_request(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create()
    payload = client.serialize_session_state(session.state)
    payload["exposed_ports"] = [22]
    resumed = await client.resume(client.deserialize_session_state(dict(payload)))
    requests = len(fake.requests)
    with pytest.raises(ExposedPortUnavailableError) as raised:
        await resumed.resolve_exposed_port(22)
    assert raised.value.context["detail"] == "port_reserved"
    assert len(fake.requests) == requests
    await client.delete(resumed)


async def test_a_workspace_with_free_credit_only_is_told_what_to_do(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.free_credit_only = True
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(exposed_ports=[8080]))
    with pytest.raises(ExposedPortUnavailableError) as raised:
        await session.resolve_exposed_port(8080)
    error = raised.value
    assert error.context["reason"] == "backend_unavailable"
    assert error.context["provider_error_code"] == "share_links_need_payment"
    assert error.context["http_status"] == 402
    assert "exposed_port_auth='key'" in str(error.context["hint"])
    assert error.retryable is False
    await client.delete(session)


async def test_a_lost_token_answer_is_asked_again(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(exposed_ports=[8080]))
    fake.lose("POST", r"/ports/8080/token$")
    endpoint = await session.resolve_exposed_port(8080)
    # The first token was made and lost; the second is the one returned.
    assert len(fake.port_tokens) == 2
    assert endpoint.query == f"prized_token={fake.port_tokens[-1]}"
    await client.delete(session)


SBX = "sbx-0000000000000001"
"""The first sandbox the fake makes, as its URLs spell it."""
HOST = f"8080-{SBX}"
"""Its port 8080: the first label of every URL for that port."""
P = f"{HOST}.p.prized.dev"
QS = "prized_token=dpt_x"


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        ({"token": "dpt_x"}, "bad_response"),
        ({"token": "dpt_x", "url": f"https://{P}/"}, "token_missing"),
        ({"token": "dpt_x", "url": f"http://{P}/?{QS}"}, "invalid_preview_url"),
        ({"token": "dpt_x", "url": f"https://{P}/app?{QS}"}, "invalid_preview_url"),
        ({"token": "dpt_x", "url": f"https://{P}/?{QS}#f"}, "invalid_preview_url"),
        ({"token": "dpt_x", "url": f"https://u@{P}/?{QS}"}, "invalid_preview_url"),
        ({"token": "dpt_x", "url": f"https://a.example\\{P}/?{QS}"}, "invalid_preview_url"),
        ({"token": "dpt_x", "url": f"https://{P}:99999/?{QS}"}, "invalid_preview_url"),
        (
            {"token": "dpt_x", "url": f"https://8080-sbx-9.p.prized.dev/?{QS}"},
            "foreign_preview_host",
        ),
    ],
    ids=[
        "no-url",
        "no-token",
        "plain-http",
        "a-path",
        "a-fragment",
        "userinfo",
        "backslash",
        "bad-port",
        "other-sandbox",
    ],
)
async def test_a_token_answer_that_cannot_be_used_is_refused(
    make_client: Callable[..., PrizedSandboxClient],
    fake: FakePrized,
    answer: dict[str, str],
    detail: str,
) -> None:
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(exposed_ports=[8080]))
    fake.script("POST", r"/ports/8080/token$", httpx.Response(201, json=answer))
    with pytest.raises(ExposedPortUnavailableError) as raised:
        await session.resolve_exposed_port(8080)
    context = raised.value.context
    assert detail in (context.get("provider_error_code"), context.get("detail"))
    # The token is the credential: it is never in the error.
    assert "dpt_x" not in repr(context)
    assert "dpt_x" not in str(raised.value)
    await client.delete(session)


async def test_a_browser_url_without_the_token_falls_back_to_the_url(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(exposed_ports=[8080]))
    answer = {
        "token": "dpt_y",
        "url": f"https://{P}/?prized_token=dpt_y",
        "browserUrl": f"https://{HOST}.prized.link/",
    }
    fake.script("POST", r"/ports/8080/token$", httpx.Response(201, json=answer))
    endpoint = await session.resolve_exposed_port(8080)
    assert endpoint.host == P
    assert endpoint.query == "prized_token=dpt_y"
    await client.delete(session)


async def test_a_refused_port_names_the_api_error(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(exposed_ports=[8080]))
    fake.script(
        "POST",
        r"/ports/8080/token$",
        httpx.Response(503, json={"error": "preview_unavailable", "message": "off"}),
        times=10,
    )
    with pytest.raises(ExposedPortUnavailableError) as raised:
        await session.resolve_exposed_port(8080)
    assert raised.value.context["provider_error_code"] == "preview_unavailable"
    assert raised.value.retryable is True  # a 503 the API may stop sending
    await client.delete(session)


# --- the API key in a header ---------------------------------------------------------


async def test_key_auth_gives_the_bare_url_and_makes_no_token(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    options = PrizedSandboxClientOptions(exposed_ports=[8080], exposed_port_auth="key")
    session = await client.create(options=options)
    sandbox_id = _inner(session).sandbox_id
    endpoint = await session.resolve_exposed_port(8080)
    assert endpoint.host == f"8080-{sandbox_id.replace('_', '-')}.p.prized.dev"
    assert (endpoint.port, endpoint.tls, endpoint.query) == (443, True, "")
    assert not _token_calls(fake)
    await client.delete(session)


@pytest.mark.parametrize(
    ("template", "code"),
    [
        (None, "preview_unavailable"),
        (f"https://{{port}}-{SBX}.attacker.example/", "untrusted_preview_host"),
        (f"https://attacker.example\\{{port}}-{SBX}.p.prized.dev/", "invalid_preview_url"),
        ("https://{port}-sbx-9.p.prized.dev/", "foreign_preview_host"),
        (f"https://u@{{port}}-{SBX}.p.prized.dev/", "invalid_preview_url"),
        (f"https://{{port}}-{SBX}.p.prized.dev/#f", "invalid_preview_url"),
        (f"https://{{port}}-{SBX}.p.prized.dev/?k=1", "untrusted_preview_host"),
    ],
    ids=[
        "ingress-off",
        "foreign-domain",
        "backslash",
        "other-sandbox",
        "userinfo",
        "fragment",
        "query",
    ],
)
async def test_key_auth_never_points_the_key_at_a_host_it_may_not_go_to(
    make_client: Callable[..., PrizedSandboxClient],
    fake: FakePrized,
    template: str | None,
    code: str,
) -> None:
    client = make_client()
    options = PrizedSandboxClientOptions(exposed_ports=[8080], exposed_port_auth="key")
    session = await client.create(options=options)
    sandbox = fake.sandbox(_inner(session).sandbox_id)
    document = sandbox.document()
    if template is None:
        document.pop("previewUrlTemplate")
    else:
        document["previewUrlTemplate"] = template
    fake.script(
        "GET",
        rf"^/api/v1/sandboxes/{sandbox.id}$",
        httpx.Response(200, json={"sandbox": document}),
    )
    with pytest.raises(ExposedPortUnavailableError) as raised:
        await session.resolve_exposed_port(8080)
    assert raised.value.context["detail"] == code
    assert raised.value.retryable is False
    await client.delete(session)


async def test_a_resumed_session_keeps_its_ports(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    options = PrizedSandboxClientOptions(exposed_ports=[8080], pause_on_exit=True)
    session = await client.create(options=options)
    await client.delete(session)  # paused, not deleted
    payload = client.serialize_session_state(session.state)
    resumed = await client.resume(client.deserialize_session_state(dict(payload)))
    assert _inner(resumed).sandbox_id == _inner(session).sandbox_id
    endpoint = await resumed.resolve_exposed_port(8080)
    assert endpoint.query == f"prized_token={fake.port_tokens[-1]}"
    await client.delete(resumed)


# --- live ------------------------------------------------------------------------------

LIVE_LABELS = {"agent": "agent-xp", "task": "openai-agents-prized"}
SERVER = (
    "mkdir -p /home/prized/site && echo exposed-port-ok > /home/prized/site/index.html && "
    "cd /home/prized/site && (nohup python3 -m http.server 8080 > /dev/null 2>&1 &)"
)


@pytest.fixture
async def live_client() -> AsyncIterator[PrizedSandboxClient]:
    client = PrizedSandboxClient()
    yield client
    await client.close()


async def _get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as http:
        for _ in range(20):
            answer = await http.get(url, headers=headers)
            if answer.status_code != 502:  # the server is still starting
                return answer
            await asyncio.sleep(0.5)
        return answer


@pytest.mark.live
@pytest.mark.parametrize("auth", ["token", "key"])
async def test_live_exposed_port(live_client: PrizedSandboxClient, auth: str) -> None:
    options = PrizedSandboxClientOptions(
        vcpu=1,
        memory_mb=2048,
        max_runtime_min=15,
        labels=LIVE_LABELS,
        exposed_ports=[8080],
        exposed_port_url_ttl_s=300,
        exposed_port_auth=auth,
    )
    session = await live_client.create(options=options)
    try:
        async with session:
            result = await session.exec(SERVER)
            assert result.ok()
            endpoint = await session.resolve_exposed_port(8080)
            url = endpoint.url_for("http")
            if auth == "token":
                assert endpoint.query.startswith("prized_token=")
                answer = await _get(url)
            else:
                assert endpoint.query == ""
                assert (await _get(url)).status_code == 401
                answer = await _get(url, {"Prized-Key": os.environ["PRIZED_API_KEY"]})
            assert answer.status_code == 200
            assert answer.text.strip() == "exposed-port-ok"
    finally:
        await live_client.delete(session)
