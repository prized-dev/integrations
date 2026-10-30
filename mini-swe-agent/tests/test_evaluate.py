"""The SWE-bench evaluator against a fake environment: the same steps and logs as swebench's
own run_instance, graded by swebench itself."""

import json
import re
from dataclasses import dataclass
from typing import ClassVar

import pytest
from prized import PrizedError

from minisweagent_prized import evaluate

INSTANCE = {
    "instance_id": "demo__demo-1",
    "image": "swebench/sweb.eval.x86_64.demo_1776_demo-1:latest",
    "repo": "demo/demo",
    "version": "1.0",
    "FAIL_TO_PASS": json.dumps(["tests/test_x.py::test_fixed"]),
    "PASS_TO_PASS": json.dumps(["tests/test_x.py::test_kept"]),
    "log_parser": "parse_log_pytest",
    "eval_type": "pass_and_fail",
    "eval_script": (
        "#!/bin/bash\nset -uxo pipefail\ncd /testbed\n: '>>>>> Start Test Output'\n"
        "pytest -rA tests/test_x.py\n: '>>>>> End Test Output'\n"
    ),
}
PRED = {"instance_id": "demo__demo-1", "model_name_or_path": "org/model", "model_patch": "diff --git a/x b/x\n"}
PASSING = ">>>>> Start Test Output\nPASSED tests/test_x.py::test_fixed\nPASSED tests/test_x.py::test_kept\n>>>>> End Test Output\n"


@dataclass
class Result:
    stdout: str
    stderr: str = ""
    exit_code: int = 0


class FakeEnv:
    scenario: ClassVar[dict] = {}
    made: ClassVar[list] = []

    def __init__(self, *, image, cwd, logger, labels, **options):
        if FakeEnv.scenario.get("quota_fails"):
            FakeEnv.scenario["quota_fails"] -= 1
            raise PrizedError("sandbox_quota_exceeded", "The workspace is at a sandbox quota.", reason="retained_disk")
        if FakeEnv.scenario.get("start_fails"):
            FakeEnv.scenario["start_fails"] -= 1
            raise RuntimeError("Could not unpack image: TOOMANYREQUESTS")
        self.image, self.cwd, self.labels, self.options = image, cwd, labels, options
        self.sandbox = type("S", (), {"id": f"sbx_{len(FakeEnv.made)}"})()
        self.timings = {"startup_s": 1.0}
        self.image_workdir = "/testbed/"
        self.files, self.commands, self.cleaned = {}, [], 0
        FakeEnv.made.append(self)

    def _env(self):
        return {"HOME": "/root"}

    def write_file(self, path, data, mode=0o644):
        self.files[path] = data

    def run_script(self, script, timeout):
        command = re.search(r"exec /bin/bash -c (.*) 2>&1$", script.strip()).group(1).strip("'")
        cwd = re.search(r"cd -- (\S+)", script).group(1)
        self.commands.append((command, cwd, timeout))
        if command.startswith("git apply --verbose /tmp") and FakeEnv.scenario.get("apply_fails"):
            return Result("error: patch failed", exit_code=1)
        if command.startswith(("git apply", "patch ")) and FakeEnv.scenario.get("apply_fails"):
            return Result("error: patch failed", exit_code=1)
        if command == "/bin/bash /eval.sh":
            if FakeEnv.scenario.get("runtime_fails"):
                return Result("", "prized runtime: RuntimeError('could not start the namespace holder')", 125)
            if FakeEnv.scenario.get("api_fails"):
                raise PrizedError("sandbox_error", "the sandbox stopped")
            if FakeEnv.scenario.get("eval_times_out"):
                raise evaluate.CommandTimeoutError(Result("half"), timeout)
            return Result(FakeEnv.scenario.get("test_output", PASSING))
        return Result("")

    def cleanup(self):
        self.cleaned += 1


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(evaluate, "PrizedEnvironment", FakeEnv)
    FakeEnv.scenario, FakeEnv.made = {}, []
    return FakeEnv.scenario


def logs(tmp_path, run_id="r1"):
    return tmp_path / "logs/run_evaluation" / run_id / "org__model" / "demo__demo-1"


