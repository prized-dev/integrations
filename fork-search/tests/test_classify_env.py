"""forksearch.classify_env against a fake Prized cloud (no network, no sandboxes).

Runs where Harbor 0.25 and harbor-prized are installed (the keyless runner):
    PYTHONPATH=~/fork-search python -m pytest -q ~/fork-search/tests
"""

import asyncio
import base64
import io
import json
import os
import socket
import subprocess
import sys
import tarfile

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from prized import ExecResult, PrizedError

from forksearch import classify_env as ce
from harbor_prized import PrizedEnvironment
from harbor_prized import _environment as hp

REWARD_RM = "rm -f -- /logs/verifier/reward.txt /logs/verifier/reward.json"
TEST_CMD = "bash /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1"


def _tar_gz(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Cloud:
    """Every fake sandbox, by name, and the rewards each one's verifier writes."""

    def __init__(self, rewards):
        self.rewards = rewards  # name -> "1" / "0"
        self.boxes = {}
        self.events = []  # (box, kind, what) in global order
        self.fork_error = None
        self.fresh_errors = 0
        self.fresh_delay = 0.0


class Files:
    def __init__(self, box):
        self.box = box

    async def write(self, path, data, mode=None):
        self.box.cloud.events.append((self.box.id, "write", path))

    async def read_bytes(self, path):
        self.box.cloud.events.append((self.box.id, "read", path))
        if path.endswith(".tar.gz"):  # a downloaded dir: the verifier dir
            return _tar_gz({"reward.txt": self.box.cloud.rewards.get(self.box.id, "1")})
        return b"workdir-tar"


class Box:
    def __init__(self, cloud, name):
        self.cloud, self.id = cloud, name
        self.files = Files(self)
        self.labels = None
        self.deleted = self.closed = self.paused = False
        cloud.boxes[name] = self

    def inner(self, cmd):
        """The command inside the image, for a runtime.py command line."""
        if cmd.startswith(f"python3 {hp._RUNTIME}") and " - " in cmd:
            script = base64.b64decode(cmd.rsplit(" ", 1)[1]).decode()
            return script.splitlines()[-1]
        return None

    async def exec(self, cmd, timeout=None, full_output=False):
        inner = self.inner(cmd)
        self.cloud.events.append((self.id, "exec", inner if inner is not None else cmd))
        out = ""
        if inner is None:
            if cmd.startswith(f"touch {ce.MARKER}"):
                out = "image_mb=812\ndisk_mb=12000\nfree_mb=9000\ndigest=sha256:feed\n"
            elif "fs-manifest-" in cmd:
                out = json.dumps({"entries": 3, "content_sha256": "m1"}) + "\n"
            elif "st_ctime" in cmd:
                out = json.dumps({"changed": 2, "top": [["/usr/local", 2]]}) + "\n"
            elif "SIGKILL" in cmd:
                out = json.dumps({"before": [[7, "nginx"]], "killed": [[7, "nginx"]], "left": []}) + "\n"
            elif cmd.startswith("stat -c %s"):
                out = "11\nabc\n"
            elif cmd.startswith("sha256sum"):
                out = "abc\n"
        return ExecResult(stdout=out, stderr="", exit_code=0)

    async def fork(self, count=1, *, labels=None, timeout=None):
        self.cloud.events.append((self.id, "fork", count))
        if self.cloud.fork_error is not None:
            raise self.cloud.fork_error
        copies = []
        for i in range(count):
            copy = Box(self.cloud, f"{self.id}.fork{i}")
            copy.labels = labels
            copies.append(copy)
        return copies

    async def keep_alive(self):
        pass

    async def delete(self):
        self.deleted = True
        self.cloud.events.append((self.id, "delete", ""))

    async def pause(self):
        self.paused = True

    async def close(self):
        self.closed = True


@pytest.fixture
def cloud(monkeypatch):
    c = Cloud({"A": "1", "A.fork0": "1", "A.fork1": "0", "B": "0"})

    async def fake_start(self, image):
        name = "A" if self.session_id.endswith("__env") else "B" if self.session_id.endswith("__transplant") else "V"
        if name == "B" and c.fresh_delay:
            await asyncio.sleep(c.fresh_delay)
        if name == "B" and c.fresh_errors:
            c.fresh_errors -= 1
            raise PrizedError("registry_error", "TOOMANYREQUESTS")
        box = Box(c, name)
        box.labels = dict(self._labels)
        box.shape = self._shape()
        self._sandbox = box

    async def no_user(self, user):
        return None

    monkeypatch.setattr(PrizedEnvironment, "_start", fake_start)
    monkeypatch.setattr(PrizedEnvironment, "_user_identity", no_user)
    monkeypatch.setattr(ce, "START_BACKOFF_SEC", (0, 0))
    return c


def _env(tmp_path, session_id="t__abc__env", shapes=None):
    task_dir = tmp_path / "task"
    (task_dir / "environment").mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        '[verifier]\ntimeout_sec = 30.0\n[environment]\ndocker_image = "python:3.12"\n'
    )
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    kw = {}
    if shapes is not None:
        path = tmp_path / "shapes.json"
        path.write_text(json.dumps(shapes))
        kw["shapes"] = str(path)
    return ce.ClassifyEnvironment(
        environment_dir=task_dir / "environment",
        environment_name="prized-test",
        session_id=session_id,
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(docker_image="python:3.12"),
        labels="agent=agent-xp,task=fs-test",
        **kw,
    )


