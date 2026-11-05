"""verifier_fork: Harbor's verifier runs in a fork of the agent's sandbox."""

import io
import json
import tarfile

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from prized import ExecResult, PrizedError

from harbor_prized import PrizedEnvironment
from harbor_prized import _environment as _module

REWARD_RM = "rm -f -- /logs/verifier/reward.txt /logs/verifier/reward.json"


def _tar_gz(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _Files:
    def __init__(self, box: "_Box") -> None:
        self.box = box

    async def write(self, path, data, mode=None):
        self.box.log.append(("write", path))

    async def read_bytes(self, path):
        self.box.log.append(("read", path))
        if path.endswith(".tar.gz"):  # a downloaded dir: the verifier dir
            return _tar_gz({"reward.txt": self.box.reward})
        return b""


class _Box:
    """Stands in for prized.AsyncSandbox, with fork()."""

    def __init__(self, name: str, reward: str = "1", fork_error=None) -> None:
        self.id, self.reward, self.fork_error = name, reward, fork_error
        self.log: list[tuple[str, str]] = []
        self.files = _Files(self)
        self.copies: list[_Box] = []
        self.fork_labels = None
        self.deleted = self.paused = self.closed = False

    async def exec(self, cmd, timeout=None, full_output=False):
        self.log.append(("exec", cmd))
        return ExecResult(stdout="0\n0\n", stderr="", exit_code=0)

    async def fork(self, count=1, *, labels=None):
        if self.fork_error is not None:
            raise self.fork_error
        self.fork_labels = labels
        copy = _Box(f"{self.id}-fork", reward=self.reward)
        self.copies.append(copy)
        return [copy]

    async def keep_alive(self):
        pass

    async def delete(self):
        self.deleted = True

    async def pause(self):
        self.paused = True

    async def close(self):
        self.closed = True

    def execs(self) -> list[str]:
        return [what for kind, what in self.log if kind == "exec"]


def _environment(tmp_path, verifier_fork, session_id="t__abc__env", steps=False):
    task_dir = tmp_path / "task"
    (task_dir / "environment").mkdir(parents=True, exist_ok=True)
    toml = '[environment]\ndocker_image = "python:3.12"\n'
    if steps:
        toml += '[[steps]]\nname = "one"\n'
    (task_dir / "task.toml").write_text(toml)
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return PrizedEnvironment(
        environment_dir=task_dir / "environment",
        environment_name="prized-test",
        session_id=session_id,
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(docker_image="python:3.12"),
        labels="agent=agent-xp",
        verifier_fork=verifier_fork,
    )


async def _verify(environment, tmp_path):
    """The calls Harbor's Verifier.verify makes, in its order."""
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    await environment.exec(REWARD_RM, user="root")
    await environment.upload_dir(tests, "/tests")
    await environment.exec("bash /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1")
    await environment.download_dir(
        "/logs/verifier", environment.trial_paths.verifier_dir
    )


@pytest.fixture(autouse=True)
def _root_only(monkeypatch):
    async def no_user(self, user):
        return None

    monkeypatch.setattr(_module.PrizedEnvironment, "_user_identity", no_user)


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        (False, None),
        ("false", None),
        ("", None),
        (True, "fork"),
        ("true", "fork"),
        ("compare", "compare"),
    ],
)
def test_option_values(value, mode):
    assert _module._fork_mode(value) == mode


def test_option_refuses_other_values():
    with pytest.raises(ValueError, match="verifier_fork"):
        _module._fork_mode("sometimes")


def test_off_for_tasks_with_steps_and_for_harbors_separate_verifier(tmp_path):
    assert _environment(tmp_path, "true", steps=True)._verifier_fork is None
    separate = _environment(tmp_path, "true", session_id="t__abc__verifier__trial")
    assert separate._verifier_fork is None


