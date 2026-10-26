"""Each tool, loaded the way Dify loads it, against the fake Prized API."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
import pytest
from conftest import KEY, StubFile, invoke, outputs, tool

import plugin_tool
import prized_client as pc


def test_create_sends_shape_labels_and_an_idempotency_key(fake):
    variables, data, _ = outputs(
        invoke(
            tool("create_sandbox"),
            {"vcpu": 2, "memory_gb": 4, "max_runtime_minutes": 30, "labels": "team=data, app=report"},
        )
    )
    request = fake.requests[-1]
    body = json.loads(request.content)
    assert request.method == "POST" and request.url.params["wait"] == "running"
    assert request.headers["idempotency-key"].startswith("dify-")
    assert body == {
        "template": "prized-base-v1",
        "resources": {"vcpu": 2, "memoryMb": 4096},
        "labels": {"team": "data", "app": "report", "integration": "dify"},
        "maxRuntimeMin": 30,
        "autoPauseMin": 10,
        "autoDeleteMin": 1440,
    }
    assert "envVars" not in body  # nothing of the plugin's (its key above all) goes into a sandbox
    assert variables["sandbox_id"] == data["sandbox_id"] == "sbx_0000000000000001"
    assert variables["state"] == "running"
    assert "expires_at" in variables


def test_create_defaults_fit_the_free_tier(fake):
    invoke(tool("create_sandbox"), {})
    body = json.loads(fake.requests[-1].content)
    assert body["resources"] == {"vcpu": 2, "memoryMb": 4096}
    assert body["maxRuntimeMin"] == 60


@pytest.mark.parametrize(
    "params",
    [{"vcpu": 0}, {"vcpu": 33}, {"memory_gb": 1.5}, {"template": "ubuntu"}, {"labels": "integration=x"}],
)
def test_create_rejects_bad_input_before_any_request(fake, params):
    with pytest.raises(plugin_tool.ToolError):
        invoke(tool("create_sandbox"), params)
    assert fake.requests == []


def test_create_polls_until_running(fake):
    starting = fake.add("sbx_00000000000000bb", state="starting")
    fake.queue.append(httpx.Response(201, json={"sandbox": starting}))  # the hold ended before running
    fake.queue.append(httpx.Response(200, json={"sandbox": starting}))  # first read: still starting
    starting["state"] = "running"  # second read: running
    _, data, _ = outputs(invoke(tool("create_sandbox"), {}))
    assert data["state"] == "running"
    assert [r.method for r in fake.requests] == ["POST", "GET", "GET"]


def test_run_command(fake):
    fake.add()
    variables, data, _ = outputs(
        invoke(tool("run_command"), {"sandbox_id": "sbx_00000000000000aa", "command": "echo hi", "cwd": "app"})
    )
    request = fake.requests[-1]
    assert request.url.host == "term.prized.dev"
    assert request.url.path == "/v1/sandbox/sbx_00000000000000aa/process/execute"
    assert json.loads(request.content) == {"command": "echo hi", "cwd": "/home/prized/work/app", "timeout": 60}
    assert variables["exit_code"] == 0 and variables["stdout"] == "ran: echo hi\n"
    assert data["timed_out"] is False and data["truncated"] is False


def test_run_command_clips_long_output_keeping_the_end(fake):
    fake.add()
    fake.exec_answer = {"stdout": "a" * 100_000 + "THE END", "stderr": "", "exitCode": 1, "durationMs": 5}
    _, data, _ = outputs(invoke(tool("run_command"), {"sandbox_id": "sbx_00000000000000aa", "command": "x"}))
    assert data["truncated"] is True
    assert data["stdout"].endswith("THE END")
    assert len(data["stdout"].encode()) < pc.OUTPUT_LIMIT + 100
    assert data["exit_code"] == 1


def test_run_command_timeout_is_bounded(fake):
    fake.add()
    with pytest.raises(plugin_tool.ToolError, match="timeout_seconds"):
        invoke(tool("run_command"), {"sandbox_id": "sbx_00000000000000aa", "command": "x", "timeout_seconds": 241})


def test_run_code_uploads_a_file_runs_it_and_removes_it(fake):
    fake.add()
    variables, _, _ = outputs(
        invoke(tool("run_code"), {"sandbox_id": "sbx_00000000000000aa", "language": "python", "code": "print(6*7)"})
    )
    upload, execute = fake.requests[-2], fake.requests[-1]
    path = upload.url.params["path"]
    assert path.startswith("/home/prized/work/.dify-run-") and path.endswith(".py")
    assert upload.content == b"print(6*7)"
    command = json.loads(execute.content)["command"]
    assert command == f"python3 {path}; status=$?; rm -f {path}; exit $status"
    assert variables["language"] == "python"


def test_write_then_read_text(fake):
    fake.add()
    sid = "sbx_00000000000000aa"
    _, written, _ = outputs(invoke(tool("write_file"), {"sandbox_id": sid, "path": "notes/a.txt", "content": "héllo"}))
    assert written == {"sandbox_id": sid, "path": "/home/prized/work/notes/a.txt", "size": len("héllo".encode())}
    variables, data, blobs = outputs(invoke(tool("read_file"), {"sandbox_id": sid, "path": "notes/a.txt"}))
    assert variables["content"] == "héllo" and data["truncated"] is False and data["binary"] is False
    assert blobs == []


def test_read_text_is_cut_at_the_limit_without_breaking_a_character(fake):
    fake.add()
    sid = "sbx_00000000000000aa"
    fake.files[(sid, "/home/prized/work/big.txt")] = "é".encode() * pc.READ_TEXT_LIMIT
    _, data, _ = outputs(invoke(tool("read_file"), {"sandbox_id": sid, "path": "big.txt"}))
    assert data["truncated"] is True and data["binary"] is False
    assert len(data["content"].encode()) <= pc.READ_TEXT_LIMIT


def test_read_binary_as_text_says_so(fake):
    fake.add()
    sid = "sbx_00000000000000aa"
    fake.files[(sid, "/home/prized/work/x.bin")] = b"\xff\xfe\x00\x01"
    _, data, _ = outputs(invoke(tool("read_file"), {"sandbox_id": sid, "path": "x.bin"}))
    assert data["binary"] is True


def test_read_as_file_returns_a_blob(fake):
    fake.add()
    sid = "sbx_00000000000000aa"
    fake.files[(sid, "/home/prized/work/chart.png")] = b"\x89PNG\r\n\x1a\n" + b"0" * 10
    variables, data, blobs = outputs(
        invoke(tool("read_file"), {"sandbox_id": sid, "path": "/home/prized/work/chart.png", "output": "file"})
    )
    assert blobs == [(b"\x89PNG\r\n\x1a\n" + b"0" * 10, {"mime_type": "image/png", "filename": "chart.png"})]
    assert data == {"sandbox_id": sid, "path": "/home/prized/work/chart.png", "size": 18, "filename": "chart.png"}
    assert "content" not in variables


@pytest.mark.parametrize("path", ["/etc/passwd", "../../../etc/passwd", "/home/prizedx/a", "/home/prized/../root"])
def test_paths_outside_home_are_refused_before_any_request(fake, path):
    fake.add()
    with pytest.raises(plugin_tool.ToolError, match="inside /home/prized"):
        invoke(tool("read_file"), {"sandbox_id": "sbx_00000000000000aa", "path": path})
    assert fake.requests == []


@pytest.mark.parametrize("name", ["get_sandbox", "pause_sandbox", "resume_sandbox", "delete_sandbox"])
def test_lifecycle_tools(fake, name):
    fake.add()
    variables, _, _ = outputs(invoke(tool(name), {"sandbox_id": "sbx_00000000000000aa"}))
    expected = {"get_sandbox": "running", "pause_sandbox": "paused", "resume_sandbox": "running"}
    assert variables["state"] == expected.get(name, "deleting")
    if name == "pause_sandbox":
        assert fake.requests[-1].url.params["wait"] == "paused"
        assert "expires_at" not in variables  # null while paused: no variable rather than None


@pytest.mark.parametrize(
    "name,params",
    [
        ("run_command", {"command": "rm -rf ~"}),
        ("write_file", {"path": "a", "content": "b"}),
        ("read_file", {"path": "a"}),
        ("pause_sandbox", {}),
        ("delete_sandbox", {}),
    ],
)
def test_sandboxes_the_plugin_did_not_create_are_refused(fake, name, params):
    fake.add("sbx_00000000000000cc", labels={"team": "prod"})
    with pytest.raises(plugin_tool.ToolError, match="not created by this plugin"):
        invoke(tool(name), {"sandbox_id": "sbx_00000000000000cc", **params})
    assert [r.method for r in fake.requests] == ["GET"]  # read only, nothing changed


def test_a_toolbox_base_off_prized_dev_is_not_trusted(fake):
    fake.add()["toolboxBase"] = "https://evil.example/v1/sandbox/sbx_00000000000000aa"
    invoke(tool("run_command"), {"sandbox_id": "sbx_00000000000000aa", "command": "id"})
    assert {r.url.host for r in fake.requests} == {"api.prized.dev", "term.prized.dev"}


def test_a_toolbox_base_on_a_customer_port_host_is_not_trusted(fake):
    fake.add()["toolboxBase"] = "https://8080-x.p.prized.dev/v1/sandbox/sbx_00000000000000aa"
    invoke(tool("run_command"), {"sandbox_id": "sbx_00000000000000aa", "command": "id"})
    assert {(r.url.host, r.url.port) for r in fake.requests} == {("api.prized.dev", None), ("term.prized.dev", None)}


def test_run_code_cleans_up_after_a_timeout(fake):
    fake.add()
    fake.exec_answer = {"stdout": "", "stderr": "", "exitCode": -1, "timedOut": True, "durationMs": 2000}
    _, data, _ = outputs(
        invoke(
            tool("run_code"), {"sandbox_id": "sbx_00000000000000aa", "code": "while True: pass", "timeout_seconds": 2}
        )
    )
    assert data["timed_out"] is True
    snippet = fake.requests[-3].url.params["path"]  # GET, upload, run, then the cleanup run
    assert json.loads(fake.requests[-1].content)["command"] == f"rm -f {snippet}"


@pytest.mark.parametrize(
    "base",
    [
        "http://term.prized.dev/v1/sandbox/sbx_00000000000000aa",
        "https://term.prized.dev.evil.example/v1/sandbox/sbx_00000000000000aa",
        "https://user:pw@term.prized.dev/v1/sandbox/sbx_00000000000000aa",
        "https://term.prized.dev/v1/sandbox/sbx_other",
        # Other prized.dev hosts are not the edge: customers' shared ports live under prized.dev.
        "https://8080-sbx_00000000000000aa.p.prized.dev/v1/sandbox/sbx_00000000000000aa",
        "https://eu.term.prized.dev/v1/sandbox/sbx_00000000000000aa",
        "https://term.prized.dev:8443/v1/sandbox/sbx_00000000000000aa",
        "https://term.prized.dev:notaport/v1/sandbox/sbx_00000000000000aa",
        "https://[::1/v1/sandbox/sbx_00000000000000aa",
        "",
    ],
)
def test_edge_url_falls_back_to_the_default_edge(base):
    url = pc.edge_url({"id": "sbx_00000000000000aa", "toolboxBase": base}, "/process/execute")
    assert url == "https://term.prized.dev/v1/sandbox/sbx_00000000000000aa/process/execute"


def test_edge_url_uses_term_prized_dev():
    url = pc.edge_url(
        {"id": "sbx_00000000000000aa", "toolboxBase": "https://term.prized.dev/v1/sandbox/sbx_00000000000000aa"},
        "/files/upload",
    )
    assert url == "https://term.prized.dev/v1/sandbox/sbx_00000000000000aa/files/upload"


@pytest.mark.parametrize("sid", ["", "sbx_", "abc", "sbx_../../x", "sbx_aa/bb", None, 12])
def test_bad_sandbox_ids_are_refused_before_any_request(fake, sid):
    with pytest.raises(plugin_tool.ToolError, match="sandbox_id"):
        invoke(tool("get_sandbox"), {"sandbox_id": sid})
    assert fake.requests == []


def test_every_request_carries_the_key_only_in_the_authorization_header(fake):
    fake.add()
    invoke(tool("run_code"), {"sandbox_id": "sbx_00000000000000aa", "language": "bash", "code": "echo $HOME"})
    for request in fake.requests:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert KEY not in str(request.url)
        assert KEY.encode() not in request.content


def test_run_code_without_a_sandbox_uses_a_one_off_and_deletes_it(fake):
    variables, data, _ = outputs(invoke(tool("run_code"), {"language": "python", "code": "print(1)"}))
    methods = [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in fake.requests]
    assert methods[0] == ("POST", "sandboxes")
    assert methods[1:3] == [("POST", "upload"), ("POST", "execute")]
    assert methods[-1][0] == "DELETE"
    body = json.loads(fake.requests[0].content)
    assert body["resources"] == {"vcpu": 2, "memoryMb": 4096} and body["maxRuntimeMin"] == 15
    assert body["labels"] == {"one-off": "true", "integration": "dify"}
    assert data["one_off"] is True and "sandbox_id" not in data and "sandbox_id" not in variables
    assert all(s["state"] == "deleting" for s in fake.sandboxes.values())


def test_a_one_off_sandbox_is_deleted_even_when_the_run_fails(fake):
    original = fake.edge

    def failing_edge(request, path, query):
        if path.endswith("/process/execute"):
            return httpx.Response(502, json={"error": "sandbox_unreachable"})
        return original(request, path, query)

    fake.edge = failing_edge
    with pytest.raises(plugin_tool.ToolError, match="may or may not have run"):
        invoke(tool("run_command"), {"command": "echo hi"})
    assert fake.requests[-1].method == "DELETE"


def test_bad_input_creates_no_one_off_sandbox(fake):
    with pytest.raises(plugin_tool.ToolError):
        invoke(tool("run_command"), {"command": "x", "timeout_seconds": 999})
    assert fake.requests == []


def test_with_a_sandbox_id_run_command_is_not_one_off(fake):
    fake.add()
    _, data, _ = outputs(invoke(tool("run_command"), {"sandbox_id": "sbx_00000000000000aa", "command": "x"}))
    assert data["one_off"] is False and data["sandbox_id"] == "sbx_00000000000000aa"
    assert "DELETE" not in [r.method for r in fake.requests]


URL = "https://files.dify.example/files/abc/file-preview?timestamp=1&sign=SECRETSIGNATURE"


def test_upload_file_puts_a_workflow_file_in_the_workdir(fake, files):
    fake.add()
    sid = "sbx_00000000000000aa"
    files.content[URL] = b"a,b\n1,2\n"
    _, data, _ = outputs(invoke(tool("upload_file"), {"sandbox_id": sid, "file": StubFile(URL, "data.csv")}))
    assert data == {"sandbox_id": sid, "path": "/home/prized/work/data.csv", "size": 8}
    assert fake.files[(sid, "/home/prized/work/data.csv")] == b"a,b\n1,2\n"
    assert "authorization" not in files.requests[0].headers  # the Prized key never goes to the file host


@pytest.mark.parametrize(
    "filename,path,expected",
    [
        ("r.pdf", "in/", "/home/prized/work/in/r.pdf"),
        ("r.pdf", "/home/prized/x.pdf", "/home/prized/x.pdf"),
        ("../../etc/passwd", None, "/home/prized/work/passwd"),
        ("..", None, "/home/prized/work/upload"),
        (None, None, "/home/prized/work/upload"),
    ],
)
def test_upload_file_paths(fake, files, filename, path, expected):
    fake.add()
    files.content[URL] = b"x"
    params = {"sandbox_id": "sbx_00000000000000aa", "file": StubFile(URL, filename)}
    if path:
        params["path"] = path
    _, data, _ = outputs(invoke(tool("upload_file"), params))
    assert data["path"] == expected


@pytest.mark.parametrize(
    "url,address",
    [
        ("http://127.0.0.1:5001/files/x", None),
        ("http://169.254.169.254/latest/meta-data/", None),
        ("http://[::1]/x", None),
        ("http://10.0.0.5/x", None),
        ("http://api:5001/files/x", "172.18.0.4"),
        ("http://internal.example/x", "10.1.2.3"),
        ("http://localhost.example/x", "127.0.0.1"),
        ("http://metadata.example/x", "169.254.169.254"),
        ("http://v6.example/x", "fd00::1"),
        ("http://cgnat.example/x", "100.64.0.1"),
    ],
)
def test_upload_file_refuses_private_hosts_before_fetching(fake, files, url, address):
    fake.add()
    if address:
        files.addresses[urlsplit(url).hostname] = address
    with pytest.raises(plugin_tool.ToolError, match="public address") as caught:
        invoke(tool("upload_file"), {"sandbox_id": "sbx_00000000000000aa", "file": StubFile(url, "a")})
    assert files.requests == [] and url not in str(caught.value)


def test_upload_file_follows_no_redirect(fake, files):
    fake.add()
    files.status = 302
    with pytest.raises(plugin_tool.ToolError, match="HTTP 302"):
        invoke(tool("upload_file"), {"sandbox_id": "sbx_00000000000000aa", "file": StubFile(URL, "a")})
    assert len(files.requests) == 1


def test_upload_file_stops_reading_at_the_cap(fake, files, monkeypatch):
    fake.add()
    monkeypatch.setattr(pc, "UPLOAD_LIMIT", 1024 * 1024)
    files.content[URL] = b"x" * (1024 * 1024 + 1)
    with pytest.raises(plugin_tool.ToolError, match="over 1 MiB"):
        invoke(tool("upload_file"), {"sandbox_id": "sbx_00000000000000aa", "file": StubFile(URL, "big")})
    assert not any(r.url.path.endswith("/files/upload") for r in fake.requests)


def test_upload_file_errors_never_show_the_file_url(fake, files):
    fake.add()
    sid = "sbx_00000000000000aa"
    with pytest.raises(plugin_tool.ToolError) as caught:  # 404 from the file server
        invoke(tool("upload_file"), {"sandbox_id": sid, "file": StubFile(URL, "a.txt")})
    assert "SECRETSIGNATURE" not in str(caught.value) and "HTTP 404" in str(caught.value)
    with pytest.raises(plugin_tool.ToolError, match="over 50 MiB"):
        invoke(tool("upload_file"), {"sandbox_id": sid, "file": StubFile(URL, "big", size=pc.UPLOAD_LIMIT + 1)})
    with pytest.raises(plugin_tool.ToolError, match="file is required"):
        invoke(tool("upload_file"), {"sandbox_id": sid})
    with pytest.raises(plugin_tool.ToolError, match="not an http"):
        invoke(tool("upload_file"), {"sandbox_id": sid, "file": StubFile("file:///etc/passwd", "p")})


def test_reading_a_missing_file_says_so(fake):
    fake.add()
    with pytest.raises(plugin_tool.ToolError, match="No such file in the sandbox: /home/prized/work/nope.txt"):
        invoke(tool("read_file"), {"sandbox_id": "sbx_00000000000000aa", "path": "nope.txt"})
