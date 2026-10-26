"""Behaviour tests for the Prized sandbox backend against an in-process fake API.

The fake edge runs every exec with the host's real bash (see fake_prized.py), so
these tests check what the scripts the backend sends actually do: cwd tracking,
exit codes, staged stdin, background launches, file sync. No network, no key.
"""

import json
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from fake_prized import API, FakePrized


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.credential_files.get_credential_file_mounts", lambda: [])
    return home


@pytest.fixture()
def fake(tmp_path):
    return FakePrized(tmp_path / "fs")


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_env(fake, *, persistent=True, task_id="default", settings=None, clock=None):
    from prized_environment import PrizedEnvironment

    return PrizedEnvironment(
        api_key="dcp_test", cwd="/home/prized/work", timeout=30, persistent_filesystem=persistent,
        task_id=task_id, settings={"api_url": API, **(settings or {})}, transport=fake.transport,
        sleep=lambda _s: None, clock=clock or time.time)


def store(home: Path) -> dict:
    path = home / "prized_sandboxes.json"
    return json.loads(path.read_text()) if path.exists() else {}


# --- create and identity ---------------------------------------------------

def test_create_sends_shape_labels_and_an_idempotency_key(fake, isolated_home):
    env = make_env(fake, settings={"labels": {"team": "ml"}, "vcpu": 4, "memory_mb": 8192})
    (create,) = fake.creates
    body = create["body"]
    assert body["template"] == "prized-base-v1"
    assert body["resources"] == {"vcpu": 4, "memoryMb": 8192}
    assert body["autoPauseMin"] == 10
    assert "maxRuntimeMin" not in body  # the account's maximum applies
    assert body["labels"]["team"] == "ml" and body["labels"]["hermes"] == "1"
    assert body["labels"]["hermes_task"] == "instance"
    assert create["params"] == {"wait": "running"}
    assert create["headers"]["idempotency-key"].startswith("hermes-")
    assert create["headers"]["authorization"] == "Bearer dcp_test"
    assert store(isolated_home) == {"instance": {"id": env.sandbox_id}}
    env.cleanup()


def test_persistent_env_resumes_the_stored_paused_sandbox_instead_of_creating(fake):
    first = make_env(fake)
    sid = first.sandbox_id
    first.cleanup()
    assert fake.sandboxes[sid]["state"] == "paused"
    second = make_env(fake)
    assert second.sandbox_id == sid
    assert len(fake.creates) == 1
    assert f"/api/v1/sandboxes/{sid}/resume" in fake.paths("POST")
    second.cleanup()


def test_stored_sandbox_of_another_task_or_home_is_never_adopted(fake, isolated_home):
    fake.add("sbx_foreign", state="paused", labels={"hermes": "1", "hermes_home": "other", "hermes_task": "instance"})
    (isolated_home / "prized_sandboxes.json").write_text(json.dumps({"instance": {"id": "sbx_foreign"}}))
    env = make_env(fake)
    assert env.sandbox_id != "sbx_foreign"
    assert fake.sandboxes["sbx_foreign"]["state"] == "paused"  # untouched
    assert store(isolated_home)["instance"]["id"] == env.sandbox_id
    env.cleanup()


def test_a_deleted_stored_sandbox_is_replaced_forgotten_and_reported(fake, isolated_home):
    (isolated_home / "prized_sandboxes.json").write_text(json.dumps({"instance": {"id": "sbx_gone"}}))
    env = make_env(fake)
    assert env.sandbox_id != "sbx_gone"
    assert store(isolated_home)["instance"]["id"] == env.sandbox_id
    assert env.execute("true")["environment_recreated"] is True
    assert "environment_recreated" not in env.execute("true")
    env.cleanup()


def test_a_first_sandbox_is_not_reported_as_recreated(fake):
    env = make_env(fake)
    assert "environment_recreated" not in env.execute("true")
    env.cleanup()


def test_cleanup_pauses_a_persistent_sandbox_and_deletes_an_ephemeral_one(fake, isolated_home):
    keep, drop = make_env(fake), make_env(fake, persistent=False, task_id="eph")
    keep_id, drop_id = keep.sandbox_id, drop.sandbox_id
    assert fake.creates[1]["body"]["labels"]["hermes_ephemeral"] == "1"
    keep.cleanup()
    drop.cleanup()
    assert fake.sandboxes[keep_id]["state"] == "paused"
    assert fake.sandboxes[drop_id]["state"] == "deleted"
    assert "eph" not in store(isolated_home)


