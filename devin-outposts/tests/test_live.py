"""Live tests: real Prized sandboxes, with the fake Outposts service standing in for Devin.

Opt-in: ``PRIZED_LIVE=1`` and ``PRIZED_API_KEY`` (Sandboxes: Write) in the environment of the test
process only; no key goes into a sandbox. Every sandbox carries ``task=devin-outposts`` plus a
run label and is deleted at the end. ``PRIZED_LIVE_REPORT=path`` writes the timings as JSON.

- ``test_session_lifecycle_on_prized``: claim -> create -> worker -> sleep (pause with memory) ->
  wake (resume, same sandbox, same processes) -> terminate (delete). A real sandbox can't reach the
  test machine, so the fake remote runs in file:// gateway mode and the test plays the gateway.
- ``test_new_sessions_fork_a_live_warm_parent``: a session forked from a paused parent starts
  with the parent's running process.
- ``test_real_devin_remote_starts_in_a_sandbox``: Cognition's own devin-remote, downloaded from
  static.devin.ai and checked against its sha256 inside the sandbox, launched by the same scripts
  with an obviously fake token, against a capture listener on the sandbox's own loopback.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

import pytest
from conftest import OUTPOST, REMOTE
from fake_outposts import TOKEN, FakeOutposts

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("PRIZED_LIVE") != "1" or not os.environ.get("PRIZED_API_KEY"),
        reason="live tests need PRIZED_LIVE=1 and PRIZED_API_KEY",
    ),
]

RUN = f"live-{secrets.token_hex(3)}"
LABELS = {"task": "devin-outposts", "run": RUN}
REPORT: dict[str, object] = {"run": RUN}


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    from prized import Sandbox

    deleted = Sandbox.delete_by_labels({"run": RUN})
    REPORT["cleanup_deleted"] = deleted
    for _ in range(60):
        if not Sandbox.list(labels={"run": RUN}):
            break
        time.sleep(2)
    REPORT["left_after_cleanup"] = [sb.id for sb in Sandbox.list(labels={"run": RUN})]
    if os.environ.get("PRIZED_LIVE_REPORT"):
        Path(os.environ["PRIZED_LIVE_REPORT"]).write_text(json.dumps(REPORT, indent=2, default=str))


def orchestrator(fake: FakeOutposts, warm_parent: str | None = None):
    from prized_outposts.devin.api import OutpostsAPI
    from prized_outposts.devin.orchestrator import Orchestrator
    from prized_outposts.devin.plan import Rules
    from prized_outposts.devin.remote import RemoteSource
    from prized_outposts.machines import Machines, MachineSpec

    spec = MachineSpec(
        vcpu=2,
        memory_gb=4,
        max_runtime_min=60,
        auto_pause_min=10,
        auto_delete_min=60,
        warm_parent=warm_parent,
        labels=LABELS,
    )
    return Orchestrator(
        OutpostsAPI(fake.url, TOKEN, f"prized-{RUN}"),
        Machines(spec, owner="prized-devin-outposts"),
        RemoteSource(local_file=REMOTE),
        outpost_id=OUTPOST,
        rules=Rules(acceptor_id=f"prized-{RUN}"),
        poll_interval=1.0,
        sweep_interval=3600,
    )


def until(orch, predicate, timeout=180.0, step=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        orch.tick()
        orch.drain(300)
        if predicate():
            return
        time.sleep(step)
    raise AssertionError(f"timed out; events: {orch.events[-12:]}")


def sh(sb, cmd: str) -> str:
    result = sb.exec(cmd, timeout=60)
    return result.stdout.strip()


def attached_hash(sb, sid: str) -> str:
    return sh(sb, f"cat ~/.devin/worker/sessions/{sid}/attached 2>/dev/null || true")


def token_hash(fake: FakeOutposts, sid: str) -> str:
    return hashlib.sha256(fake.row(sid).tokens[-1].encode()).hexdigest()


def test_session_lifecycle_on_prized():
    from prized import Sandbox

    fake = FakeOutposts(remote_file=REMOTE)
    fake.gateway_url = "file:///gateway"
    orch = orchestrator(fake)
    sid = f"devin-{RUN}-a"
    try:
        t0 = time.monotonic()
        fake.enqueue(sid)
        until(
            orch,
            lambda: sid in orch.sandboxes and orch.tracks.get(sid) and orch.tracks[sid].remote == "alive",
        )
        sb = orch.sandboxes[sid]
        until(orch, lambda: attached_hash(sb, sid) == token_hash(fake, sid))
        fake.mark_running(sid)
        REPORT["new_session_to_worker_s"] = round(time.monotonic() - t0, 2)
        REPORT["create"] = [e for e in orch.events if e[1] == "created"]

        assert sb.labels["devin-session"] == sid and sb.labels["task"] == "devin-outposts"
        keys = set(json.loads(sh(sb, f"cat ~/.devin/worker/sessions/{sid}/env-keys.json")))
        assert not any(k.startswith(("PRIZED", "PO_")) for k in keys), keys
        launch = json.loads(sh(sb, f"cat ~/.devin/worker/sessions/{sid}/launch.json"))
        assert launch["cwd"] == "/home/prized/work" and launch["uid"] != 0
        assert sh(sb, f"grep -rl {TOKEN} ~ 2>/dev/null | head -1; true") == ""
        REPORT["remote_env_keys"] = sorted(keys)

        pid = sh(sb, f"cat ~/.devin/worker/sessions/{sid}/devserver.pid")
        before = int(sh(sb, f"cat ~/.devin/worker/sessions/{sid}/devserver.count"))

        # Sleep: Devin tells the remote, it exits 0, the orchestrator pauses the sandbox and releases.
        sh(sb, f"echo sleep > ~/.devin/worker/sessions/{sid}/gateway-action")
        fake.suspend(sid)
        t1 = time.monotonic()
        until(orch, lambda: fake.row(sid).phase == "pending" and not orch.tracks)
        REPORT["suspend_to_paused_s"] = round(time.monotonic() - t1, 2)
        [listed] = Sandbox.list(labels={"devin-session": sid, "run": RUN})
        assert listed.state == "paused"
        time.sleep(5)

        # Wake: claimed again, the same sandbox resumes, a new worker gets the new token.
        fake.wake(sid)
        t2 = time.monotonic()
        until(orch, lambda: orch.tracks.get(sid) and orch.tracks[sid].remote == "alive")
        sb = orch.sandboxes[sid]
        until(orch, lambda: attached_hash(sb, sid) == token_hash(fake, sid), step=0.2)
        REPORT["wake_to_worker_s"] = round(time.monotonic() - t2, 2)
        REPORT["resume"] = [e for e in orch.events if e[1] == "resumed"]
        assert sb.id == listed.id
        assert sh(sb, f"cat ~/.devin/worker/sessions/{sid}/devserver.pid") == pid, (
            "same process after the wake"
        )
        after = int(sh(sb, f"cat ~/.devin/worker/sessions/{sid}/devserver.count"))
        assert after > before, "the process kept its count (memory) and kept going"
        REPORT["devserver_count_before_after"] = [before, after]
        assert sh(sb, f"cat ~/.devin/worker/sessions/{sid}/launches") == "2"
        assert sh(sb, f"ls ~/work/.devin-outpost-stamps/{sid}") != ""

        # Terminate: the sandbox is deleted.
        sh(sb, f"echo terminate > ~/.devin/worker/sessions/{sid}/gateway-action")
        fake.terminate(sid)
        until(orch, lambda: not Sandbox.list(labels={"devin-session": sid, "run": RUN}))
        REPORT["events_a"] = orch.events
    finally:
        orch.close()
        fake.close()


def test_new_sessions_fork_a_live_warm_parent():
    from prized import Sandbox

    from prized_outposts.machines import Machines, MachineSpec

    fake = FakeOutposts(remote_file=REMOTE)
    fake.gateway_url = "file:///gateway"
    spec = MachineSpec(
        vcpu=2, memory_gb=4, max_runtime_min=60, auto_pause_min=10, auto_delete_min=60, labels=LABELS
    )

    def prepare(sb):
        sb.exec("mkdir -p ~/work/repos/app && echo warm > ~/work/repos/app/README", timeout=60)
        sb.exec("echo $$ > ~/parent-pid; exec sleep 1000000", background=True)
        time.sleep(1)

    parent = Machines(spec, owner="prized-devin-outposts").make_parent(prepare)
    orch = orchestrator(fake, warm_parent=parent.id)
    sid = f"devin-{RUN}-b"
    try:
        t0 = time.monotonic()
        fake.enqueue(sid)
        until(
            orch,
            lambda: sid in orch.sandboxes and orch.tracks.get(sid) and orch.tracks[sid].remote == "alive",
        )
        child = orch.sandboxes[sid]
        REPORT["fork_session_to_worker_s"] = round(time.monotonic() - t0, 2)
        REPORT["fork"] = [e for e in orch.events if e[1] == "forked"]
        assert child.id != parent.id
        assert sh(child, "cat ~/work/repos/app/README") == "warm"
        parent_pid = sh(child, "cat ~/parent-pid").split()[0]
        assert sh(child, f"kill -0 {parent_pid} && echo running") == "running", (
            "the parent's process runs in the copy"
        )
        sh(child, f"echo terminate > ~/.devin/worker/sessions/{sid}/gateway-action")
        fake.terminate(sid)
        until(orch, lambda: not Sandbox.list(labels={"devin-session": sid, "run": RUN}))
    finally:
        orch.close()
        fake.close()
        parent.delete()


GATEWAY_CAPTURE = r"""
import json, socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 8099)); s.listen(8)
out = open(sys.argv[1], "a")
while True:
    c, _ = s.accept(); c.settimeout(5); data = b""
    try:
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = c.recv(4096)
            if not chunk: break
            data += chunk
    except Exception: pass
    head = data.split(b"\r\n\r\n")[0].decode("latin-1")
    redact = lambda l: "Authorization: <redacted>" if l.lower().startswith("authorization") else l
    lines = [redact(l) for l in head.split("\r\n")][:30]
    out.write(json.dumps({"bytes": len(data), "tls": data[:1] == b"\x16", "head": lines}) + "\n")
    out.flush()
    c.close()