@pytest.mark.asyncio
async def test_the_verifier_runs_in_a_fork_and_the_agent_sandbox_gets_none_of_it(
    tmp_path,
):
    environment = _environment(tmp_path, "true")
    environment._sandbox = agent = _Box("sbx_agent")
    await environment.exec("echo agent step")
    before = len(agent.log)

    await _verify(environment, tmp_path)

    assert len(agent.log) == before  # nothing of the verifier reached it
    (fork,) = agent.copies
    assert agent.fork_labels["harbor-role"] == "verifier"
    assert agent.fork_labels["agent"] == "agent-xp"
    fork_cmds = fork.execs()
    assert "/etc/hostname" in fork_cmds[0]  # the copy's hostname first
    assert any(REWARD_RM in _script(c) for c in fork_cmds if " - " in c)
    assert any(
        path.startswith("/home/prized/rootfs/.runtime-xfer/up-")
        for kind, path in fork.log
        if kind == "write"
    )
    assert (environment.trial_paths.verifier_dir / "reward.txt").read_text() == "1"
    assert environment._sandbox is agent  # the verifier is done: back to the agent's

    await environment.stop(delete=True)
    assert agent.deleted and fork.deleted
    record = json.loads(
        (environment.trial_paths.trial_dir / "verifier-fork.json").read_text()
    )
    assert (
        record["agent_sandbox"] == "sbx_agent"
        and record["fork_sandbox"] == "sbx_agent-fork"
    )
    assert (
        record["mode"] == "fork"
        and record["fork_sec"] >= 0
        and record["verifier_sec"] >= 0
    )


@pytest.mark.asyncio
async def test_the_tests_upload_starts_it_too(tmp_path):
    environment = _environment(tmp_path, "true")
    environment._sandbox = agent = _Box("sbx_agent")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("true\n")
    await environment.upload_dir(tests, "/tests")
    assert environment._sandbox is agent.copies[0]
    assert not [kind for kind, _ in agent.log if kind == "write"]
    await environment.stop(delete=True)


@pytest.mark.asyncio
async def test_off_by_default(tmp_path):
    environment = _environment(tmp_path, False)
    environment._sandbox = agent = _Box("sbx_agent")
    await _verify(environment, tmp_path)
    assert agent.copies == []
    assert any(REWARD_RM in _script(c) for c in agent.execs() if " - " in c)
    await environment.stop(delete=True)
    assert not (environment.trial_paths.trial_dir / "verifier-fork.json").exists()


@pytest.mark.asyncio
async def test_a_failed_fork_verifies_on_the_agent_sandbox(tmp_path):
    environment = _environment(tmp_path, "true")
    environment._sandbox = agent = _Box(
        "sbx_agent", fork_error=PrizedError("fork_capacity")
    )
    await _verify(environment, tmp_path)
    assert any(REWARD_RM in _script(c) for c in agent.execs() if " - " in c)
    await environment.stop(delete=True)
    record = json.loads(
        (environment.trial_paths.trial_dir / "verifier-fork.json").read_text()
    )
    assert "fork_capacity" in record["fork_error"]


@pytest.mark.asyncio
async def test_compare_runs_the_same_steps_on_the_agent_sandbox_at_stop(tmp_path):
    environment = _environment(tmp_path, "compare")
    environment._sandbox = agent = _Box("sbx_agent", reward="0")
    await _verify(environment, tmp_path)
    fork = agent.copies[0]
    assert fork.reward == "0"
    assert agent.execs() == []

    await environment.stop(delete=True)

    replayed = [_script(c) for c in agent.execs() if " - " in c]
    assert any(REWARD_RM in s for s in replayed)
    assert any("bash /tests/test.sh" in s for s in replayed)
    out = environment.trial_paths.trial_dir / "verifier-agent-sandbox" / "reward.txt"
    assert out.read_text() == "0"
    record = json.loads(
        (environment.trial_paths.trial_dir / "verifier-fork.json").read_text()
    )
    assert record["fork_rewards"] == {"reward": 0.0}
    assert record["agent_sandbox_rewards"] == {"reward": 0.0}
    assert record["agree"] is True
    assert record["agent_sandbox_had_tests"] is False
    assert agent.deleted and fork.deleted


@pytest.mark.asyncio
async def test_compare_reports_different_verdicts(tmp_path):
    environment = _environment(tmp_path, "compare")
    environment._sandbox = agent = _Box("sbx_agent", reward="1")
    await _verify(environment, tmp_path)
    agent.reward = "0"  # the agent's sandbox would grade differently
    await environment.stop(delete=True)
    record = json.loads(
        (environment.trial_paths.trial_dir / "verifier-fork.json").read_text()
    )
    assert record["fork_rewards"] == {"reward": 1.0}
    assert record["agent_sandbox_rewards"] == {"reward": 0.0}
    assert record["agree"] is False


@pytest.mark.asyncio
async def test_no_delete_pauses_both(tmp_path):
    environment = _environment(tmp_path, "true")
    environment._sandbox = agent = _Box("sbx_agent")
    await _verify(environment, tmp_path)
    await environment.stop(delete=False)
    assert agent.paused and agent.copies[0].paused
    assert not agent.deleted and not agent.copies[0].deleted


def _script(command: str) -> str:
    import base64

    return base64.b64decode(command.split(" - ", 1)[1]).decode()
