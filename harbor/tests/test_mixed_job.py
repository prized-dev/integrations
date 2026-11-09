"""A whole Harbor job (Harbor's own CLI, Job and Trial) over a mixed dataset
where Prized refuses the Docker template: the native task's trials run and
score, and each Dockerfile trial records the refusal as its own error, so the
job finishes instead of ending at the first refused trial.

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


def _task(root: Path, name: str, extra: str, dockerfile: bool) -> None:
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


def test_a_mixed_dataset_finishes_where_the_docker_template_is_refused(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRIZED_API_KEY", "fake-key-for-preflight")
    monkeypatch.setattr(_module, "_docker_refusal", None)
    monkeypatch.setattr(_module, "_network_refused_at", None)
    cloud = _Cloud()
    templates = []

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

    tasks = tmp_path / "tasks"
    _task(tasks, "native", 'docker_image = "python:3.12-slim"\n', dockerfile=False)
    _task(tasks, "dockerfile", "", dockerfile=True)
    jobs = tmp_path / "jobs"
    from harbor.cli.main import app

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harbor",
            "run",
            "-p",
            str(tasks),
            "-i",
            "native",
            "-i",
            "dockerfile",
            "-k",
            "2",
            "-n",
            "1",
            "-a",
            "oracle",
            "-e",
            "harbor_prized:PrizedEnvironment",
            "-o",
            str(jobs),
            "--job-name",
            "mixed",
        ],
    )
    try:
        app()
    except SystemExit as exc:
        assert exc.code in (0, None)

    results = {}
    for path in sorted((jobs / "mixed").glob("*/result.json")):
        result = json.loads(path.read_text())
        task = path.parent.name.split("__")[0]
        error = result.get("exception_info") or {}
        reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get(
            "reward"
        )
        results.setdefault(task, []).append(
            (reward, error.get("exception_type"), error.get("exception_message"))
        )
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
    job = json.loads((jobs / "mixed" / "result.json").read_text())
    assert job.get("finished_at") is not None
    assert cloud.alive() == []


@pytest.fixture(autouse=True)
def _no_rich_width(monkeypatch):
    # Harbor's console wraps at the terminal's width; keep its output plain.
    monkeypatch.setenv("COLUMNS", "200")
