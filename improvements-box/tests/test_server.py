"""The page and its API on 127.0.0.1, against fake sandboxes."""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator

import pytest

from improvements_box import Box, make_server


@pytest.fixture
def served(make) -> Iterator[tuple[Box, int]]:
    box = make()
    server = make_server(box, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield box, server.server_address[1]
    server.shutdown()
    server.server_close()


def call(
    port: int, method: str, path: str, body: bytes | None = None, **headers: str
) -> tuple[int, dict | str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers={k.replace("_", "-"): v for k, v in headers.items()})
    r = conn.getresponse()
    data = r.read().decode()
    conn.close()
    return r.status, json.loads(data) if r.getheader("Content-Type") == "application/json" else data


def post(port: int, doc: object, **headers: str) -> tuple[int, dict | str]:
    return call(
        port, "POST", "/api/suggestions", json.dumps(doc).encode(), Content_Type="application/json", **headers
    )


def test_the_page_takes_a_suggestion_and_shows_it_through_to_the_patch(served) -> None:
    box, port = served
    status, page = call(port, "GET", "/")
    assert status == 200 and "<h1>Improvements box</h1>" in page

    status, doc = post(port, {"text": "Kelvin please"}, Origin=f"http://127.0.0.1:{port}")
    assert status == 202
    sid = doc["id"]

    deadline = time.monotonic() + 60
    while True:
        status, state = call(port, "GET", "/api/suggestions")
        (s,) = state["suggestions"]
        if s["outcome"] not in ("queued", "running"):
            break
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert s["id"] == sid and s["outcome"] == "patch"
    assert s["sandbox"]["state"] == "deleted"
    assert [st["state"] for st in s["steps"]] == ["ok"] * 9
    assert "test_kelvin" in s["patch"] and s["agent"]["cost_usd"] == 0.0123
    assert state["config"]["mode"] == "local" and state["config"]["protect"] == [".github/"]


def test_the_page_never_writes_suggestion_text_as_html(served) -> None:
    _, port = served
    _, page = call(port, "GET", "/")
    assert "innerHTML" not in page and "textContent" in page


@pytest.mark.parametrize(
    "headers, status",
    [
        ({"Origin": "https://evil.example"}, 403),  # another site's page
        ({"Host": "evil.example"}, 403),  # a name rebound to 127.0.0.1
    ],
)
def test_other_sites_cannot_submit(served, headers: dict[str, str], status: int) -> None:
    box, port = served
    code, _ = post(port, {"text": "Kelvin please"}, **headers)
    assert code == status
    assert box.items == {}


def test_a_name_given_with_allow_host_is_served(make) -> None:
    box = make()
    server = make_server(box, "127.0.0.1", 0, ("box.example.com",))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        assert call(port, "GET", "/api/suggestions", Host=f"box.example.com:{port}")[0] == 200
        assert call(port, "GET", "/api/suggestions", Host="other.example.com")[0] == 403
    finally:
        server.shutdown()
        server.server_close()


def test_only_json_is_taken(served) -> None:
    box, port = served
    code, _ = call(
        port, "POST", "/api/suggestions", b"text=hi", Content_Type="application/x-www-form-urlencoded"
    )
    assert code == 415
    assert post(port, {"text": ""})[0] == 400
    assert post(port, ["not", "an", "object"])[0] == 400
    assert post(port, {"text": "x" * 2001})[0] == 413
    bad = {"Content_Type": "application/json", "Content_Length": "-1"}
    assert call(port, "POST", "/api/suggestions", None, **bad)[0] == 400
    assert call(port, "GET", "/nope")[0] == 404
    assert box.items == {}
