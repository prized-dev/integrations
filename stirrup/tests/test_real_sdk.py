# Copyright 2026 Prized
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The backend through the real prized SDK, with canned API answers (httpx.MockTransport).

The fakes in the other tests stand in for prized.AsyncSandbox; these keep the SDK itself, so
every keyword the backend passes is one the SDK takes, and a command over 240 s really goes
the SDK's detached way: a start script, then polls.
"""

import base64
import json

import httpx
import pytest

from stirrup_prized import PrizedCodeExecToolProvider

SEND = httpx.AsyncClient.send  # the real one, kept before the offline guard replaces it
SANDBOX = {
    "id": "sbx_real0001",
    "state": "running",
    "template": "prized-base-v1",
    "resources": {"vcpu": 2, "memoryMb": 4096, "diskGb": 10},
    "toolboxBase": "https://term.prized.dev/v1/sandbox/sbx_real0001",
}


class MockClient(httpx.AsyncClient):
    async def send(self, request, *args, **kwargs):
        return await SEND(self, request, *args, **kwargs)


class Api:
    """Answers the API and the command endpoint the way Prized does, from canned replies."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.exec_replies: list[dict] = []
        self.files: dict[str, bytes] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.headers.get("content-type") == "application/json" else {}
        path = request.url.path
        self.requests.append((request.method, path, body))
        if request.url.host == "api.prized.dev":
            if request.method == "POST" and path == "/api/v1/sandboxes":
                return httpx.Response(201, json={"sandbox": SANDBOX})
            if request.method == "DELETE" and path == f"/api/v1/sandboxes/{SANDBOX['id']}":
                return httpx.Response(200, json={"sandbox": {**SANDBOX, "state": "deleting"}})
        if request.url.host == "term.prized.dev" and path.startswith(f"/v1/sandbox/{SANDBOX['id']}/"):
            op = path.rsplit("/", 2)[-2:]
            if op == ["process", "execute"]:
                return httpx.Response(200, json=self.exec_replies.pop(0))
            if op == ["files", "download"]:
                wanted = request.url.params["path"]
                if wanted not in self.files:
                    return httpx.Response(404, json={"error": "not_found"})
                return httpx.Response(200, content=self.files[wanted])
        return httpx.Response(500, json={"error": "unexpected", "message": f"{request.method} {request.url}"})

    def execs(self) -> list[dict]:
        return [body for method, path, body in self.requests if path.endswith("/process/execute")]


@pytest.fixture
async def started():
    api = Api()
    client = MockClient(transport=httpx.MockTransport(api))
    connection = {"api_key": "dcp_offline_unit_test_key", "base_url": "https://api.prized.dev", "http_client": client}
    provider = PrizedCodeExecToolProvider(sandbox_kwargs=connection)
    async with provider:
        yield provider, api
    await client.aclose()
    assert ("DELETE", f"/api/v1/sandboxes/{SANDBOX['id']}", {}) in api.requests


async def test_create_sends_the_mapped_fields(started):
    _, api = started
    method, path, body = api.requests[0]
    assert (method, path) == ("POST", "/api/v1/sandboxes")
    assert body == {"template": "prized-base-v1", "maxRuntimeMin": 10, "autoDeleteMin": 60}


async def test_a_short_command_is_one_blocking_call(started):
    provider, api = started
    api.exec_replies.append({"stdout": "hi\n", "stderr": "", "exitCode": 0, "durationMs": 3})
    result = await provider.run_command("echo hi", timeout=30)
    assert (result.exit_code, result.stdout, result.error_kind) == (0, "hi\n", None)
    sent = api.execs()[-1]
    assert sent["timeout"] == 30 and sent["cwd"] == "/"
    assert sent["command"].endswith("2>/dev/null; echo hi")


async def test_a_timeout_from_the_endpoint(started):
    provider, api = started
    api.exec_replies.append({"stdout": "partial\n", "stderr": "", "exitCode": -1, "timedOut": True})
    result = await provider.run_command("sleep 100", timeout=5)
    assert result.error_kind == "timeout"
    assert "partial" in result.stderr


async def test_a_long_command_runs_detached_and_is_polled(started):
    provider, api = started
    out = base64.b64encode(b"built\n").decode()
    api.exec_replies += [
        {"stdout": "4242\n", "stderr": "", "exitCode": 0},  # the start script prints the process group
        {"stdout": f"prized-run done 0 6 0 0 1500\n{out}\n\n", "stderr": "", "exitCode": 0},  # one poll
        {"stdout": "", "stderr": "", "exitCode": 0},  # cleanup
    ]
    result = await provider.run_command("make build", timeout=900)
    assert (result.exit_code, result.stdout, result.error_kind) == (0, "built\n", None)
    start = api.execs()[-3]
    assert "setsid" in start["command"] and "prized-run" in start["command"]
    assert "make build" in start["command"] and " 900 " in start["command"]
    assert start["timeout"] == 60, "the start itself is short; the command runs on"
    assert len(api.execs()) == 3


async def test_files_through_the_sdk(started):
    provider, api = started
    api.files["/home/prized/work/out.txt"] = b"result"
    assert await provider.read_file_bytes("out.txt") == b"result"
    with pytest.raises(FileNotFoundError):
        await provider.read_file_bytes("missing.txt")
