"""Whole Harbor jobs (Harbor's own CLI, Job and Trial) over mixed datasets
where Prized refuses what some tasks need, the Docker template or switching a
sandbox's network off: the other task's trials run and score, and each refused
trial records the refusal as its own error, so the job finishes instead of
ending at the first refused trial.

The Prized API is faked (``test_docker_fork._Box``); the native trials'
commands answer as a passing task would, and their verifier's folder holds a
reward of 1.
"""

import json
import sys
from pathlib import Path

import pytest
from prized import PrizedError
from test_docker_fork import _Box, _Cloud

from harbor_prized import PrizedEnvironment
from harbor_prized import _environment as _module

TASK_TOML = "[environment]\ncpus = 1\nmemory_mb = 1024\n{extra}[agent]\ntimeout_sec = 60\n[verifier]\ntimeout_sec = 60\n"
PREBUILT = 'docker_image = "python:3.12-slim"\n'


def _task(root: Path, name: str, extra: str, dockerfile: bool = False) -> None:
    d = root / name
    for sub in ("environment", "tests", "solution"):
        (d / sub).mkdir(parents=True)
    (d / "task.toml").write_text('version = "1.0"\n' + TASK_TOML.format(extra=extra))
    (d / "instruction.md").write_text("Write ok to /app/out.txt\n")
    (d / "solution" / "solve.sh").write_text("#!/bin/bash\necho ok > /app/out.txt\n")
    (d / "tests" / "test.sh").write_text(
        "#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n"
    )
    if dockerfile:
        (d / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")


@pytest.fixture
def harbor_job(tmp_path, monkeypatch):
    """Runs ``harbor run`` over the tasks in tmp_path/tasks with the fakes;
    returns each task's trials as (reward, exception type, message), and the
    templates asked for."""
    monkeypatch.setenv("PRIZED_API_KEY", "fake-key-for-preflight")
    monkeypatch.setenv("COLUMNS", "200")  # Harbor's console: plain output
    monkeypatch.setattr(_module, "_docker_refusal", None)
    monkeypatch.setattr(_module, "_network_refused_at", None)
    cloud = _Cloud()
    templates: list[str] = []

    async def create(template, **kwargs):
        templates.append(template)
        if template == "prized-docker-v1":
            raise PrizedError("template_inactive", status=409)
        return _Box(cloud, kwargs["labels"])

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    downloads = PrizedEnvironment.download_dir

    async def download_dir(self, source_dir, target_dir):
        if self._compose is not None:
            return await downloads(self, source_dir, target_dir)
        # The native trial's folders as a passing task leaves them.
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        if source_dir.rstrip("/").endswith("verifier"):
            (target / "reward.txt").write_text("1\n")

    monkeypatch.setattr(PrizedEnvironment, "download_dir", download_dir)

    def run(names: list[str], *ek: str):
        jobs = tmp_path / "jobs"
        argv = ["harbor", "run", "-p", str(tmp_path / "tasks")]
        for name in names:
            argv += ["-i", name]
        argv += ["-k", "2", "-n", "1", "-a", "oracle"]
        argv += ["-e", "harbor_prized:PrizedEnvironment", "-o", str(jobs)]
        argv += ["--job-name", "mixed"]
        for value in ek:
            argv += ["--ek", value]
        from harbor.cli.main import app

        monkeypatch.setattr(sys, "argv", argv)
        try:
            app()
        except SystemExit as exc:
            assert exc.code in (0, None)
        results: dict[str, list[tuple]] = {}
        for path in sorted((jobs / "mixed").glob("*/result.json")):
            result = json.loads(path.read_text())
            error = result.get("exception_info") or {}
            reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get(
                "reward"
            )
            results.setdefault(path.parent.name.split("__")[0], []).append(
                (reward, error.get("exception_type"), error.get("exception_message"))
            )
        job = json.loads((jobs / "mixed" / "result.json").read_text())
        assert job.get("finished_at") is not None  # the job finished
        assert cloud.alive() == []
        return results, templates

    return run


def test_a_mixed_dataset_finishes_where_the_docker_template_is_refused(
    tmp_path, harbor_job
):
    _task(tmp_path / "tasks", "native", PREBUILT)
    _task(tmp_path / "tasks", "dockerfile", "", dockerfile=True)
    results, templates = harbor_job(["native", "dockerfile"])
    # Every trial has a result: none ended the job.
    assert sorted(results) == ["dockerfile", "native"]
    assert [r[:2] for r in results["native"]] == [(1.0, None), (1.0, None)]
    assert len(results["dockerfile"]) == 2
    for reward, kind, message in results["dockerfile"]:
        assert (reward, kind) == (None, "RuntimeError")
        assert message.startswith(_module._DOCKERFILE_REFUSED)
        assert message.endswith("(409 template_inactive).")
    # The API was asked once; the second Dockerfile trial made no sandbox.
    assert templates.count("prized-docker-v1") == 1
    assert templates.count("prized-base-v1") == 2


def test_a_mixed_dataset_finishes_where_prized_cant_switch_the_network(
    tmp_path, harbor_job, monkeypatch
):
    # Without Docker mode (--ek docker=false): a public task and a no-network
    # one, where Prized can't turn a sandbox's network off yet.
    switches = []

    async def no_switch(sandbox, network):
        switches.append(network)
        raise PrizedError("network_none_unavailable", status=409)

    monkeypatch.setattr(_module, "put_network", no_switch)
    _task(tmp_path / "tasks", "public", PREBUILT)
    _task(tmp_path / "tasks", "closed", PREBUILT + 'network_mode = "no-network"\n')
    results, templates = harbor_job(["public", "closed"], "docker=false")
    assert sorted(results) == ["closed", "public"]
    assert [r[:2] for r in results["public"]] == [(1.0, None), (1.0, None)]
    assert len(results["closed"]) == 2
    for reward, kind, message in results["closed"]:
        assert (reward, kind) == (None, "RuntimeError")
        assert message.startswith(_module._NETWORK_REFUSED)
    # The API was asked once (the first closed trial's sandbox, deleted); the
    # second closed trial made no sandbox.
    assert switches == ["default"]
    assert templates == ["prized-base-v1"] * 3
