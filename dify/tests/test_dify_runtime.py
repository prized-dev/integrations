"""The plugin inside Dify's own plugin runtime (`dify plugin run`), against the fake API.

Needs the Dify plugin CLI (DIFY_CLI_PATH) and uv; skipped without them. The
runtime builds the plugin's environment from requirements.txt, so it needs PyPI,
but the plugin only talks to a fake Prized API on 127.0.0.1: the test packages
a copy whose two base URLs point there. The real package is built too, and its
file list checked; DIFY_PACKAGE_OUT, if set, is where it is written.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from conftest import KEY, ROOT, FakePrized
from pydantic import BaseModel

CLI = os.environ.get("DIFY_CLI_PATH", "")
pytestmark = pytest.mark.skipif(
    not (CLI and shutil.which("uv")), reason="set DIFY_CLI_PATH to the Dify plugin CLI, with uv on PATH"
)

PROVIDER = "prized_sandboxes"
RUNTIME_FILES = {
    ".difyignore",
    "LICENSE",
    "PRIVACY.md",
    "README.md",
    "_assets/icon.svg",
    "main.py",
    "manifest.yaml",
    "plugin_tool.py",
    "prized_client.py",
    "provider/prized.py",
    "provider/prized.yaml",
    "requirements.txt",
}


class Validated(BaseModel):
    result: bool


def serve(api: FakePrized) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _any(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            request = httpx.Request(
                self.command,
                f"http://127.0.0.1:{self.server.server_port}{self.path}",
                headers=dict(self.headers.items()),
                content=self.rfile.read(length) if length else b"",
            )
            response = api.handle(request)
            body = response.read()
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() not in ("content-length", "transfer-encoding", "connection"):
                    self.send_header(name, value)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_DELETE = _any

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def package(source: Path, out: Path) -> Path:
    subprocess.run([CLI, "plugin", "package", str(source), "-o", str(out)], check=True, capture_output=True)
    return out


def test_the_package_holds_only_runtime_files(tmp_path):
    out = Path(os.environ.get("DIFY_PACKAGE_OUT") or tmp_path)
    pkg = package(ROOT, out / "prized_sandboxes-0.1.0.difypkg")
    names = {n for n in zipfile.ZipFile(pkg).namelist() if not n.endswith("/")}
    tools = {n for n in names if n.startswith("tools/")}
    assert names - tools == RUNTIME_FILES
    assert len(tools) == 20 and all(n.endswith((".py", ".yaml")) for n in tools)


def test_every_tool_through_the_dify_runtime(tmp_path):
    from dify_plugin.config.integration_config import IntegrationConfig
    from dify_plugin.core.entities.plugin.request import (
        PluginInvokeType,
        ToolActions,
        ToolInvokeRequest,
        ToolValidateCredentialsRequest,
    )
    from dify_plugin.entities.tool import ToolInvokeMessage
    from dify_plugin.integration.run import PluginRunner

    api = FakePrized()
    server = serve(api)
    port = server.server_port
    # Untrusted (plain http), so the plugin falls back to its edge base: the fake below.
    api.toolbox_base = f"http://127.0.0.1:{port}/v1/sandbox/{{sid}}"
    copy = tmp_path / "plugin"
    shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    client = (copy / "prized_client.py").read_text()
    for old, new in [
        ('API_BASE = "https://api.prized.dev/api/v1"', f'API_BASE = "http://127.0.0.1:{port}/api/v1"'),
        ('EDGE_BASE = "https://term.prized.dev"', f'EDGE_BASE = "http://127.0.0.1:{port}"'),
    ]:
        assert old in client
        client = client.replace(old, new)
    (copy / "prized_client.py").write_text(client)
    pkg = package(copy, tmp_path / "loopback.difypkg")
    credentials = {"api_key": KEY}

    def call(tool: str, **params) -> list[ToolInvokeMessage]:
        request = ToolInvokeRequest(
            user_id="test", provider=PROVIDER, tool=tool, credentials=credentials, tool_parameters=params
        )
        return list(runner.invoke(PluginInvokeType.Tool, ToolActions.InvokeTool, request, ToolInvokeMessage))

    def variables(messages: list[ToolInvokeMessage]) -> dict:
        return {
            m.message.variable_name: m.message.variable_value
            for m in messages
            if m.type == ToolInvokeMessage.MessageType.VARIABLE
        }

    try:
        with PluginRunner(config=IntegrationConfig(dify_cli_path=CLI), plugin_package_path=str(pkg)) as runner:
            (ok,) = runner.invoke(
                PluginInvokeType.Tool,
                ToolActions.ValidateCredentials,
                ToolValidateCredentialsRequest(user_id="test", provider=PROVIDER, credentials=credentials),
                Validated,
            )
            assert ok.result is True

            created = call("create_sandbox")
            kinds = {m.type for m in created}
            assert ToolInvokeMessage.MessageType.JSON in kinds
            sid = variables(created)["sandbox_id"]
            assert variables(created)["state"] == "running"

            call("write_file", sandbox_id=sid, path="main.py", content="print(6 * 7)")
            ran = variables(call("run_code", sandbox_id=sid, language="python", code="print(1)"))
            assert ran["exit_code"] == 0 and ran["stdout"].startswith("ran: python3 /home/prized/work/.dify-run-")
            text = variables(call("read_file", sandbox_id=sid, path="main.py"))
            assert text["content"] == "print(6 * 7)"
            as_file = call("read_file", sandbox_id=sid, path="main.py", output="file")
            blobs = [m for m in as_file if m.type.value.startswith("blob")]
            assert blobs, [m.type for m in as_file]

            # A file parameter arrives the way Dify sends one: a reference to a file server URL.
            api.dify_files["data.csv"] = b"a,b\n1,2\n"
            file_param = {
                "dify_model_identity": "__dify__file__",
                "url": f"http://127.0.0.1:{port}/dify-files/data.csv",
                "mime_type": "text/csv",
                "type": "document",
                "filename": "data.csv",
                "extension": ".csv",
                "size": 8,
            }
            # It reaches the tool as a file, and the tool refuses a loopback URL before fetching it.
            with pytest.raises(ValueError, match="public address"):
                call("upload_file", sandbox_id=sid, file=file_param)
            assert not any(r.url.path.startswith("/dify-files/") for r in api.requests)

            one_off = variables(call("run_code", language="bash", code="echo hi"))
            assert one_off["one_off"] is True and "sandbox_id" not in one_off

            with pytest.raises(ValueError, match="sandbox_id must be"):
                call("get_sandbox", sandbox_id="not-an-id")
            credentials["api_key"] = "dcp_wrong"
            with pytest.raises(ValueError, match="key was refused") as caught:
                call("get_sandbox", sandbox_id=sid)
            assert "dcp_wrong" not in str(caught.value)
            credentials["api_key"] = KEY

            assert variables(call("delete_sandbox", sandbox_id=sid))["state"] == "deleting"
    finally:
        server.shutdown()