async def _oracle_and_verify(env, tmp_path):
    """What Harbor's oracle trial does to the environment, in its order."""
    await env.start(force_build=False)
    await env.exec("bash /solution/solve.sh")
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    await env.exec(REWARD_RM, user="root")
    await env.upload_dir(tests, "/tests")
    await env.exec(TEST_CMD)
    await env.download_dir("/logs/verifier", env.trial_paths.verifier_dir)
    await env.stop(delete=True)
    return json.loads((env.trial_paths.trial_dir / "classify.json").read_text())


def _execs(cloud, box):
    return [w for b, k, w in cloud.events if b == box and k == "exec"]


async def test_classifies_a_live_service_task(cloud, tmp_path):
    rec = await _oracle_and_verify(_env(tmp_path), tmp_path)
    assert rec["oracle_rewards"] == {"reward": 1.0}
    assert rec["replays"]["control"]["rewards"] == {"reward": 1.0}
    assert rec["replays"]["kill"]["rewards"] == {"reward": 0.0}
    assert rec["replays"]["transplant"]["rewards"] == {"reward": 0.0}
    assert rec["copies"] == {"control": "A.fork0", "kill": "A.fork1"}
    assert rec["kill"]["killed"] == [[7, "nginx"]]
    assert rec["image_mb"] == 812 and rec["image_digest"] == "sha256:feed"
    assert rec["copy"]["sha256_ok"] is True
    assert rec["manifest_control"]["same_as_ref"] and rec["manifest_transplant"]["same_as_ref"]
    # every sandbox is deleted
    assert all(b.deleted for b in cloud.boxes.values())


async def test_fork_comes_before_any_verifier_step_reaches_a(cloud, tmp_path):
    await _oracle_and_verify(_env(tmp_path), tmp_path)
    order = [(b, k, w) for b, k, w in cloud.events if b == "A"]
    fork_at = next(i for i, e in enumerate(order) if e[1] == "fork")
    before = [w for _, k, w in order[:fork_at] if k == "exec"]
    assert not any("/logs/verifier/reward" in w or "/tests" in w for w in before)
    assert any(w == REWARD_RM for _, k, w in order[fork_at:] if k == "exec")


async def test_the_same_verifier_steps_run_on_every_copy(cloud, tmp_path):
    await _oracle_and_verify(_env(tmp_path), tmp_path)

    def verifier_part(box):
        cmds = _execs(cloud, box)
        start = cmds.index(REWARD_RM)
        return [c for c in cmds[start:] if c in (REWARD_RM, TEST_CMD)]

    expected = [REWARD_RM, TEST_CMD]
    for box in ("A", "A.fork0", "A.fork1", "B"):
        assert verifier_part(box) == expected, box
    # the copies are prepared (restore, kill) before the replay starts on them
    c = _execs(cloud, "A.fork0")
    assert any("tar -C" in x and "-xf" in x for x in c[: c.index(REWARD_RM)])
    assert any("SIGKILL" in x for x in _execs(cloud, "A.fork1")[: _execs(cloud, "A.fork1").index(REWARD_RM)])
    b = _execs(cloud, "B")
    assert any("tar -C" in x and "-xf" in x for x in b[: b.index(REWARD_RM)])
    # nothing ran the solution on B: it only got the workdir
    assert "bash /solution/solve.sh" not in b


async def test_transplant_waits_for_a_slow_fresh_start(cloud, tmp_path):
    cloud.fresh_delay = 0.3
    rec = await _oracle_and_verify(_env(tmp_path), tmp_path)
    assert rec["replays"]["transplant"]["rewards"] == {"reward": 0.0}
    assert rec["copy"]["sha256_ok"] is True


async def test_fresh_start_is_retried_then_reported(cloud, tmp_path):
    cloud.fresh_errors = 2
    rec = await _oracle_and_verify(_env(tmp_path), tmp_path)
    assert len(rec["fresh_start_errors"]) == 2
    assert rec["replays"]["transplant"]["rewards"] == {"reward": 0.0}


async def test_fresh_start_that_never_comes_up_drops_only_the_transplant(cloud, tmp_path):
    cloud.fresh_errors = 5
    rec = await _oracle_and_verify(_env(tmp_path), tmp_path)
    assert "transplant" in rec["prepare_errors"]
    assert set(rec["replays"]) == {"control", "kill"}