def test_instance_scope_shares_one_sandbox_and_pauses_it_only_when_the_last_session_lets_go(fake):
    a, b = make_env(fake, task_id="session-a"), make_env(fake, task_id="session-b")
    assert a.sandbox_id == b.sandbox_id and len(fake.creates) == 1
    sid = a.sandbox_id
    a.cleanup()
    assert fake.sandboxes[sid]["state"] == "running"  # b may be mid-command
    assert b.execute("echo still here")["output"].strip() == "still here"
    b.cleanup()
    assert fake.sandboxes[sid]["state"] == "paused"


def test_session_scope_gives_each_task_its_own_sandbox(fake, isolated_home):
    a = make_env(fake, task_id="session-a", settings={"scope": "session"})
    b = make_env(fake, task_id="session-b", settings={"scope": "session"})
    assert a.sandbox_id != b.sandbox_id
    assert set(store(isolated_home)) == {"session-a", "session-b"}
    a.cleanup()
    b.cleanup()


def test_cleanup_pulls_back_files_the_agent_changed_before_pausing(fake, tmp_path, monkeypatch):
    skill = tmp_path / "skills" / "notes" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("v1")
    remote = "/home/prized/.hermes/skills/notes/SKILL.md"
    import prized_environment

    monkeypatch.setattr(prized_environment, "iter_sync_files", lambda _base: [(str(skill), remote)])
    env = make_env(fake)
    assert (fake.root / ".hermes/skills/notes/SKILL.md").read_text() == "v1"
    env.execute(f"printf v2 > {remote}")
    sid = env.sandbox_id
    env.cleanup()
    assert skill.read_text() == "v2"
    assert fake.sandboxes[sid]["state"] == "paused"


# --- exec --------------------------------------------------------------------

def test_commands_keep_cwd_merge_stderr_and_return_the_exit_code(fake):
    env = make_env(fake)
    env.execute("mkdir -p proj && cd proj")
    result = env.execute("pwd; echo out; echo err >&2; exit 7")
    assert result["returncode"] == 7
    assert result["output"].split() == ["/home/prized/work/proj", "out", "err"]
    env.cleanup()


def test_stdin_is_staged_under_home_and_read_by_the_command(fake):
    env = make_env(fake)
    result = env.execute("cat | tr a-z A-Z", stdin_data="hello sandbox\n")
    assert result["output"].strip() == "HELLO SANDBOX"
    uploads = [r for r in fake.requests if r.url.path.endswith("/files/upload")]
    assert uploads and uploads[-1].url.params["path"].startswith("/home/prized/.cache/hermes/stdin-")
    assert not list((fake.root / ".cache" / "hermes").glob("stdin-*"))  # the shell unlinked it
    env.cleanup()


def test_a_background_child_does_not_hold_the_answer(fake):
    """The edge waits for its output pipe to close; a child holding it would delay every launch."""
    from prized_environment import _exec_script

    script = _exec_script("( sleep 30 ) & echo started", login=False, pid_path="/tmp/hermes-prized-test")
    started = time.monotonic()
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = proc.communicate(timeout=20)
    assert out.decode().strip() == "started"
    assert time.monotonic() - started < 5


def test_answers_where_nothing_ran_are_retried(fake):
    env = make_env(fake)
    fake.exec_answers = [httpx.Response(503, json={"error": "sandbox_not_ready"}, headers={"Retry-After": "1"}),
                         httpx.Response(429, json={"error": "rate_limited"})]
    assert env.execute("echo ok")["output"].strip() == "ok"
    env.cleanup()


def test_an_unreachable_sandbox_is_reported_not_retried(fake):
    env = make_env(fake)
    before = len(fake.exec_scripts)
    fake.exec_answers = [httpx.Response(502, json={"error": "sandbox_unreachable"})]
    result = env.execute("touch once")
    assert result["returncode"] == 1
    assert "may or may not have run" in result["output"]
    assert len(fake.exec_scripts) == before + 1
    env.cleanup()


# --- the lifetime cap -------------------------------------------------------

