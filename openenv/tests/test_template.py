# SPDX-License-Identifier: Apache-2.0

"""Unit tests for PrizedProvider(template="auto").

The fake Prized API is test_fork_group's: every "sandbox" runs a real OpenEnv
server on loopback behind a stand-in for Prized's edge, and a fork copies the
server's sessions. On top of it this fake can pause a sandbox, connect to one
by id and answer the reseed command, so a base is really built, warmed over a
WebSocket, paused, recorded in a state file and forked. The launcher that
reseeds a copy's server runs for real in a subprocess, with a stand-in for
uvicorn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import test_fork_group as tfg
from openenv_prized import PrizedProvider
from openenv_prized import _template
from openenv_prized import provider as prized_provider
from test_fork_group import DiceEnv, roll

IMAGE = "space:test/dice"


class _Missing(Exception):
    """What the SDK raises for a sandbox that is not there."""

    status = 404
    code = "not_found"


class _Quota(Exception):
    status = 429
    code = "sandbox_quota_exceeded"


class _NotForkable(Exception):
    status = 409
    code = "parent_not_forkable"


class _TemplateAdapter(tfg._ForkingAdapter):
    def __init__(self, edge):
        super().__init__(edge)
        self.events: list = []
        self.created: list = []
        self.paused: set = set()
        self.released: list = []
        self.fail_fork = None
        self.install_exit = 0
        self.reseed_exit = 0
        self.ws_opens = 0
        self.create_delay_s = 0.0
        self.copies: dict = {}
        self.states: dict = {}  # sandbox id -> state to report
        self.reseed_raises = None
        self.reseed_report = None
        self.bad_headers_once = False
        self.health_gets = 0

    def create_sandbox(self, **kwargs):
        if self.create_delay_s:
            time.sleep(self.create_delay_s)
        sandbox = super().create_sandbox(**kwargs)
        sandbox.labels = kwargs["labels"]
        sandbox.settings = kwargs
        self.created.append(sandbox)
        self.events.append(("create", sandbox.id, kwargs["labels"].get("openenv")))
        return sandbox

    def exec(self, sandbox, command, *, timeout=60, env=None, background=False):
        if background:
            self.events.append(("launch", sandbox.id, command))
        elif "OPENENV_ROOT=" in command:
            self.events.append(("install", sandbox.id))
            if self.install_exit:
                return self.install_exit, ""
        elif "probe()" in command:
            self.events.append(("ready", sandbox.id))
        elif "rev-parse HEAD" in command:
            return 0, "a" * 40 + "\n"
        elif "reseed.pid" in command:
            self.events.append(("reseed", sandbox.id))
            if self.reseed_raises is not None:
                raise self.reseed_raises
            if self.reseed_exit:
                return self.reseed_exit, ""
            done = self.reseed_report or {
                "generation": 1,
                "seed": 42,
                "random": True,
                "hook": "none",
            }
            return 0, json.dumps(done) + "\n"
        return super().exec(
            sandbox, command, timeout=timeout, env=env, background=background
        )

    def fork(self, sandbox, *, count, labels):
        self.events.append(("fork", sandbox.id))
        if self.fail_fork is not None:
            raise self.fail_fork
        copies = super().fork(sandbox, count=count, labels=labels)
        self.copies.setdefault(sandbox.id, []).extend(c.id for c in copies)
        for c in copies:
            # No labels: a copy keeps its parent's (as on Prized).
            c.labels = labels if labels is not None else getattr(sandbox, "labels", {})
        return copies

    def connect(self, sandbox_id):
        self.events.append(("connect", sandbox_id))
        if sandbox_id not in self.live:
            raise _Missing(f"{sandbox_id} not found")
        return self.live[sandbox_id]

    def state(self, sandbox):
        if sandbox.id in self.states:
            return self.states[sandbox.id]
        return "paused" if sandbox.id in self.paused else "running"

    def port_headers(self, sandbox):
        if self.bad_headers_once:
            self.bad_headers_once = False
            return {"Prized-Key": "wrong"}
        return super().port_headers(sandbox)

    def pause(self, sandbox):
        self.events.append(("pause", sandbox.id))
        self.paused.add(sandbox.id)

    def release(self, sandbox):
        self.released.append(sandbox.id)

    def delete(self, sandbox):
        super().delete(sandbox)
        self.events.append(("delete", sandbox.id))

    def kinds(self):
        return [e[0] for e in self.events]


@pytest.fixture()
def adapter():
    adapter = _TemplateAdapter(tfg._Edge())
    yield adapter
    for sandbox in list(adapter.live.values()):
        sandbox.server.stop()


@pytest.fixture()
def state_path(tmp_path):
    return str(tmp_path / "state" / "templates.json")


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch):
    monkeypatch.setattr(prized_provider, "_sleep", lambda seconds: None)
    prized_provider._custom_cmd_warned.clear()


def _provider(adapter, state_path, **kwargs) -> PrizedProvider:
    kwargs.setdefault("image", IMAGE)
    return PrizedProvider(
        _adapter=adapter, template="auto", template_state=state_path, **kwargs
    )


def _entries(state_path):
    with open(state_path) as handle:
        return json.load(handle)["templates"]


async def _use(provider) -> float:
    """Start an environment through the provider, play one roll, close it."""
    url = await asyncio.to_thread(provider.start_container)
    await asyncio.to_thread(provider.wait_for_ready, url, 10)
    env = DiceEnv(base_url=url, provider=provider)
    async with env:
        await env.reset()
        return await roll(env)


# ---------------------------------------------------------------------------
# Build once, fork after
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_first_start_builds_a_paused_base_and_later_starts_fork_it(
    adapter, state_path
):
    first = _provider(adapter, state_path, labels={"team": "rl"})
    await _use(first)
    built = first.last_template
    assert built["status"] == "built"
    base_id = built["base"]

    # Install, start (through the launcher), rebase, warm, pause; then fork.
    kinds = adapter.kinds()
    assert kinds[:10] == [
        "create",  # the install parent
        "install",
        "launch",
        "ready",
        "fork",  # the rebase: the copy is the base
        "delete",  # the install parent
        "ready",  # the base, checked again after its warm-up reset
        "pause",
        "fork",  # this environment's copy
        "reseed",
    ]
    assert kinds[9] == "reseed"
    assert {adapter.events[i][1] for i in (6, 7, 8)} == {base_id}
    launch = next(e for e in adapter.events if e[0] == "launch")
    assert "prized_launch.py" in launch[2] and "-m uvicorn" not in launch[2]

    # The base: labelled as a template (not with this run's labels, so a
    # cleanup by them spares it), paused, kept; its install parent gone.
    base = adapter.live[base_id]
    assert base.labels == {
        "openenv": "template",
        "openenv-template": built["key"][:12],
    }
    assert base_id in adapter.paused
    install_parent = adapter.created[0]
    assert install_parent.id != base_id and install_parent.id in adapter.deleted
    assert install_parent.settings["auto_delete_min"] == 7 * 24 * 60
    # The warm-up reset opened the base's port and left no session behind.
    assert adapter.edge.opened
    deadline = time.monotonic() + 5
    while any(base.server.env_server._sessions.values()):
        assert time.monotonic() < deadline, "the warm-up session was not closed"
        time.sleep(0.02)

    # The state file: readable, one entry naming the base.
    (entry,) = _entries(state_path).values()
    assert entry["sandbox_id"] == base_id
    assert entry["image"] == IMAGE and entry["version"] == "unpinned"
    assert entry["commit"] == "a" * 40 and entry["warmed"] is True
    assert built["reseed"]["random"] is True

    # The environment ran in a copy, labelled as a server, now deleted.
    (copy_id,) = adapter.copies[base_id]
    assert first.sandbox_id is None and copy_id in adapter.deleted
    assert {"timings": set(first.timings)} == {
        "timings": {
            "create_s",
            "install_s",
            "ready_s",
            "rebase_s",
            "warm_s",
            "pause_s",
            "fork_s",
            "reseed_s",
            "endpoint_s",
            "total_s",
        }
    }

    # Two more starts, from new providers (as from new processes): forks only.
    creates = len(adapter.created)
    for _ in range(2):
        later = _provider(adapter, state_path, labels={"team": "rl"})
        await _use(later)
        assert later.last_template["status"] == "forked"
        assert later.last_template["base"] == base_id
        assert set(later.timings) == {
            "connect_s",
            "fork_s",
            "reseed_s",
            "endpoint_s",
            "total_s",
        }
    assert len(adapter.created) == creates
    forks = [f for f in adapter.forks if f["parent"] == base_id]
    assert len(forks) == 3
    assert all(f["labels"] == {"openenv": "server", "team": "rl"} for f in forks)
    assert adapter.released.count(base_id) == 3  # handles let go, base kept
    assert base_id in adapter.live and base_id not in adapter.deleted


@pytest.mark.asyncio
async def test_each_copy_is_reseeded_unless_turned_off(adapter, state_path):
    await _use(_provider(adapter, state_path))
    assert ("reseed" in adapter.kinds()) is True

    adapter.events.clear()
    off = _provider(adapter, state_path, reseed_after_fork=False)
    await _use(off)
    assert "reseed" not in adapter.kinds()
    assert off.last_template["reseed"] == "off"


@pytest.mark.asyncio
async def test_a_copy_that_cannot_be_reseeded_warns(adapter, state_path):
    await _use(_provider(adapter, state_path))
    adapter.reseed_exit = 10
    later = _provider(adapter, state_path)
    with pytest.warns(RuntimeWarning, match="not started by openenv-prized's launcher"):
        await _use(later)
    assert later.last_template["reseed"] == {
        "error": "it was not started by openenv-prized's launcher"
    }


@pytest.mark.asyncio
async def test_a_custom_cmd_has_no_launcher_and_says_so_once(adapter, state_path):
    cmd = "python -m mine"
    first = _provider(adapter, state_path, cmd=cmd)
    with pytest.warns(RuntimeWarning, match="with your own cmd"):
        await _use(first)
    launch = next(e for e in adapter.events if e[0] == "launch")
    assert "prized_launch.py" not in launch[2]
    assert "reseed" not in adapter.kinds()
    (entry,) = _entries(state_path).values()
    assert entry["reseed"] == "none (custom cmd)"


# ---------------------------------------------------------------------------
# Falling back, forgetting, deleting
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_failed_fork_starts_fresh_and_keeps_the_base(
    adapter, state_path, caplog
):
    await _use(_provider(adapter, state_path))
    (entry,) = _entries(state_path).values()
    adapter.fail_fork = _Quota("retained_disk")
    adapter.events.clear()

    later = _provider(adapter, state_path)
    with caplog.at_level(logging.WARNING, logger="openenv_prized"):
        await _use(later)
    assert later.last_template["status"] == "fresh"
    assert "_Quota: retained_disk" in later.last_template["error"]
    (line,) = [r.getMessage() for r in caplog.records if r.name == "openenv_prized"]
    assert entry["sandbox_id"] in line and "starting a fresh environment" in line
    assert "gone" not in line
    # Fresh: created, installed and served without the launcher.
    assert adapter.kinds()[:3] == ["connect", "fork", "create"]
    fresh = adapter.created[-1]
    assert fresh.labels["openenv"] == "server"
    launch = next(e for e in adapter.events if e[0] == "launch")
    assert "-m uvicorn" in launch[2]
    assert _entries(state_path) == {later.last_template["key"]: entry}


@pytest.mark.asyncio
async def test_a_base_deleted_elsewhere_is_forgotten_and_rebuilt(
    adapter, state_path, caplog
):
    first = _provider(adapter, state_path)
    await _use(first)
    base_id = first.last_template["base"]
    adapter.delete(adapter.live[base_id])  # e.g. its auto-delete ran

    later = _provider(adapter, state_path)
    with caplog.at_level(logging.WARNING, logger="openenv_prized"):
        await _use(later)
    assert later.last_template["status"] == "fresh"
    (line,) = [r.getMessage() for r in caplog.records if r.name == "openenv_prized"]
    assert "next start builds a new one" in line
    assert _entries(state_path) == {}

    rebuilt = _provider(adapter, state_path)
    await _use(rebuilt)
    assert rebuilt.last_template["status"] == "built"
    assert rebuilt.last_template["base"] != base_id


@pytest.mark.asyncio
async def test_delete_template_deletes_the_base_and_forgets_it(adapter, state_path):
    first = _provider(adapter, state_path)
    await _use(first)
    base_id = first.last_template["base"]

    assert first.delete_template() == base_id
    assert base_id in adapter.deleted and _entries(state_path) == {}
    assert first.delete_template() is None

    # A base that is already gone is still forgotten.
    second = _provider(adapter, state_path)
    await _use(second)
    adapter.delete(adapter.live[second.last_template["base"]])
    assert second.delete_template(IMAGE) == second.last_template["base"]
    assert _entries(state_path) == {}


@pytest.mark.asyncio
async def test_a_failed_build_deletes_what_it_made_and_records_nothing(
    adapter, state_path
):
    adapter.install_exit = 5
    failing = _provider(adapter, state_path)
    with pytest.raises(RuntimeError, match="installing it"):
        await asyncio.to_thread(failing.start_container)
    assert adapter.live == {}
    assert not os.path.exists(state_path)
    adapter.install_exit = 0
    await _use(_provider(adapter, state_path))  # the build lock was let go
    assert len(_entries(state_path)) == 1


@pytest.mark.asyncio
async def test_concurrent_first_starts_build_one_base(adapter, state_path):
    adapter.create_delay_s = 0.2
    providers = [_provider(adapter, state_path) for _ in range(3)]
    await asyncio.gather(*(_use(p) for p in providers))
    statuses = sorted(p.last_template["status"] for p in providers)
    assert statuses == ["built", "forked", "forked"]
    templates = [e for e in adapter.events if e[0] == "create" and e[2] == "template"]
    assert len(templates) == 1
    assert len({p.last_template["base"] for p in providers}) == 1


# ---------------------------------------------------------------------------
# The cache key
# ---------------------------------------------------------------------------
def _key(adapter, state_path, image=IMAGE, env_vars=None, cmd=None, **kwargs):
    p = _provider(adapter, state_path, image=image, **kwargs)
    source = prized_provider._parse_source(image)
    upload = (
        prized_provider._tar_directory(source["location"])
        if source["kind"] == "dir"
        else None
    )
    return p._template_key(source, upload, image, "prized-base-v1", cmd, env_vars)[0]


def test_the_key_follows_what_the_base_holds(adapter, state_path, tmp_path):
    base = _key(adapter, state_path)
    assert base == _key(adapter, state_path)  # stable
    assert base == _key(adapter, state_path, labels={"x": "y"})  # labels: no
    for other in (
        # copies inherit these from the base
        _key(adapter, state_path, max_runtime_min=5),
        _key(adapter, state_path, auto_pause_min=10),
        _key(adapter, state_path, template_auto_delete_min=0),
        _key(adapter, state_path, image="space:test/dice@v2"),
        _key(adapter, state_path, image="space:test/other"),
        _key(adapter, state_path, vcpu=4),
        _key(adapter, state_path, memory_gb=8),
        _key(adapter, state_path, disk_gb=20),
        _key(adapter, state_path, env_vars={"MODE": "hard"}),
        _key(adapter, state_path, cmd="python -m mine"),
        _key(adapter, state_path, install_cmd='"$UV" sync --extra x'),
        _key(adapter, state_path, api_key="pz-other"),
        _key(adapter, state_path, base_url="http://127.0.0.1:9"),
    ):
        assert other != base


def test_a_dir_source_is_keyed_by_its_content(adapter, state_path, tmp_path):
    src = tmp_path / "env"
    src.mkdir()
    (src / "server.py").write_text("x = 1\n")
    image = f"dir:{src}"
    first = _key(adapter, state_path, image=image)
    os.utime(src / "server.py", (1, 1))  # touched, same bytes
    assert _key(adapter, state_path, image=image) == first
    (src / "server.py").write_text("x = 2\n")
    assert _key(adapter, state_path, image=image) != first
    copy_dir = tmp_path / "elsewhere"
    copy_dir.mkdir()
    (copy_dir / "server.py").write_text("x = 2\n")
    assert _key(adapter, state_path, image=f"dir:{copy_dir}") == _key(
        adapter, state_path, image=image
    )


@pytest.mark.asyncio
async def test_the_state_file_holds_no_secret(adapter, state_path):
    secret = "very-secret-value-123"
    p = _provider(adapter, state_path, env_vars={"TOKEN": secret}, api_key="pz-key-xyz")
    await _use(p)
    text = open(state_path).read()
    assert secret not in text and "pz-key-xyz" not in text
    assert oct(os.stat(state_path).st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
async def test_an_unforkable_base_is_deleted_and_forgotten(adapter, state_path):
    first = _provider(adapter, state_path)
    await _use(first)
    base_id = first.last_template["base"]
    adapter.fail_fork = _NotForkable("memory_discarded")
    later = _provider(adapter, state_path)
    await _use(later)
    assert later.last_template["status"] == "fresh"
    assert base_id in adapter.deleted and _entries(state_path) == {}


@pytest.mark.asyncio
async def test_a_failed_base_is_deleted_and_forgotten(adapter, state_path):
    first = _provider(adapter, state_path)
    await _use(first)
    base_id = first.last_template["base"]
    adapter.states[base_id] = "failed"
    adapter.events.clear()
    later = _provider(adapter, state_path)
    await _use(later)
    assert later.last_template["status"] == "fresh"
    assert "fork" not in adapter.kinds()
    assert base_id in adapter.deleted and _entries(state_path) == {}


@pytest.mark.asyncio
async def test_a_running_base_is_paused_again_after_the_fork(adapter, state_path):
    first = _provider(adapter, state_path)
    await _use(first)
    base_id = first.last_template["base"]
    adapter.paused.discard(base_id)  # someone resumed it
    adapter.events.clear()
    later = _provider(adapter, state_path)
    await _use(later)
    assert later.last_template["status"] == "forked"
    kinds = adapter.kinds()
    assert kinds.index("fork") < kinds.index("pause")
    assert ("pause", base_id) in adapter.events and base_id in adapter.paused


@pytest.mark.asyncio
async def test_reseed_failures_warn_but_the_environment_runs(adapter, state_path):
    await _use(_provider(adapter, state_path))
    adapter.reseed_raises = RuntimeError("exec timed out")
    later = _provider(adapter, state_path)
    with pytest.warns(RuntimeWarning, match="reseed command failed"):
        await _use(later)
    assert later.last_template["status"] == "forked"

    adapter.reseed_raises = None
    adapter.reseed_report = {
        "generation": 1,
        "seed": 7,
        "random": True,
        "hook": "none",
        "error": "TypeError: bad hook secret-xyz",
    }
    hooked = _provider(adapter, state_path, env_vars={"TOKEN": "secret-xyz"})
    with pytest.warns(RuntimeWarning, match="did not fully reseed") as caught:
        await _use(hooked)
    assert "secret-xyz" not in str(caught[0].message)
    assert "secret-xyz" not in hooked.last_template["reseed"]["error"]


@pytest.mark.asyncio
async def test_a_copy_whose_server_is_gone_retires_the_base(adapter, state_path):
    first = _provider(adapter, state_path)
    await _use(first)
    base_id = first.last_template["base"]
    adapter.reseed_exit = 11
    later = _provider(adapter, state_path)
    await _use(later)
    assert later.last_template["status"] == "fresh"
    (copy_id,) = adapter.copies[base_id][1:]
    assert copy_id in adapter.deleted and base_id in adapter.deleted
    assert _entries(state_path) == {}


@pytest.mark.asyncio
async def test_a_base_that_cannot_be_recorded_is_deleted(
    adapter, state_path, monkeypatch
):
    def broken_put(self, key, entry):
        raise _template.StateFileError("disk full")

    monkeypatch.setattr(_template.StateFile, "put", broken_put)
    p = _provider(adapter, state_path)
    await _use(p)
    assert p.last_template["status"] == "fresh"
    templates = [c for c in adapter.created if c.labels["openenv"] == "template"]
    assert templates and all(t.id in adapter.deleted for t in templates)
    bases = [
        c
        for f in adapter.forks
        if f["labels"] is None
        for c in adapter.copies[f["parent"]]
    ]
    assert bases and all(b in adapter.deleted for b in bases)
    assert list(adapter.live) == []


@pytest.mark.asyncio
async def test_an_unreadable_state_file_starts_fresh(adapter, tmp_path):
    state_dir = tmp_path / "is-a-dir.json"
    state_dir.mkdir()
    p = _provider(adapter, str(state_dir))
    await _use(p)
    assert p.last_template["status"] == "fresh"
    assert [e[2] for e in adapter.events if e[0] == "create"] == ["server"]


@pytest.mark.asyncio
async def test_the_warm_up_stops_when_the_edge_refuses(adapter, state_path):
    adapter.bad_headers_once = True  # the base's port answers 401
    p = _provider(adapter, state_path)
    t = time.monotonic()
    await _use(p)
    assert time.monotonic() - t < 30
    (entry,) = _entries(state_path).values()
    assert entry["warmed"] is False


def test_a_corrupt_state_file_is_ignored(state_path, caplog):
    os.makedirs(os.path.dirname(state_path))
    with open(state_path, "w") as handle:
        handle.write("{not json")
    state = _template.StateFile(state_path)
    with caplog.at_level(logging.WARNING, logger="openenv_prized"):
        assert state.get("k") is None
    assert "unreadable template state" in caplog.text
    state.put("k", {"sandbox_id": "sbx_1"})
    assert state.get("k") == {"sandbox_id": "sbx_1"}
    assert state.forget("k", "sbx_other") is False
    assert state.forget("k", "sbx_1") is True and state.get("k") is None


def test_the_default_state_path(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENENV_PRIZED_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert _template.default_state_path() == str(
        tmp_path / "openenv-prized" / "templates.json"
    )
    monkeypatch.setenv("OPENENV_PRIZED_STATE", str(tmp_path / "mine.json"))
    assert _template.StateFile().path == str(tmp_path / "mine.json")


def test_template_auto_arguments_are_checked(adapter, state_path):
    with pytest.raises(ValueError, match="drop fork_base"):
        _provider(adapter, state_path, fork_base=True)
    with pytest.raises(ValueError, match="template_auto_delete_min"):
        _provider(adapter, state_path, template_auto_delete_min=-1)
    p = _provider(adapter, state_path, image="registry.hf.space/x:latest")
    with pytest.raises(ValueError, match="not registry images"):
        p.start_container()
    assert adapter.created == []


# ---------------------------------------------------------------------------
# The launcher, for real (with a stand-in for uvicorn)
# ---------------------------------------------------------------------------
_FAKE_UVICORN = """
import os, random, sys, time, json
def main():
    root = os.environ["LAUNCH_ROOT"]
    with open(os.path.join(root, "argv.json"), "w") as f:
        json.dump({"argv": sys.argv, "path0": sys.path[0], "cwd": os.getcwd()}, f)
    module = __import__(sys.argv[1].partition(":")[0])
    while True:  # a server: answer draws on request
        req = os.path.join(root, "draw.req")
        if os.path.exists(req):
            os.unlink(req)
            np = sys.modules.get("numpy")
            out = {"random": random.random(), "hook_seed": getattr(module, "SEEN", None)}
            with open(os.path.join(root, "draw.out.tmp"), "w") as f:
                json.dump(out, f)
            os.replace(os.path.join(root, "draw.out.tmp"), os.path.join(root, "draw.out"))
        time.sleep(0.01)
