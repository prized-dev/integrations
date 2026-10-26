"""Every tool against the real Prized API. Opt-in: PRIZED_LIVE=1 and PRIZED_API_KEY.

Creates one small sandbox (2 vCPU, 4 GB, 15 minutes of run time) labelled with
PRIZED_LIVE_LABELS (default test=dify-plugin), billed to the key's workspace,
and deletes it at the end. The key is read from the environment only and is
never sent into the sandbox.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from conftest import StubFile, invoke, outputs, tool

import plugin_tool
import prized_client as pc

KEY = os.environ.get("PRIZED_API_KEY", "")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not (os.environ.get("PRIZED_LIVE") and KEY), reason="set PRIZED_LIVE=1 and PRIZED_API_KEY"),
]


def run(name: str, **params):
    messages = invoke(tool(name), params, key=KEY)
    variables, data, blobs = outputs(messages)
    dump = json.dumps([variables, data], default=str)
    assert KEY not in dump  # the key never comes back in a result
    return variables, data, blobs


def test_every_tool_on_a_real_sandbox(monkeypatch):
    with pc.PrizedClient(KEY) as client:
        client.validate()
    labels = os.environ.get("PRIZED_LIVE_LABELS", "test=dify-plugin")
    monkeypatch.setattr(plugin_tool, "ONE_OFF_LABELS", {**pc.parse_labels(labels), "one-off": "true"})
    timings: dict[str, float] = {}
    sid = None
    try:
        start = time.monotonic()
        created, _, _ = run(
            "create_sandbox", vcpu=2, memory_gb=4, max_runtime_minutes=15, auto_pause_minutes=5, labels=labels
        )
        timings["create"] = time.monotonic() - start
        sid = created["sandbox_id"]
        assert created["state"] == "running" and created["vcpu"] == 2 and created["memory_mb"] == 4096

        run("write_file", sandbox_id=sid, path="main.py", content="print(6 * 7)\n")
        out, _, _ = run("run_command", sandbox_id=sid, command="python3 main.py")
        assert (out["exit_code"], out["stdout"]) == (0, "42\n")

        out, _, _ = run("run_code", sandbox_id=sid, language="python", code="import sys; print(sys.version_info[0])")
        assert out["stdout"] == "3\n"
        out, _, _ = run("run_code", sandbox_id=sid, language="javascript", code="console.log(6 * 7)")
        assert out["stdout"] == "42\n"
        out, _, _ = run("run_code", sandbox_id=sid, language="bash", code="echo $USER; pwd")
        assert out["stdout"] == "prized\n/home/prized/work\n"
        out, _, _ = run("run_command", sandbox_id=sid, command="ls -A | grep -c '^.dify-run-' || true")
        assert out["stdout"] == "0\n"  # each snippet's file was removed after its run
        out, _, _ = run(
            "run_code", sandbox_id=sid, language="python", code="import time; time.sleep(30)", timeout_seconds=2
        )
        assert out["timed_out"] is True
        out, _, _ = run("run_command", sandbox_id=sid, command="ls -A | grep -c '^.dify-run-' || true")
        assert out["stdout"] == "0\n"  # removed after a timeout too

        out, _, _ = run("run_command", sandbox_id=sid, command="echo oops >&2; exit 3")
        assert (out["exit_code"], out["stderr"]) == (3, "oops\n")
        out, _, _ = run("run_command", sandbox_id=sid, command="sleep 10", timeout_seconds=2)
        assert out["timed_out"] is True and out["exit_code"] == -1
        out, _, _ = run("run_command", sandbox_id=sid, command="env | grep -c dcp_ || true")
        assert out["stdout"] == "0\n"  # no key in the sandbox's environment

        # A public file URL stands in for Dify's file server.
        up, _, _ = run(
            "upload_file", sandbox_id=sid, file=StubFile("https://prized.dev/llms.txt", "llms.txt"), path="in/"
        )
        assert up["path"] == "/home/prized/work/in/llms.txt" and up["size"] > 0
        out, _, _ = run("run_command", sandbox_id=sid, command="wc -c < llms.txt", cwd="in")
        assert int(out["stdout"]) == up["size"]

        text, _, _ = run("read_file", sandbox_id=sid, path="/home/prized/work/main.py")
        assert text["content"] == "print(6 * 7)\n" and text["truncated"] is False
        run("run_code", sandbox_id=sid, language="python", code="open('b.bin','wb').write(bytes(range(256)))")
        data, _, blobs = run("read_file", sandbox_id=sid, path="b.bin", output="file")
        assert blobs[0][0] == bytes(range(256)) and data["size"] == 256

        with pytest.raises(plugin_tool.ToolError, match="inside /home/prized"):
            run("read_file", sandbox_id=sid, path="/etc/passwd")

        start = time.monotonic()
        paused, _, _ = run("pause_sandbox", sandbox_id=sid)
        timings["pause"] = time.monotonic() - start
        assert paused["state"] in ("paused", "pausing")
        _wait(sid, "paused")
        start = time.monotonic()
        out, _, _ = run("run_command", sandbox_id=sid, command="cat main.py")  # wakes it
        timings["command on a paused sandbox"] = time.monotonic() - start
        assert out["stdout"] == "print(6 * 7)\n"
        run("pause_sandbox", sandbox_id=sid)
        _wait(sid, "paused")
        start = time.monotonic()
        resumed, _, _ = run("resume_sandbox", sandbox_id=sid)
        timings["resume"] = time.monotonic() - start
        assert resumed["state"] in ("running", "starting")
        got, _, _ = run("get_sandbox", sandbox_id=sid)
        assert got["sandbox_id"] == sid

        deleted, _, _ = run("delete_sandbox", sandbox_id=sid)
        assert deleted["state"] in ("deleting", "deleted")
        sid = None

        start = time.monotonic()
        one, _, _ = run("run_code", language="python", code="print(6 * 7)")
        timings["one-off run_code (create, run, delete)"] = time.monotonic() - start
        assert one["stdout"] == "42\n" and one["one_off"] is True
    finally:
        if sid:
            with pc.PrizedClient(KEY) as client:
                client.delete(sid)
        print("timings (s):", {k: round(v, 2) for k, v in timings.items()})


def _wait(sid: str, state: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        got, _, _ = run("get_sandbox", sandbox_id=sid)
        if got["state"] == state:
            return
        time.sleep(1)
    raise AssertionError(f"{sid} did not reach {state}")
