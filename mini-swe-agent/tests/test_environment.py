import json
import os

import pytest
from conftest import UNAME, FakeSandbox
from minisweagent.exceptions import Submitted
from prized import PrizedError

from minisweagent_prized import PrizedEnvironment
from minisweagent_prized import environment as env_module


def make(**kwargs):
    return PrizedEnvironment(
        image="docker.io/swebench/sweb.eval.x86_64.demo:latest", interpreter=["bash", "-c"], **kwargs
    )


def test_start_creates_a_labelled_sandbox_and_unpacks_the_image(fake_prized):
    env = make(cwd="/tmp", vcpu=4, memory_mb=8192, disk_gb=40, labels={"run": "r1"}, max_runtime_min=60)
    sandbox = FakeSandbox.created[0]
    assert sandbox.kwargs["template"] == "prized-base-v1"
    assert (sandbox.kwargs["vcpu"], sandbox.kwargs["memory_mb"], sandbox.kwargs["disk_gb"]) == (4, 8192, 40)
    assert sandbox.kwargs["labels"] == {"minisweagent": "1", "run": "r1"}
    assert sandbox.kwargs["max_runtime_min"] == 60
    setup = sandbox.calls[0][0]
    assert "crane pull --platform linux/amd64 docker.io/swebench/sweb.eval.x86_64.demo:latest image.tar" in setup
    assert setup.count("crane pull") == 1 and "crane config" not in setup  # one pull per image
    assert env_module.CRANE_SHA256 in setup
    assert sandbox.files_written[env_module.RUNTIME][0].decode().startswith('"""Run one command inside')
    assert env.image_env["TZ"] == "Etc/UTC"
    assert env.image_env["HOME"] == "/root"  # as docker exec sets it for root
    assert env.image_workdir == "/testbed/"


def test_execute_runs_in_cwd_with_merged_output(fake_prized, tmp_path):
    env = make(cwd=str(tmp_path))
    result = env.execute({"command": "pwd; echo out; echo err >&2; exit 3"})
    assert result == {"output": f"{tmp_path}\nout\nerr\n", "returncode": 3, "exception_info": ""}
    other = tmp_path / "sub"
    other.mkdir()
    assert env.execute({"command": "pwd"}, cwd=str(other))["output"] == f"{other}\n"


def test_a_missing_cwd_fails_like_docker_exec(fake_prized):
    result = make(cwd="/no/such/dir").execute({"command": "echo hi"})
    assert result["returncode"] == 126
    assert "chdir to cwd" in result["output"]


def test_environment_precedence(fake_prized, monkeypatch, tmp_path):
    monkeypatch.setenv("FORWARDED", "from-host")
    monkeypatch.setenv("BOTH", "from-host")
    env = make(
        cwd=str(tmp_path), env={"BOTH": "from-config", "QUOTED": "a 'b' $c"}, forward_env=["FORWARDED", "UNSET_X"]
    )
    out = env.execute({"command": 'echo "$TZ|$HOME|$FORWARDED|$BOTH|$QUOTED|${UNSET_X-unset}"'})["output"]
    assert out == "Etc/UTC|/root|from-host|from-config|a 'b' $c|unset\n"


def test_timeout_returns_partial_output(fake_prized, tmp_path):
    env = make(cwd=str(tmp_path), timeout=7)
    FakeSandbox.created[0].timeout_next = True
    result = env.execute({"command": "sleep 100"})
    assert result["returncode"] == -1
    assert result["output"] == "partial\n"
    assert "timed out after 7 seconds" in result["exception_info"]
    assert FakeSandbox.created[0].calls[-1][1] == 7
    assert env.execute({"command": "true"}, timeout=99)["returncode"] == 0
    assert FakeSandbox.created[0].calls[-1][1] == 99


def test_an_sdk_error_is_returned_not_raised(fake_prized, tmp_path):
    env = make(cwd=str(tmp_path))

    def broken(*args, **kwargs):
        raise PrizedError("network_error", "connection reset")

    env.sandbox.exec = broken
    result = env.execute({"command": "true"})
    assert result["returncode"] == -1
    assert "connection reset" in result["exception_info"]


