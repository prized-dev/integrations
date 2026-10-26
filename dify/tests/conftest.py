"""A fake Prized API (control plane and command endpoint) behind httpx.MockTransport."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from dify_plugin import Tool
from dify_plugin.core.utils.class_loader import load_single_subclass_from_source

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import plugin_tool  # noqa: E402
import prized_client as pc  # noqa: E402

KEY = "dcp_0000000000000000fake"


class FakePrized:
    """Enough of https://prized.dev/docs/sandboxes-api to drive every tool."""

    def __init__(self) -> None:
        self.sandboxes: dict[str, dict] = {}
        self.files: dict[tuple[str, str], bytes] = {}
        self.requests: list[httpx.Request] = []
        self.idempotency: dict[str, str] = {}
        self.queue: list[httpx.Response | Exception] = []  # forced answers, first in first out
        self.exec_answer: dict | None = None
        self.dify_files: dict[str, bytes] = {}
        self.toolbox_base = "https://term.prized.dev/v1/sandbox/{sid}"
        self.counter = 0

    def add(self, sid: str = "sbx_00000000000000aa", labels: dict | None = None, state: str = "running") -> dict:
        doc = {
            "id": sid,
            "state": state,
            "template": "prized-base-v1",
            "resources": {"vcpu": 2, "memoryMb": 4096, "diskGb": 10},
            "labels": {"integration": "dify"} if labels is None else labels,
            "autoPauseMin": 10,
            "autoDeleteMin": 1440,
            "expiresAt": "2026-10-26T12:00:00Z",
            "errorReason": None,
            "toolboxBase": self.toolbox_base.format(sid=sid),
            "createdAt": "2026-10-26T11:00:00Z",
        }
        self.sandboxes[sid] = doc
        return doc

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.startswith("/dify-files/"):  # Dify's own file server, which needs no Prized key
            content = self.dify_files.get(request.url.path.removeprefix("/dify-files/"))
            return httpx.Response(200, content=content) if content is not None else httpx.Response(404)
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if self.queue:
            forced = self.queue.pop(0)
            if isinstance(forced, Exception):
                raise forced
            return forced
        url = urlsplit(str(request.url))
        query = parse_qs(url.query)
        path = url.path
        loopback = url.hostname == "127.0.0.1"  # tests/test_dify_runtime.py serves this over HTTP
        if url.hostname == "api.prized.dev" or (loopback and path.startswith("/api/v1/")):
            return self.control(request, path, query)
        if url.hostname == "term.prized.dev" or (loopback and path.startswith("/v1/sandbox/")):
            return self.edge(request, path, query)
        return httpx.Response(599, json={"error": "unexpected_host"})

    def control(self, request: httpx.Request, path: str, query: dict) -> httpx.Response:
        parts = path.removeprefix("/api/v1/").split("/")
        if parts == ["sandboxes"] and request.method == "GET":
            return httpx.Response(200, json={"sandboxes": list(self.sandboxes.values())})
        if parts == ["sandboxes"] and request.method == "POST":
            key = request.headers.get("idempotency-key")
            if key in self.idempotency:
                return httpx.Response(200, json={"sandbox": self.sandboxes[self.idempotency[key]]})
            body = json.loads(request.content)
            self.counter += 1
            doc = self.add(f"sbx_{self.counter:016x}", labels=body["labels"])
            doc["template"] = body["template"]
            doc["resources"].update(body["resources"])
            doc["autoPauseMin"], doc["autoDeleteMin"] = body["autoPauseMin"], body["autoDeleteMin"]
            self.idempotency[key] = doc["id"]
            return httpx.Response(201, json={"sandbox": doc})
        if len(parts) >= 2 and parts[0] == "sandboxes":
            doc = self.sandboxes.get(parts[1])
            if doc is None:
                return httpx.Response(404, json={"error": "not_found"})
            if len(parts) == 2 and request.method == "GET":
                return httpx.Response(200, json={"sandbox": doc})
            if len(parts) == 2 and request.method == "DELETE":
                doc["state"] = "deleting"
                return httpx.Response(202, json={"sandbox": doc})
            if parts[2] == "pause":
                doc["state"] = "paused"
                doc["expiresAt"] = None
                return httpx.Response(200, json={"sandbox": doc})
            if parts[2] == "resume":
                doc["state"] = "running"
                return httpx.Response(200, json={"sandbox": doc})
        return httpx.Response(404, json={"error": "not_found"})

    def edge(self, request: httpx.Request, path: str, query: dict) -> httpx.Response:
        prefix = "/v1/sandbox/"
        sid, _, op = path.removeprefix(prefix).partition("/")
        if sid not in self.sandboxes:
            return httpx.Response(404, json={"error": "not_found"})
        if op == "process/execute":
            body = json.loads(request.content)
            if self.exec_answer is not None:
                return httpx.Response(200, json=self.exec_answer)
            return httpx.Response(
                200, json={"stdout": f"ran: {body['command']}\n", "stderr": "", "exitCode": 0, "durationMs": 12}
            )
        if op == "files/upload":
            target = query["path"][0]
            self.files[(sid, target)] = request.content
            return httpx.Response(200, json={"path": target, "size": len(request.content)})
        if op == "files/download":
            target = query["path"][0]
            if (sid, target) not in self.files:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, content=self.files[(sid, target)])
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePrized:
    api = FakePrized()
    monkeypatch.setattr(
        plugin_tool,
        "make_client",
        lambda credentials: pc.PrizedClient(credentials.get("api_key"), transport=api.transport, sleep=lambda s: None),
    )
    return api


def invoke(tool_cls, params: dict, key: str = KEY) -> list:
    tool = tool_cls.from_credentials({"api_key": key})
    return list(tool.invoke(dict(params)))


def outputs(messages: list) -> tuple[dict, dict, list]:
    """(variables, json, blobs) from a tool's messages."""
    variables, data, blobs = {}, {}, []
    for message in messages:
        kind = message.type.value if hasattr(message.type, "value") else message.type
        if kind == "variable":
            variables[message.message.variable_name] = message.message.variable_value
        elif kind == "json":
            data = message.message.json_object
        elif kind == "blob":
            blobs.append((message.message.blob, message.meta))
    return variables, data, blobs


def tool(name: str) -> type[Tool]:
    """A tool class, loaded from its source file the way Dify loads it."""
    return load_single_subclass_from_source(
        module_name=f"tools.{name}", script_path=str(ROOT / "tools" / f"{name}.py"), parent_type=Tool
    )


class StubFile:
    """What Dify hands a tool for a file parameter: a URL to fetch it from, a name, a size."""

    def __init__(self, url: str, filename: str | None, size: int | None = None):
        self.url, self.filename, self.size = url, filename, size


class FakeFiles:
    """Dify's file server for Upload file: URLs to bytes, and what each host resolves to."""

    def __init__(self) -> None:
        self.content: dict[str, bytes] = {}
        self.addresses: dict[str, str] = {"files.dify.example": "93.184.216.34"}
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, headers={"location": "http://127.0.0.1/x"})
        body = self.content.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    def resolve(self, host, port, proto=0):
        if host not in self.addresses:
            raise OSError("no such host")
        return [(2, 1, 6, "", (self.addresses[host], port))]


@pytest.fixture
def files(monkeypatch: pytest.MonkeyPatch) -> FakeFiles:
    server = FakeFiles()
    monkeypatch.setattr(plugin_tool, "FILE_TRANSPORT", httpx.MockTransport(server.handle))
    monkeypatch.setattr(plugin_tool, "RESOLVE", server.resolve)
    return server
