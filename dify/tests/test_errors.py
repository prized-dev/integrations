"""Errors, retries and the API key's safety."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from conftest import KEY, ROOT, invoke, tool
from dify_plugin import Tool, ToolProvider
from dify_plugin.core.utils.class_loader import load_single_subclass_from_source
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

import plugin_tool
import prized_client as pc

SID = "sbx_00000000000000aa"


def provider() -> ToolProvider:
    cls = load_single_subclass_from_source(
        module_name="provider.prized", script_path=str(ROOT / "provider" / "prized.py"), parent_type=ToolProvider
    )
    return cls()


def test_an_error_echoing_the_key_never_shows_it(fake):
    fake.add()
    fake.queue.append(httpx.Response(400, json={"error": "weird_thing", "message": f"bad key {KEY} here"}))
    with pytest.raises(plugin_tool.ToolError) as caught:
        invoke(tool("get_sandbox"), {"sandbox_id": SID})
    assert KEY not in str(caught.value)
    assert "dcp_[redacted]" in str(caught.value) and "(weird_thing)" in str(caught.value)


@pytest.mark.parametrize("kind", [RuntimeError, ValueError, KeyError])
def test_an_unexpected_exception_is_not_passed_on(fake, kind):
    def boom(client, params):
        raise kind(f"internal detail with {KEY}")

    cls = type("T", (plugin_tool.PrizedTool,), {"run": staticmethod(boom)})
    with pytest.raises(plugin_tool.ToolError) as caught:
        invoke(cls, {"sandbox_id": SID})
    assert "internal detail" not in str(caught.value) and KEY not in str(caught.value)


def test_known_codes_get_a_fixed_sentence(fake):
    fake.queue.append(httpx.Response(429, json={"error": "sandbox_quota_exceeded", "reason": "sandbox_limit"}))
    with pytest.raises(plugin_tool.ToolError) as caught:
        invoke(tool("create_sandbox"), {})
    text = str(caught.value)
    assert "sandbox quota" in text and "sandbox_limit" in text and "(sandbox_quota_exceeded)" in text
    assert len(fake.requests) == 1  # a quota does not change by waiting: no retry


def test_a_wrong_key_is_refused_with_a_clear_message(fake):
    with pytest.raises(plugin_tool.ToolError, match="key was refused"):
        invoke(tool("get_sandbox"), {"sandbox_id": SID}, key="dcp_wrong")


@pytest.mark.parametrize("key", [None, "", "   ", "dcp_has space", "dcp_\nnewline", "x" * 600, "dcp_caf\u00e9"])
def test_malformed_keys_never_reach_the_network(fake, key):
    with pytest.raises(plugin_tool.ToolError, match="missing or malformed"):
        invoke(tool("get_sandbox"), {"sandbox_id": SID}, key=key)
    assert fake.requests == []


def test_a_redirect_is_not_followed(fake):
    fake.queue.append(httpx.Response(302, headers={"location": "https://evil.example/steal"}))
    with pytest.raises(plugin_tool.ToolError, match="redirect"):
        invoke(tool("get_sandbox"), {"sandbox_id": SID})
    assert all(r.url.host == "api.prized.dev" for r in fake.requests)


def test_create_retries_a_lost_answer_with_the_same_idempotency_key(fake):
    fake.queue.append(httpx.ReadTimeout("lost"))
    invoke(tool("create_sandbox"), {})
    posts = [r for r in fake.requests if r.method == "POST"]
    assert len(posts) == 2
    assert posts[0].headers["idempotency-key"] == posts[1].headers["idempotency-key"]
    assert len(fake.sandboxes) == 1


def test_a_command_whose_answer_was_lost_is_not_sent_again(fake):
    fake.add()
    fake.queue += [httpx.Response(200, json={"sandbox": fake.sandboxes[SID]}), httpx.ReadTimeout("lost")]
    with pytest.raises(plugin_tool.ToolError, match="may or may not have run"):
        invoke(tool("run_command"), {"sandbox_id": SID, "command": "make deploy"})
    assert sum(r.url.path.endswith("/process/execute") for r in fake.requests) == 1


def test_a_command_the_sandbox_may_have_run_is_not_sent_again(fake):
    fake.add()
    fake.queue += [
        httpx.Response(200, json={"sandbox": fake.sandboxes[SID]}),
        httpx.Response(502, json={"error": "sandbox_unreachable"}),
    ]
    with pytest.raises(plugin_tool.ToolError, match="may or may not have run"):
        invoke(tool("run_command"), {"sandbox_id": SID, "command": "make deploy"})
    assert sum(r.url.path.endswith("/process/execute") for r in fake.requests) == 1


def test_a_command_that_never_ran_is_retried(fake):
    fake.add()
    fake.queue += [
        httpx.Response(200, json={"sandbox": fake.sandboxes[SID]}),
        httpx.Response(503, json={"error": "sandbox_not_ready"}, headers={"retry-after": "1"}),
    ]
    invoke(tool("run_command"), {"sandbox_id": SID, "command": "echo once"})
    assert sum(r.url.path.endswith("/process/execute") for r in fake.requests) == 2


def test_a_read_is_retried_on_a_server_error(fake):
    fake.add()
    fake.queue.append(httpx.Response(500, json={"error": "internal"}))
    invoke(tool("get_sandbox"), {"sandbox_id": SID})
    assert [r.method for r in fake.requests] == ["GET", "GET"]


def test_retries_stop(fake):
    fake.queue += [httpx.Response(503, json={"error": "sandbox_capacity"})] * 5
    with pytest.raises(plugin_tool.ToolError, match="capacity"):
        invoke(tool("create_sandbox"), {})
    assert len(fake.requests) == pc.MAX_ATTEMPTS


def test_a_sandbox_in_error_is_reported(fake):
    fake.queue.append(
        httpx.Response(201, json={"sandbox": {**fake.add("sbx_00000000000000dd"), "state": "error"}}),
    )
    with pytest.raises(plugin_tool.ToolError, match="failed to start"):
        invoke(tool("create_sandbox"), {})


def test_provider_accepts_a_working_key(monkeypatch):
    from conftest import FakePrized

    api = FakePrized()
    monkeypatch.setattr(pc, "PrizedClient", _client_with(api))
    provider().validate_credentials({"api_key": KEY})
    request = api.requests[-1]
    assert request.method == "GET" and request.url.params["label"] == "integration=dify"


def test_provider_refuses_a_wrong_key_without_echoing_it(monkeypatch):
    from conftest import FakePrized

    monkeypatch.setattr(pc, "PrizedClient", _client_with(FakePrized()))
    with pytest.raises(ToolProviderCredentialValidationError) as caught:
        provider().validate_credentials({"api_key": "dcp_wrongwrongwrong"})
    assert "refused" in str(caught.value) and "dcp_wrongwrongwrong" not in str(caught.value)


def test_nothing_is_logged_with_the_key_or_a_path(fake, caplog):
    fake.add()
    with caplog.at_level(logging.DEBUG):
        invoke(tool("write_file"), {"sandbox_id": SID, "path": "secret-plans.txt", "content": "x"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert KEY not in text and "secret-plans" not in text


def test_error_bodies_are_never_shown_raw(fake):
    fake.add()
    fake.queue.append(httpx.Response(500, content=b"<html>proxy page " + KEY.encode() + b"</html>"))
    fake.queue.append(httpx.Response(500, content=b"<html>proxy page</html>"))
    fake.queue.append(httpx.Response(500, content=b"<html>proxy page</html>"))
    with pytest.raises(plugin_tool.ToolError) as caught:
        invoke(tool("get_sandbox"), {"sandbox_id": SID})
    assert "html" not in str(caught.value) and "(http_500)" in str(caught.value)


def _client_with(api):
    real = pc.PrizedClient

    def make(key, **kwargs):
        return real(key, transport=api.transport, sleep=lambda s: None)

    return make


def test_json_error_shapes():
    response = httpx.Response(409, content=json.dumps({"error": "insufficient_balance"}).encode())
    assert pc._error_from(response).code == "insufficient_balance"
    assert pc._error_from(httpx.Response(404, content=b"nope")).code == "not_found"
    assert pc._error_from(httpx.Response(418, json={"error": "Bad Code!"})).code == "http_418"


def test_tool_class_is_a_tool():
    assert issubclass(plugin_tool.PrizedTool, Tool)