def test_past_expires_at_the_next_command_runs_in_a_fresh_sandbox_and_says_so(fake, isolated_home):
    clock = Clock()
    fake.expires_at = "1970-01-12T13:46:40Z"  # 1_000_000 s after the epoch
    clock.now = 1_000_000 - 3600
    env = make_env(fake, clock=clock)
    old = env.sandbox_id
    assert "environment_recreated" not in env.execute("echo before")
    clock.now = 1_000_000 + 1
    fake.expires_at = "2999-01-01T00:00:00Z"
    result = env.execute("echo after")
    assert result["output"].strip() == "after"
    assert result["environment_recreated"] is True
    assert env.sandbox_id != old
    assert store(isolated_home)["instance"]["id"] == env.sandbox_id
    env.cleanup()


def test_a_sandbox_deleted_underneath_is_reported_then_replaced(fake):
    env = make_env(fake)
    old = env.sandbox_id
    fake.sandboxes[old]["state"] = "deleted"
    gone = env.execute("echo hi")
    assert gone["returncode"] == 1 and "sandbox is gone" in gone["output"]
    again = env.execute("echo hi")
    assert again["output"].strip() == "hi" and again["environment_recreated"] is True
    assert env.sandbox_id != old
    env.cleanup()


def test_a_stored_sandbox_near_its_deletion_is_not_adopted(fake, isolated_home):
    fake.add("sbx_old", state="paused", expires_at="1970-01-12T13:46:40Z",
             labels={"hermes": "1", "hermes_task": "instance", "hermes_home": _digest()})
    (isolated_home / "prized_sandboxes.json").write_text(json.dumps({"instance": {"id": "sbx_old"}}))
    env = make_env(fake, clock=Clock(1_000_000 - 30))
    assert env.sandbox_id != "sbx_old"
    env.cleanup()


def _digest():
    from prized_environment import _home_digest

    return _home_digest()


# --- interrupt ---------------------------------------------------------------

def test_cancel_kills_the_command_s_process_group(fake):
    env = make_env(fake)
    marker = fake.root / "survived"
    result = {}

    def run():
        result.update(env.execute(f"sleep 5; touch {marker}", timeout=20))

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(1.0)
    from tools.interrupt import set_interrupt

    set_interrupt(True, worker.ident)
    worker.join(timeout=20)
    set_interrupt(False, worker.ident)
    assert result["returncode"] == 130
    assert any("kill -TERM" in s for s in fake.exec_scripts)
    time.sleep(5)
    assert not marker.exists()
    env.cleanup()


# --- file sync -----------------------------------------------------------------

def test_bulk_upload_unpacks_files_at_their_remote_paths_and_download_round_trips(fake, tmp_path):
    env = make_env(fake)
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("alpha")
    b.write_text("beta")
    env._bulk_upload([(str(a), "/home/prized/.hermes/skills/x/a.txt"), (str(b), "/home/prized/.hermes/b.txt")])
    assert (fake.root / ".hermes/skills/x/a.txt").read_text() == "alpha"
    assert (fake.root / ".hermes/b.txt").read_text() == "beta"
    dest = tmp_path / "back.tar"
    env._bulk_download(dest)
    with tarfile.open(dest) as tar:
        names = tar.getnames()
    assert "home/prized/.hermes/skills/x/a.txt" in names
    env.cleanup()


def test_uploads_outside_home_are_refused_before_any_request(fake, tmp_path):
    env = make_env(fake)
    f = tmp_path / "f"
    f.write_text("x")
    with pytest.raises(RuntimeError, match="only sync files under /home/prized"):
        env._bulk_upload([(str(f), "/etc/f")])
    env.cleanup()


# --- settings ------------------------------------------------------------------

@pytest.mark.parametrize("raw, match", [
    ({"vcpu": 0}, "vcpu"),
    ({"vcpu": True}, "vcpu"),
    ({"memory_mb": 512}, "memory_mb"),
    ({"labels": {"hermes_x": "1"}}, "hermes"),
    ({"labels": ["a"]}, "labels"),
    ({"api_url": "http://api.prized.dev"}, "https"),
    ({"template": "a b"}, "template"),
])
def test_invalid_settings_fail_closed(raw, match):
    from prized_environment import resolve_settings

    with pytest.raises(ValueError, match=match):
        resolve_settings(raw)


def test_unset_settings_use_the_documented_defaults():
    from prized_environment import resolve_settings

    s = resolve_settings({"vcpu": None, "memory_mb": ""})
    assert (s["template"], s["vcpu"], s["memory_mb"], s["auto_pause_min"]) == ("prized-base-v1", 2, 4096, 10)
    assert s["api_url"] == "https://api.prized.dev/api/v1"