async def test_fork_failure_is_recorded_and_the_oracle_verdict_stands(cloud, tmp_path):
    cloud.fork_error = PrizedError("fork_failed", "no capacity")
    rec = await _oracle_and_verify(_env(tmp_path), tmp_path)
    assert "no capacity" in rec["fork_error"]
    assert rec["oracle_rewards"] == {"reward": 1.0}
    assert rec["replays"] == {}
    assert cloud.boxes["A"].deleted and cloud.boxes["B"].deleted


async def test_other_sessions_are_plain_prized_environments(cloud, tmp_path):
    env = _env(tmp_path, session_id="t__abc__verifier")
    assert env._fresh is None
    await env.start(force_build=False)
    await env.exec(REWARD_RM, user="root")
    await env.stop(delete=True)
    assert [k for b, k, w in cloud.events if k == "fork"] == []
    assert not (env.trial_paths.trial_dir / "classify.json").exists()


async def test_shapes_table_sizes_both_sandboxes(cloud, tmp_path):
    env = _env(tmp_path, shapes={"prized-test": {"image_headroom_gb": 3, "min_vcpu": 4}})
    await _oracle_and_verify(env, tmp_path)
    a, b = cloud.boxes["A"].shape, cloud.boxes["B"].shape
    assert a == b and a[0] == 4 and a[2] == 13
    assert cloud.boxes["B"].labels["fs-role"] == "transplant"
    assert cloud.boxes["A"].labels["fs-role"] == "agent"


async def test_mirror_pins_the_image_by_digest_for_both_sandboxes(cloud, tmp_path):
    env = _env(tmp_path, shapes={"prized-test": {"digest": "sha256:abc"}})
    assert env.task_env_config.docker_image == "python:3.12"  # no mirror asked: unchanged
    env = ce.ClassifyEnvironment(
        environment_dir=env.environment_dir, environment_name="prized-test", session_id="t__abc__env",
        trial_paths=env.trial_paths, task_env_config=EnvironmentConfig(docker_image="alexgshaw/x:20251031"),
        shapes=str(tmp_path / "shapes.json"), mirror="mirror.gcr.io",
    )
    assert env.task_env_config.docker_image == "mirror.gcr.io/alexgshaw/x@sha256:abc"
    assert env._fresh.task_env_config.docker_image == "mirror.gcr.io/alexgshaw/x@sha256:abc"
    assert env._cls["image"] == "alexgshaw/x:20251031"


async def test_oracle_verifier_that_never_finishes_skips_replays(cloud, tmp_path):
    env = _env(tmp_path)
    await env.start(force_build=False)
    await env.exec(REWARD_RM, user="root")
    await env.stop(delete=True)
    rec = json.loads((env.trial_paths.trial_dir / "classify.json").read_text())
    assert rec["replays"].startswith("skipped")
    assert all(b.deleted for b in cloud.boxes.values())


def test_manifest_diff():
    ref = "a\tf\t644\t3\tx\t\t10\nb\td\t755\t\t\t\t\nsock\ts\t755\t\t\t\t\n"
    same_but_mtime = "a\tf\t644\t3\tx\t\t11\nb\td\t755\t\t\t\t\n"
    d = ce.manifest_diff(ref, same_but_mtime)
    assert d["missing"] == 1 and d["sockets_missing"] == 1 and d["changed"] == 0 and d["mtime_only"] == 1
    changed = "a\tf\t600\t3\tx\t\t10\nb\td\t755\t\t\t\t\nsock\ts\t755\t\t\t\t\nnew\tf\t644\t0\ty\t\t1\n"
    d = ce.manifest_diff(ref, changed)
    assert d["changed"] == 1 and d["extra"] == 1 and d["missing"] == 0


def _run_manifest(root, out):
    r = subprocess.run([sys.executable, "-", str(root), str(out)], input=ce.MANIFEST_PY,
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_manifest_script_sees_content_and_modes_not_mtimes(tmp_path):
    root = tmp_path / "wd"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_text("hello")
    os.symlink("sub/f.txt", root / "link")
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(root / "srv.sock"))
    first = _run_manifest(root, tmp_path / "m1")
    assert first["files"] == 1 and first["sockets"] == 1 and first["bytes"] == 5
    os.utime(root / "sub" / "f.txt", (1, 1))
    assert _run_manifest(root, tmp_path / "m2")["content_sha256"] == first["content_sha256"]
    os.chmod(root / "sub" / "f.txt", 0o751 if (os.stat(root / "sub" / "f.txt").st_mode & 0o777) != 0o751 else 0o640)
    assert _run_manifest(root, tmp_path / "m3")["content_sha256"] != first["content_sha256"]
    (root / "sub" / "f.txt").write_text("hellO")
    assert _run_manifest(root, tmp_path / "m4")["content_sha256"] != first["content_sha256"]
    s.close()
    d = ce.manifest_diff((tmp_path / "m1").read_text(), (tmp_path / "m4").read_text())
    assert d["changed"] == 1