def test_submission_is_raised(fake_prized, tmp_path):
    env = make(cwd=str(tmp_path))
    with pytest.raises(Submitted) as raised:
        env.execute({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT; echo diff --git a/x b/x"})
    assert raised.value.messages[0]["extra"]["submission"] == "diff --git a/x b/x\n"


def test_long_scripts_are_uploaded(fake_prized, tmp_path):
    env = make(cwd=str(tmp_path))
    big = "x" * (env_module.INLINE_LIMIT + 10)
    result = env.execute({"command": f"echo {big} | wc -c"})
    assert result["output"].strip() == str(len(big) + 1)
    cmd = FakeSandbox.created[0].calls[-1][0]
    assert cmd.startswith(f"python3 {env_module.RUNTIME} {env_module.XFER}/cmd-")


def test_write_file_lands_inside_the_image(fake_prized):
    env = make()
    env.write_file("/tmp/patch.diff", "diff")
    assert FakeSandbox.created[0].files_written[env_module.ROOT + "/tmp/patch.diff"] == (b"diff", 0o644)
    with pytest.raises(ValueError):
        env.write_file("relative", "x")


def test_pull_is_retried_then_fails_and_the_sandbox_is_deleted(fake_prized):
    fake_prized["setup_failures"] = 2
    env = make()
    assert len(FakeSandbox.created[0].calls) == 3  # 2 failures, then success
    env.cleanup()
    fake_prized["setup_failures"] = 5
    with pytest.raises(RuntimeError, match="Could not unpack .* crane export failed"):
        make(pull_attempts=2)
    assert FakeSandbox.created[1].deleted == 1


def test_a_throttled_registry_is_waited_out_with_no_sandbox_held(fake_prized, monkeypatch):
    slept = []
    monkeypatch.setattr(env_module.time, "sleep", slept.append)
    monkeypatch.setattr(env_module.time, "monotonic", lambda: 1000.0)  # sleeps don't advance the clock
    fake_prized["setup_failures"] = 4
    fake_prized["setup_error"] = (
        "GET https://index.docker.io/v2/...: TOOMANYREQUESTS: You have reached your pull rate limit."
    )
    env = make(pull_attempts=1)
    assert slept == [30.0, 30.0, 60.0, 120.0]
    assert env.timings["pull_throttled_s"] == 240
    # A fresh sandbox per try: each throttled one is deleted before the wait.
    assert len(FakeSandbox.created) == 5
    assert [sb.deleted for sb in FakeSandbox.created] == [1, 1, 1, 1, 0]
    assert env.sandbox is FakeSandbox.created[-1]
    # The backoff is shared: another environment waits before creating its sandbox.
    env_module._REGISTRY_RETRY_AT[0] = 1000.0 + 45
    slept.clear()
    make(pull_attempts=1)
    assert slept == [45.0]
    env_module._REGISTRY_RETRY_AT[0] = 0.0
    fake_prized["setup_failures"] = 100
    with pytest.raises(RuntimeError, match="kept rate limiting pulls"):
        make(pull_attempts=1, pull_rate_limit_wait=100)
    assert all(sb.deleted == 1 for sb in FakeSandbox.created[6:])


def test_auth_errors_carry_the_sign_up_hint(fake_prized):
    fake_prized["raise"] = PrizedError("invalid_api_key", "bad key")
    with pytest.raises(RuntimeError, match="auth.md"):
        make()


def test_cleanup_deletes_once(fake_prized):
    env = make()
    env.cleanup()
    env.cleanup()
    del env
    assert FakeSandbox.created[0].deleted == 1
    assert FakeSandbox.created[0].closed == 1


def test_template_vars_describe_the_sandbox(fake_prized):
    env = make()
    tv = env.get_template_vars(extra=1)
    assert (tv["system"], tv["release"], tv["machine"]) == ("Linux", "6.1.119", "x86_64")
    assert tv["version"] == UNAME.split(" ", 3)[3].rpartition(" ")[0]
    assert tv["image"].endswith("demo:latest") and tv["extra"] == 1
    data = env.serialize()["info"]["config"]
    assert data["environment_type"] == "minisweagent_prized.environment.PrizedEnvironment"
    json.dumps(data)


def test_a_non_root_user_in_the_image_is_reported(fake_prized, monkeypatch, caplog):
    import conftest

    monkeypatch.setitem(conftest.IMAGE_CONFIG["config"], "User", "app")
    make()
    assert "commands run as root" in caplog.text


def test_mini_swe_agent_finds_the_class_by_path(fake_prized):
    from minisweagent.environments import get_environment

    env = get_environment(
        {"environment_class": "minisweagent_prized.PrizedEnvironment", "image": "alpine", "container_timeout": "2h"}
    )
    assert isinstance(env, PrizedEnvironment)  # Docker-only keys from shared configs are ignored


def test_no_key_reaches_the_tests():
    assert "PRIZED_API_KEY" not in os.environ