def test_resolved_like_the_docker_harness(fake, tmp_path):
    info = evaluate.run_instance(INSTANCE, PRED, "r1", 1800, env_options={"vcpu": 4})
    assert info["status"] == "resolved" and info["resolved"] is True
    env = FakeEnv.made[0]
    assert env.image == INSTANCE["image"] and env.cwd == "/testbed"
    assert env.options == {"vcpu": 4, "max_runtime_min": (900 + 1800) // 60 + 10}  # gone soon after its timeout
    assert env.labels == {"swebench-run": evaluate.label_value("r1")}
    assert env.files["/tmp/patch.diff"] == PRED["model_patch"].encode()
    assert "SWEBENCH_TEST_EXIT_CODE=$?" in env.files["/eval.sh"]  # swebench's own eval script
    commands = [c for c, _, _ in env.commands]
    assert commands[0] == "git apply --verbose /tmp/patch.diff"
    assert ("/bin/bash /eval.sh", "/testbed/", 1800) in env.commands  # the image's WORKDIR, as docker exec
    assert env.cleaned == 1
    d = logs(tmp_path)
    report = json.loads((d / "report.json").read_text())
    assert report["demo__demo-1"]["resolved"] is True
    assert (d / "test_output.txt").read_text() == PASSING
    assert ">>>>> Applied Patch" in (d / "run_instance.log").read_text()
    # A second run reads the report instead of starting a sandbox.
    assert evaluate.run_instance(INSTANCE, PRED, "r1", 1800)["status"] == "cached"
    assert len(FakeEnv.made) == 1


def test_failing_tests_are_unresolved(fake, tmp_path):
    fake["test_output"] = PASSING.replace("PASSED tests/test_x.py::test_fixed", "FAILED tests/test_x.py::test_fixed")
    assert evaluate.run_instance(INSTANCE, PRED, "r1", 1800)["status"] == "unresolved"


def test_patch_that_does_not_apply_is_an_error_after_every_fallback(fake, tmp_path):
    fake["apply_fails"] = True
    info = evaluate.run_instance(INSTANCE, PRED, "r1", 1800)
    assert info["status"] == "error" and "Patch Apply Failed" in info["error"]
    commands = [c for c, _, _ in FakeEnv.made[0].commands]
    assert [c for c in commands if "apply" in c or c.startswith("patch")] == [
        "git apply --verbose /tmp/patch.diff",
        "git apply --verbose --3way /tmp/patch.diff",
        "git apply --verbose --reject /tmp/patch.diff",
        "patch --batch --forward --fuzz=5 -p1 -i /tmp/patch.diff",
        "git apply --check --reverse /tmp/patch.diff",
    ]
    assert commands.count("git checkout -- . ; git clean -fd") == 3
    assert not (logs(tmp_path) / "report.json").exists()


def test_eval_timeout_is_an_error_with_swebench_marker(fake, tmp_path):
    fake["eval_times_out"] = True
    info = evaluate.run_instance(INSTANCE, PRED, "r1", 60)
    assert info["status"] == "error" and "timed out after 60" in info["error"]
    assert (logs(tmp_path) / "test_output.txt").read_text() == "half\n\nTimeout error: 60 seconds exceeded."


def test_a_sandbox_that_does_not_start_is_infra_and_retried(fake, tmp_path):
    fake["start_fails"] = 1
    seen = []
    results = evaluate.run([INSTANCE], {"demo__demo-1": PRED}, "r1", max_workers=2, retries=1, on_result=seen.append)
    assert results[0]["status"] == "resolved"
    assert [a["status"] for a in results[0]["attempts"]] == ["infra", "resolved"]
    assert json.loads((logs(tmp_path) / "prized.json").read_text())["status"] == "resolved"
    assert seen == results


def test_empty_patches_are_not_run_and_a_cap_skips(fake, tmp_path):
    empty = {**PRED, "model_patch": ""}
    assert evaluate.run([INSTANCE], {"demo__demo-1": empty}, "r1") == []
    results = evaluate.run([INSTANCE], {"demo__demo-1": PRED}, "r2", should_start=lambda: False)
    assert results[0]["status"] == "skipped" and FakeEnv.made == []


def test_mirror_image_template():
    spec = type("T", (), {"instance_id": "a__b-1", "image": "swebench/x:latest"})()
    assert evaluate.image_for(spec, None) == "swebench/x:latest"
    assert evaluate.image_for(spec, "ghcr.io/m/{instance_id}:latest") == "ghcr.io/m/a__b-1:latest"


def test_label_values_are_safe_and_distinct():
    assert evaluate.label_value("gold run/1").startswith("gold-run-1-")
    assert evaluate.label_value("gold run/1") != evaluate.label_value("gold-run-1")
    assert len(evaluate.label_value("x" * 100)) == 63


def test_a_full_workspace_is_waited_for_without_spending_retries(fake, tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(evaluate.time, "sleep", slept.append)
    fake["quota_fails"] = 3
    results = evaluate.run([INSTANCE], {"demo__demo-1": PRED}, "r1", retries=0)
    assert results[0]["status"] == "resolved"
    assert [a["status"] for a in results[0]["attempts"]] == ["resolved"]
    assert slept == [5.0, 7.5, 11.25] and results[0]["quota_wait_s"] == 24
    fake["quota_fails"] = 100
    results = evaluate.run([INSTANCE], {"demo__demo-1": PRED}, "r2", retries=0, quota_wait_s=10)
    assert results[0]["status"] == "infra" and "sandbox_quota_exceeded" in results[0]["error"]


@pytest.mark.parametrize("failure", ["runtime_fails", "api_fails"])
def test_a_prized_failure_mid_eval_is_infra_not_graded(fake, tmp_path, failure):
    fake[failure] = True
    info = evaluate.run_instance(INSTANCE, PRED, "r1", 1800)
    assert info["status"] == "infra" and "InfraError" in info["error"]
    assert not (logs(tmp_path) / "test_output.txt").exists()
    assert not (logs(tmp_path) / "report.json").exists()
    assert FakeEnv.made[0].cleaned == 1


def test_image_assets_fail_one_instance_not_the_run(fake):
    multimodal = {**INSTANCE, "image_assets": json.dumps({"test_patch": [{"path": "a.png", "url": "u"}]})}
    info = evaluate.run_instance(multimodal, PRED, "r1", 1800)
    assert info["status"] == "error" and "image assets" in info["error"]