"""


def test_real_devin_remote_starts_in_a_sandbox():
    from prized import Sandbox

    from prized_outposts.devin import remote as rm

    binary = rm.RemoteSource().resolve(None)
    REPORT["devin_remote"] = {"sha": binary.sha, "digest": binary.digest}
    sb = Sandbox.create(
        vcpu=2,
        memory_gb=4,
        max_runtime_min=30,
        auto_pause_min=10,
        auto_delete_min=30,
        labels={**LABELS, "devin-session": "devin-smoke"},
    )
    sid = "devin-smoke"
    try:
        t0 = time.monotonic()
        prep = sb.exec(rm.prepare_script(sid, binary, "/home/prized/work"), timeout=600)
        REPORT["remote_prepare"] = {
            "exit": prep.exit_code,
            "stdout": prep.stdout.strip()[-200:],
            "stderr": prep.stderr.strip()[-300:],
            "s": round(time.monotonic() - t0, 2),
        }
        assert prep.exit_code == 0, prep.stderr
        sb.files.write("/home/prized/capture.py", GATEWAY_CAPTURE)
        sb.exec("python3 ~/capture.py ~/gateway.jsonl", background=True)
        time.sleep(1)
        env = rm.launch_env(
            sid,
            gateway_url="ws://127.0.0.1:8099",
            connect_token="fake-connect-token-smoke-test",
            binary_path=f"{rm.HOME}/.devin/worker/cache/{binary.filename}",
            workdir="/home/prized/work",
        )
        sb.exec(rm.launch_command(sid), env=env, background=True)
        time.sleep(15)
        status = rm.parse_status(sb.exec(rm.status_script(sid), timeout=30).stdout)
        log = sh(
            sb, f"tail -c 3000 {rm.run_dir(sid)}/remote.log | sed 's/fake-connect-token-smoke-test/<token>/g'"
        )
        capture = sh(sb, "cat ~/gateway.jsonl 2>/dev/null | head -5")
        state = sh(sb, f"ls -la ~/.devin/worker/sessions/{sid} 2>&1 | head -20; id")
        REPORT["real_remote"] = {
            "status": status,
            "log_tail": log,
            "gateway_capture": capture,
            "state_dir": state,
        }
        assert status[0] in ("alive", "exited"), status
    finally:
        sb.delete()