"""

_APP = """
SEEN = None
def prized_reseed(seed):
    global SEEN
    SEEN = seed
app = object()
"""


def _draw(root) -> dict:
    out = os.path.join(root, "draw.out")
    if os.path.exists(out):
        os.unlink(out)
    open(os.path.join(root, "draw.req"), "w").close()
    deadline = time.monotonic() + 10
    while not os.path.exists(out):
        assert time.monotonic() < deadline, "the fake server did not answer"
        time.sleep(0.01)
    with open(out) as handle:
        return json.load(handle)


def test_the_launcher_reseeds_its_server_when_signalled(tmp_path):
    root = tmp_path / "openenv"
    root.mkdir()
    (root / "prized_launch.py").write_text(_template.LAUNCHER)
    stubs = tmp_path / "stubs"
    (stubs / "uvicorn").mkdir(parents=True)
    (stubs / "uvicorn" / "__init__.py").write_text("")
    (stubs / "uvicorn" / "main.py").write_text(textwrap.dedent(_FAKE_UVICORN))
    envdir = tmp_path / "env"
    envdir.mkdir()
    (envdir / "myapp.py").write_text(textwrap.dedent(_APP))
    env = {
        **os.environ,
        "PYTHONPATH": str(stubs),
        "LAUNCH_ROOT": str(root),
        "PYTHONHASHSEED": "0",
    }
    script = _template.reseed_script(str(root))
    # No launcher yet: exit 10.
    assert subprocess.run(["sh", "-c", script]).returncode == 10

    server = subprocess.Popen(
        [sys.executable, str(root / "prized_launch.py"), "myapp:app", "--port", "8000"],
        cwd=envdir,
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while not (root / "argv.json").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        seen = json.loads((root / "argv.json").read_text())
        assert seen["argv"] == ["uvicorn", "myapp:app", "--port", "8000"]
        assert seen["path0"] == str(envdir) == seen["cwd"]
        assert (root / "reseed.pid").read_text() == str(server.pid)

        before = _draw(root)
        assert before["hook_seed"] is None
        results = []
        for generation in (1, 2):
            done = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True, timeout=30
            )
            assert done.returncode == 0, done
            report = json.loads(done.stdout)
            assert report["generation"] == generation
            assert report["random"] is True and report["hook"] == "called"
            results.append(report)
            drawn = _draw(root)
            assert drawn["hook_seed"] == report["seed"]
            # The draw is the one random.seed(seed) gives: the reseed took.
            import random as stdlib_random

            assert drawn["random"] == stdlib_random.Random(report["seed"]).random()
        assert results[0]["seed"] != results[1]["seed"]
    finally:
        server.kill()
        server.wait(timeout=10)
    # The process is gone: exit 11.
    assert subprocess.run(["sh", "-c", script]).returncode == 11


def test_reseed_waits_bounded_for_a_silent_process(tmp_path):
    """A process that never writes reseed.json makes the script exit 12."""
    root = tmp_path / "openenv"
    root.mkdir()
    sleeper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGUSR2, lambda *a: None); "
            "time.sleep(30)",
        ]
    )
    try:
        time.sleep(1.0)  # its handler is in place
        (root / "reseed.pid").write_text(str(sleeper.pid))
        t = time.monotonic()
        code = subprocess.run(
            ["sh", "-c", _template.reseed_script(str(root))]
        ).returncode
        assert code == 12 and time.monotonic() - t < 15
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)


def test_dir_digest_ignores_times_not_bytes():
    import io
    import tarfile

    def pack(data: bytes, mtime: int) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("./a.py")
            info.size, info.mtime = len(data), mtime
            archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    assert _template.dir_digest(pack(b"x", 1)) == _template.dir_digest(pack(b"x", 2))
    assert _template.dir_digest(pack(b"x", 1)) != _template.dir_digest(pack(b"y", 1))


def test_threads_wait_for_the_build_lock(state_path):
    state = _template.StateFile(state_path)
    order: list = []

    def hold():
        with state.building("k"):
            order.append("a-in")
            time.sleep(0.3)
            order.append("a-out")

    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.1)
    with state.building("k"):
        order.append("b-in")
    thread.join()
    assert order == ["a-in", "a-out", "b-in"]
