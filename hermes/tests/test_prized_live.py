"""Live checks against the real Prized API. Skipped unless PRIZED_LIVE_TESTS=1 and PRIZED_API_KEY are set.

Each test creates its own small sandbox (1 vCPU, 2 GB) and deletes it. Run from a hermes-agent checkout:

    PRIZED_LIVE_TESTS=1 PRIZED_API_KEY=dcp_... python -m pytest /path/to/integrations/hermes/tests/test_prized_live.py -v

(``scripts/run_tests.sh`` clears the environment, so it always skips these.)
"""

import os
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("PRIZED_LIVE_TESTS") != "1" or not os.environ.get("PRIZED_API_KEY"),
    reason="live Prized API tests are opt-in (PRIZED_LIVE_TESTS=1 and PRIZED_API_KEY)")

LABELS = {k: v for k, v in (p.split("=", 1) for p in os.environ.get("PRIZED_LIVE_LABELS", "").split(",") if "=" in p)}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.credential_files.get_credential_file_mounts", lambda: [])
    return home


def make_env(persistent=True, **settings):
    from prized_environment import PrizedEnvironment

    return PrizedEnvironment(api_key=os.environ["PRIZED_API_KEY"], timeout=60, persistent_filesystem=persistent,
                             settings={"vcpu": 1, "memory_mb": 2048, "labels": LABELS, **settings})


def test_a_background_server_survives_release_and_a_new_environment_resumes_it():
    first = make_env()
    sid = first.sandbox_id
    try:
        first.execute("nohup python3 -m http.server 8765 >/dev/null 2>&1 </dev/null & echo $! > ~/srv.pid")
        first.cleanup()  # pauses: memory and processes kept
        from prized_environment import PrizedClient

        assert PrizedClient(os.environ["PRIZED_API_KEY"]).get(sid)["state"] in ("pausing", "paused")
        second = make_env()
        assert second.sandbox_id == sid
        out = second.execute("kill -0 $(cat ~/srv.pid) && curl -s -o /dev/null -w '%{http_code}' localhost:8765/")
        assert out["returncode"] == 0 and out["output"].strip().endswith("200")
    finally:
        from prized_environment import PrizedClient

        PrizedClient(os.environ["PRIZED_API_KEY"]).delete(sid)


def test_interrupt_kills_the_command_and_leaves_the_sandbox_running():
    env = make_env(persistent=False)
    result = {}

    def run():
        result.update(env.execute("sleep 20; touch ~/finished", timeout=60))

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(2)
    from tools.interrupt import set_interrupt

    set_interrupt(True, worker.ident)
    worker.join(timeout=30)
    set_interrupt(False, worker.ident)
    try:
        assert result["returncode"] == 130
        time.sleep(20)
        check = env.execute("ls ~/finished 2>/dev/null; pgrep -x sleep || echo none")
        assert check["output"].split() == ["none"]
    finally:
        env.cleanup()


def test_past_the_lifetime_cap_the_next_command_gets_a_fresh_sandbox_and_is_told():
    env = make_env(persistent=True, max_runtime_min=1)
    old = new = env.sandbox_id
    try:
        env.execute("echo before > ~/work/marker")
        time.sleep(65)
        result = env.execute("cat ~/work/marker 2>/dev/null || echo gone")
        assert result["output"].strip() == "gone"
        assert result["environment_recreated"] is True
        new = env.sandbox_id
        assert new != old
    finally:
        env.cleanup()
        from prized_environment import PrizedClient

        PrizedClient(os.environ["PRIZED_API_KEY"]).delete(new)
