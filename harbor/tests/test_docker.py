"""Docker mode (_docker.py): compose inside a prized-docker-v1 sandbox.

The fakes stand in for the Prized API (templates that are active, not active
yet, or paid-only) and for the sandbox's Docker (networks, containers and
their attachments), so the tests check what each container can reach in each
of a trial's phases, not only the commands sent.
"""

import io
import json
import posixpath
import shlex
import tarfile
import time

import pytest
import yaml
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from prized import ExecResult, PrizedError

from harbor_prized import PrizedEnvironment
from harbor_prized import _docker as _docker_module
from harbor_prized import _environment as _module

HOME = "/home/prized"
COMPOSE = f"{HOME}/harbor/compose"
PROJECT = f"{HOME}/harbor/environment"
STAGING = f"{HOME}/harbor/staging/"
UNTAR = 'mkdir -p "$1" && tar --no-same-owner -xzf - -C "$1"'
DOCKER = "/usr/bin/docker"
EGRESS = "harbor-prized-egress"

PUBLIC = NetworkPolicy(network_mode=NetworkMode.PUBLIC)
NO_NETWORK = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
EMPTY_ALLOWLIST = NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=[])
DOCKERFILE = {"Dockerfile": "FROM ubuntu:24.04\n"}
OK = ExecResult(stdout="", stderr="", exit_code=0)


class _Files:
    def __init__(self) -> None:
        self.content: dict[str, bytes] = {}

    async def write(self, path, data, mode=None):
        # The files API reaches only the sandbox user's home.
        assert path.startswith(HOME + "/"), path
        self.content[path] = data

    async def read_bytes(self, path):
        assert path.startswith(HOME + "/"), path
        if path not in self.content:
            raise PrizedError("not_found")
        return self.content[path]


class FakeDocker:
    """The sandbox's dockerd and compose CLI, for the commands Docker mode
    sends: networks (internal or not), the project's containers, and which
    networks each one is attached to. ``can_reach_internet`` and
    ``can_reach`` answer what a container could reach."""

    def __init__(self, files: _Files, environment_dir) -> None:
        self.files = files
        self.environment_dir = environment_dir
        self.networks: dict[str, bool] = {"bridge": False}  # name: internal
        self.containers: dict[str, dict] = {}  # id: service, mode, networks
        self.compose: list[list[str]] = []  # every compose argv
        self.connects: list[tuple[str, str]] = []
        self.stuck = False  # network connect/disconnect answers 0 but does nothing
        self.resolved_extra: dict = {}  # what includes/extends would add to config
        self.garble_inspect = False
        self.drop_inspect_line = False
        self.symlinks: dict[str, str] = {}  # path on the sandbox: where it points

    # -- what a container could reach --------------------------------------

    def _netns(self, cid: str) -> dict:
        c = self.containers[cid]
        if c["mode"].startswith("container:"):
            return self._netns(c["mode"].split(":", 1)[1])
        return c

    def container(self, service: str) -> str:
        return next(i for i, c in self.containers.items() if c["service"] == service)

    def can_reach_internet(self, service: str) -> bool:
        c = self._netns(self.container(service))
        if c["mode"] == "none":
            return False
        return any(not self.networks[n] for n in c["networks"])

    def can_reach(self, a: str, b: str) -> bool:
        na = self._netns(self.container(a))["networks"]
        nb = self._netns(self.container(b))["networks"]
        return bool(na & nb) or na is nb

    # -- commands ------------------------------------------------------------

    def _compose_docs(self, files: list[str]) -> list[dict]:
        docs = []
        for path in files:
            if path.startswith(PROJECT + "/"):
                text = (self.environment_dir / path[len(PROJECT) + 1 :]).read_text()
            elif path in self.files.content:
                text = self.files.content[path].decode()
            else:
                continue
            docs.append(yaml.safe_load(text) or {})
        return docs

    @staticmethod
    def _long_volume(volume) -> dict:
        """As Compose writes a volume: long form, a bind's source absolute
        against the project folder, ``~`` expanded, named volumes by name."""
        if isinstance(volume, str):
            parts = volume.split(":")
            if len(parts) == 1:
                volume = {"type": "volume", "target": parts[0]}
            else:
                src = parts[0]
                kind = "bind" if src.startswith(("/", ".", "~")) else "volume"
                volume = {"type": kind, "source": src, "target": parts[1]}
        source = str(volume.get("source") or "")
        if volume.get("type") == "bind" and source and not source.startswith("/"):
            if source.startswith("~"):
                source = HOME + source[1:]
            else:
                source = f"{PROJECT}/{source}"
            volume = {**volume, "source": posixpath.normpath(source)}
        return volume

    def _resolved(self, files: list[str]) -> dict:
        """``docker compose config --format json``: the files merged, with
        services' volumes merged by target and written in long form."""
        services: dict[str, dict] = {}
        top: dict[str, dict[str, dict]] = {
            "networks": {},
            "volumes": {},
            "secrets": {},
            "configs": {},
        }
        for doc in [*self._compose_docs(files), self.resolved_extra]:
            for key, entries in top.items():
                for name, spec in (doc.get(key) or {}).items():
                    entries.setdefault(name, {}).update(spec or {})
            for name, spec in (doc.get("services") or {}).items():
                merged = services.setdefault(name, {})
                volumes = {
                    v["target"]: v
                    for v in map(self._long_volume, merged.get("volumes") or [])
                }
                for v in map(self._long_volume, (spec or {}).get("volumes") or []):
                    volumes[v["target"]] = v
                merged.update(spec or {})
                merged["volumes"] = list(volumes.values())
        networks = top["networks"]
        if any(
            not s.get("network_mode") and not s.get("networks")
            for s in services.values()
        ):
            networks.setdefault("default", {"name": "harbor_default"})
        return {"name": "harbor", "services": services, **top}

    def _up(self, files: list[str]) -> None:
        docs = self._compose_docs(files)
        services: dict[str, dict] = {"main": {}}
        networks: dict[str, bool] = {"default": False}
        for doc in docs:
            for name, spec in (doc.get("networks") or {}).items():
                networks[name] = bool((spec or {}).get("internal")) or networks.get(
                    name, False
                )
            for name, spec in (doc.get("services") or {}).items():
                services.setdefault(name, {}).update(spec or {})
        for name, internal in networks.items():
            self.networks[f"harbor_{name}"] = internal
        for name, spec in services.items():
            cid = f"{name}0123456789abcdef"
            mode = str(spec.get("network_mode") or "")
            nets = spec.get("networks") or ["default"]
            if isinstance(nets, dict):
                nets = list(nets)
            self.containers[cid] = {
                "service": name,
                "mode": mode,
                "networks": set() if mode else {f"harbor_{n}" for n in nets},
            }
        for c in self.containers.values():
            if c["mode"].startswith("service:"):
                c["mode"] = "container:" + self.container(c["mode"].split(":", 1)[1])
            elif not c["mode"]:
                c["mode"] = min(c["networks"])

    def run(self, cmd: str, env) -> ExecResult | None:
        if cmd.startswith(f"{DOCKER} info"):
            return ExecResult(stdout="29.9.2\n", stderr="", exit_code=0)
        if (
            cmd.startswith(f"{DOCKER} network inspect {EGRESS} ")
            and "network create" in cmd
        ):
            self.networks.setdefault(EGRESS, False)
            return OK
        if cmd.startswith(f"{DOCKER} compose "):
            argv = shlex.split(cmd.split(" < ")[0].split(" > ")[0])
            self.compose.append(argv)
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
            sub = argv[argv.index(files[-1]) + 1 :] if files else []
            if sub[:2] == ["up", "-d"]:
                self._up(files)
            if sub == ["config", "--format", "json"]:
                assert not any(f.endswith("docker-compose-network.json") for f in files)
                return ExecResult(
                    stdout=json.dumps(self._resolved(files)), stderr="", exit_code=0
                )
            return None
        if cmd.startswith(
            f"{DOCKER} ps -aq --filter label=com.docker.compose.project=harbor"
        ):
            return ExecResult(
                stdout="".join(f"{i[:12]}\n" for i in self.containers),
                stderr="",
                exit_code=0,
            )
        if cmd.startswith(f"{DOCKER} inspect -f "):
            argv = shlex.split(cmd)
            assert argv[3].startswith(
                '{{.Id}} {{index .Config.Labels "com.docker.compose.service"}} '
            )
            lines = []
            for short in argv[4:]:
                cid = next(i for i in self.containers if i.startswith(short))
                c = self.containers[cid]
                on = "on" if EGRESS in c["networks"] else "off"
                privileged = "true" if c.get("privileged") else "false"
                nets = "".join(f"{n}," for n in sorted(c["networks"]))
                if c["mode"] == "none":
                    nets = "none,"
                lines.append(
                    f"{cid} {c['service']} {c['mode']} {on} {privileged} nets:{nets}"
                )
            if self.garble_inspect:
                lines[-1] = "Template parsing error"
            if self.drop_inspect_line:
                lines = lines[:-1]
            return ExecResult(stdout="\n".join(lines) + "\n", stderr="", exit_code=0)
        if cmd.startswith("readlink -m -- "):
            out = ""
            for path in shlex.split(cmd)[3:]:
                for link, target in self.symlinks.items():
                    if path == link or path.startswith(link + "/"):
                        path = target + path[len(link) :]
                out += path + "\n"
            return ExecResult(stdout=out, stderr="", exit_code=0)
        if cmd.startswith(f"{DOCKER} network inspect -f "):
            argv = shlex.split(cmd)
            assert argv[4] == "{{.Name}} {{.Internal}}"
            out = "".join(
                f"{n} {'true' if self.networks[n] else 'false'}\n" for n in argv[5:]
            )
            return ExecResult(stdout=out, stderr="", exit_code=0)
        if cmd.startswith(
            (f"{DOCKER} network connect ", f"{DOCKER} network disconnect ")
        ):
            argv = shlex.split(cmd)
            net, cid = argv[-2], argv[-1]
            mode = self.containers[cid]["mode"]
            if mode == "none" or mode.startswith("container:") or mode == "host":
                # As dockerd answers: such a container has no network of its own.
                return ExecResult(
                    stdout="",
                    stderr="Error response from daemon: container sharing network "
                    "namespace with another container or host cannot be connected "
                    "to any other network",
                    exit_code=1,
                )
            self.connects.append((argv[2], self.containers[cid]["service"]))
            if not self.stuck:
                if argv[2] == "connect":
                    self.containers[cid]["networks"].add(net)
                else:
                    self.containers[cid]["networks"].discard(net)
            return OK
        return None


class _Sandbox:
    id = "sbx_docker"

    def __init__(self, environment_dir=None) -> None:
        self.files = _Files()
        self.execs: list[dict] = []
        self.answer = None
        self.deleted = self.closed = self.paused = False
        self.docker = FakeDocker(self.files, environment_dir)

    async def exec(self, cmd, timeout=None, full_output=False, env=None):
        self.execs.append({"cmd": cmd, "timeout": timeout, "env": env})
        if self.answer is not None:
            result = self.answer(cmd)
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
        return self.docker.run(cmd, env) or OK

    async def delete(self):
        self.deleted = True

    async def pause(self):
        self.paused = True

    async def keep_alive(self):
        pass

    async def close(self):
        self.closed = True

    def compose_calls(self) -> list[list[str]]:
        return [
            shlex.split(e["cmd"])
            for e in self.execs
            if e["cmd"].startswith("/usr/bin/docker compose")
        ]


class FakePrized:
    """The create endpoint, with each template's state as the API answers it."""

    PAYMENT = (
        "This template runs on paid workspaces only, and the free credit does "
        "not count. A top-up (from $5) or a plan lifts this: "
        "https://prized.dev/dashboard/billing?lift=sandboxes"
    )

    def __init__(self, docker_state: str = "active") -> None:
        self.templates = {"prized-base-v1": "active", "prized-docker-v1": docker_state}
        self.creates: list[dict] = []
        self.sandboxes: list[_Sandbox] = []
        self.environment_dir = None

    async def create(self, template, **kwargs):
        self.creates.append({"template": template, **kwargs})
        state = self.templates.get(template)
        if state is None:
            raise PrizedError("template_not_found", status=404)
        if state == "deprecated":
            raise PrizedError("template_inactive", status=409)
        if state == "paid-only":
            raise PrizedError("template_needs_payment", self.PAYMENT, status=402)
        sandbox = _Sandbox(self.environment_dir)
        self.sandboxes.append(sandbox)
        return sandbox


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    # Refusals are remembered per process: each test starts without one.
    monkeypatch.setattr(_module, "_docker_refusal", None)
    monkeypatch.setattr(_module, "_network_refused_at", None)


@pytest.fixture
def prized(monkeypatch, tmp_path):
    api = FakePrized()
    api.environment_dir = tmp_path / "environment"
    monkeypatch.setattr(_module.AsyncSandbox, "create", api.create)

    async def no_put(sandbox, network):
        raise AssertionError("Docker mode never asks Prized to switch the network")

    monkeypatch.setattr(_module, "put_network", no_put)
    return api


def _environment(
    tmp_path,
    *,
    files=None,
    network_policy=None,
    phase_network_policies=None,
    ek=None,
    **config,
):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(exist_ok=True)
    for name, text in (files or {}).items():
        (environment_dir / name).write_text(text)
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return PrizedEnvironment(
        environment_dir=environment_dir,
        environment_name="docker-test",
        session_id="docker-test",
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(**config),
        network_policy=network_policy,
        phase_network_policies=phase_network_policies,
        **({"docker": "true"} if ek is None else ek),
    )


def _fake_create(monkeypatch, sandbox, created=None):
    async def create(template, **kwargs):
        if created is not None:
            created.update(template=template, **kwargs)
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)


async def _started(tmp_path, monkeypatch, force_build=False, **kwargs):
    environment = _environment(tmp_path, **kwargs)
    sandbox = _Sandbox(tmp_path / "environment")
    created = {}
    _fake_create(monkeypatch, sandbox, created)
    await environment.start(force_build=force_build)
    sandbox.execs_at_start = len(sandbox.execs)
    return environment, sandbox, created


def _files_flags(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]


def _staged_after(argv: list[str], op: str) -> str:
    path = argv[argv.index(op) + 1]
    assert path.startswith(STAGING), path
    return path


def _sub(argv: list[str]) -> list[str]:
    return argv[argv.index(_files_flags(argv)[-1]) + 1 :]


# -- which tasks run in Docker mode ---------------------------------------------


def test_a_prebuilt_image_task_stays_native(tmp_path):
    environment = _environment(tmp_path, docker_image="ubuntu:24.04", ek={})
    assert not environment._docker
    assert environment._template == "prized-base-v1"
    assert not environment.capabilities.docker_compose


def test_a_dockerfile_task_runs_in_docker_mode_by_itself(tmp_path):
    environment = _environment(tmp_path, files=DOCKERFILE, ek={})
    assert environment._docker and environment._docker_auto
    assert environment._template == "prized-docker-v1"
    caps = environment.capabilities
    assert caps.docker_compose and caps.disable_internet
    # Allowlists only without hosts (Harbor refuses entries no capability names).
    assert caps.network_allowlist and not caps.network_allowlist_hostnames
    assert caps.dynamic_network_policy


def test_a_compose_task_runs_in_docker_mode_even_with_an_image(tmp_path):
    environment = _environment(
        tmp_path,
        files={"docker-compose.yaml": "services:\n  db:\n    image: redis:7-alpine\n"},
        docker_image="ubuntu:24.04",
        ek={},
    )
    assert environment._docker


def test_docker_false_keeps_the_old_refusal(tmp_path):
    with pytest.raises(ValueError) as info:
        _environment(tmp_path, files=DOCKERFILE, ek={"docker": "false"})
    assert str(info.value) == _module._DOCKERFILE_REFUSED


def test_docker_true_or_a_docker_template_runs_a_prebuilt_task_in_docker(tmp_path):
    assert _environment(tmp_path, docker_image="ubuntu:24.04")._docker
    implied = _environment(
        tmp_path, docker_image="ubuntu:24.04", ek={"template": "prized-docker-v1"}
    )
    assert implied._docker and implied._template == "prized-docker-v1"
    other = _environment(
        tmp_path, files=DOCKERFILE, ek={"docker_template": "prized-docker-v2"}
    )
    assert other._template == "prized-docker-v2"
    # A native template named for native tasks doesn't turn Docker mode off.
    pinned = _environment(tmp_path, files=DOCKERFILE, ek={"template": "prized-base-v1"})
    assert pinned._docker and pinned._template == "prized-docker-v1"


def test_harbors_coerced_flags_are_understood(tmp_path):
    assert _environment(
        tmp_path, docker_image="ubuntu:24.04", ek={"docker": True}
    )._docker
    assert _environment(tmp_path, docker_image="ubuntu:24.04", ek={"docker": 1})._docker
    with pytest.raises(ValueError, match="Building from a Dockerfile"):
        _environment(tmp_path, files=DOCKERFILE, ek={"docker": 0})


def test_bad_docker_flag_is_refused(tmp_path):
    with pytest.raises(ValueError, match="auto, true or false"):
        _environment(tmp_path, docker_image="ubuntu:24.04", ek={"docker": "maybe"})


def test_docker_mode_still_needs_a_definition_and_a_disk_that_fits(tmp_path):
    with pytest.raises(FileNotFoundError):
        _environment(tmp_path, ek={})
    with pytest.raises(ValueError, match="at most 128 GB"):
        _environment(tmp_path, files=DOCKERFILE, storage_mb=200 * 1024, ek={})


def test_docker_mode_adds_memory_for_the_daemons(tmp_path):
    environment = _environment(
        tmp_path, files=DOCKERFILE, cpus=1, memory_mb=2048, ek={}
    )
    assert environment._shape() == (1, 2560, 30)


# -- where Prized refuses the Docker template -------------------------------------


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ("deprecated", "409 template_inactive"),
        (None, "404 template_not_found"),
        ("paid-only", "402 template_needs_payment: This template runs on paid"),
    ],
)
async def test_a_refused_template_keeps_the_old_refusal(tmp_path, prized, state, why):
    if state is None:
        del prized.templates["prized-docker-v1"]
    else:
        prized.templates["prized-docker-v1"] = state
    environment = _environment(tmp_path, files=DOCKERFILE, ek={})
    with pytest.raises(RuntimeError) as info:
        await environment.start(force_build=False)
    message = str(info.value)
    assert message.startswith(_module._DOCKERFILE_REFUSED + " ")
    assert f"({why}" in message
    assert [c["template"] for c in prized.creates] == ["prized-docker-v1"]
    assert prized.sandboxes == []  # nothing was made, nothing to delete


async def test_a_refusal_is_remembered_so_later_trials_make_no_sandbox(
    tmp_path, prized, monkeypatch
):
    prized.templates["prized-docker-v1"] = "deprecated"
    with pytest.raises(RuntimeError):
        await _environment(tmp_path, files=DOCKERFILE, ek={}).start(force_build=False)
    # The next trial is built (a refusal here would end Harbor's whole job)
    # and refused when it starts, before a sandbox is made: its own error.
    later = _environment(tmp_path, files=DOCKERFILE, ek={})
    with pytest.raises(RuntimeError) as info:
        await later.start(force_build=False)
    assert str(info.value).startswith(_module._DOCKERFILE_REFUSED)
    assert "409 template_inactive" in str(info.value)
    # Native tasks are not touched by it, nor is another Docker template.
    assert not _environment(tmp_path, docker_image="ubuntu:24.04", ek={})._docker
    _environment(tmp_path, files=DOCKERFILE, ek={"docker_template": "prized-docker-v2"})
    assert len(prized.creates) == 1
    # Ten minutes on, the template may be active: the next trial asks again.
    prized.templates["prized-docker-v1"] = "active"
    when, template, why = _module._docker_refusal
    monkeypatch.setattr(
        _module,
        "_docker_refusal",
        (when - _module._DOCKER_REFUSAL_TTL_SEC - 1, template, why),
    )
    environment = _environment(tmp_path, files=DOCKERFILE, ek={})
    await environment.start(force_build=False)
    assert prized.creates[-1]["template"] == "prized-docker-v1"


async def test_a_refused_compose_task_keeps_the_compose_refusal(tmp_path, prized):
    prized.templates["prized-docker-v1"] = "deprecated"
    environment = _environment(
        tmp_path,
        files={"docker-compose.yaml": "services:\n  db:\n    image: redis:7-alpine\n"},
        docker_image="ubuntu:24.04",
        ek={},
    )
    with pytest.raises(RuntimeError) as info:
        await environment.start(force_build=False)
    assert str(info.value).startswith(_module._COMPOSE_REFUSED + " ")
    # Without an image, the missing image was named first, as before.
    no_image = _environment(
        tmp_path,
        files={"docker-compose.yaml": "services:\n  db:\n    image: redis:7-alpine\n"},
        ek={"docker_template": "prized-docker-v3"},
    )
    with pytest.raises(RuntimeError) as info:
        await no_image.start(force_build=False)
    assert str(info.value).startswith(_module._DOCKERFILE_REFUSED + " ")


async def test_docker_true_on_a_refused_template_points_at_the_box_route(
    tmp_path, prized
):
    prized.templates["prized-docker-v1"] = "deprecated"
    environment = _environment(tmp_path, docker_image="ubuntu:24.04")
    with pytest.raises(RuntimeError, match="`-e docker` on a Prized box"):
        await environment.start(force_build=False)
    assert len(prized.creates) == 1  # not retried


async def test_without_docker_mode_a_missing_template_is_the_sdk_error(
    tmp_path, monkeypatch
):
    environment = _environment(
        tmp_path, docker_image="ubuntu:24.04", ek={"template": "no-such-template"}
    )

    async def create(template, **kwargs):
        raise PrizedError("template_not_found", status=404)

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    with pytest.raises(PrizedError, match="template_not_found"):
        await environment.start(force_build=False)


async def test_prizeds_network_switch_being_unavailable_does_not_refuse_docker_mode(
    tmp_path, monkeypatch, prized
):
    # Native mode remembers that Prized can't switch a sandbox's network yet;
    # Docker mode doesn't need that switch.
    monkeypatch.setattr(_module, "_network_refused_at", time.monotonic())
    _environment(
        tmp_path,
        files=DOCKERFILE,
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    # A prebuilt task with a closed phase goes to Docker mode by itself too.
    routed = _environment(
        tmp_path,
        docker_image="ubuntu:24.04",
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    assert routed._docker and routed._docker_for_network
    native = _environment(
        tmp_path,
        docker_image="ubuntu:24.04",
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={"docker": "false"},
    )
    with pytest.raises(RuntimeError, match="can't turn a sandbox's network off"):
        await native.start(force_build=False)
    assert prized.creates == []  # refused before a sandbox is made


# -- start --------------------------------------------------------------------------


async def test_start_builds_the_dockerfile_in_the_sandbox(tmp_path, prized):
    environment = _environment(
        tmp_path, files=DOCKERFILE, cpus=2, memory_mb=4096, workdir="/app", ek={}
    )
    await environment.start(force_build=False)
    (sandbox,) = prized.sandboxes
    created = prized.creates[0]
    assert created["template"] == "prized-docker-v1"
    assert (created["vcpu"], created["memory_mb"], created["disk_gb"]) == (2, 4608, 30)
    assert sandbox.execs[0]["cmd"].startswith("/usr/bin/docker info")
    subs = [_sub(c) for c in sandbox.compose_calls()]
    assert subs[:3] == [
        ["build"],
        ["pull", "--ignore-buildable", "--ignore-pull-failures", "--quiet"],
        ["up", "-d"],
    ]
    up = sandbox.compose_calls()[2]
    assert up[up.index("-p") + 1] == "harbor"
    assert up[up.index("--project-directory") + 1] == PROJECT
    assert _files_flags(up) == [
        f"{COMPOSE}/docker-compose-resources.json",
        f"{COMPOSE}/docker-compose-build.yaml",
        f"{COMPOSE}/docker-compose-environment.json",
        f"{COMPOSE}/docker-compose-mounts.json",
    ]
    # The project folder holds the task's environment (the build context).
    unpack = [e["cmd"] for e in sandbox.execs if f"-C {PROJECT}" in e["cmd"]]
    assert unpack and unpack[0].startswith(f"mkdir -p {PROJECT} && tar -xzf {STAGING}")
    # As Harbor's Docker provider: the task's cpus and memory cap main.
    resources = json.loads(
        sandbox.files.content[f"{COMPOSE}/docker-compose-resources.json"]
    )
    main = resources["services"]["main"]
    assert (main["cpus"], main["mem_limit"]) == (2.0, "4096m")
    # A public task: no network override, no egress network, main reaches out.
    assert f"{COMPOSE}/docker-compose-network.json" not in sandbox.files.content
    assert EGRESS not in sandbox.docker.networks
    assert sandbox.docker.can_reach_internet("main")
    assert any(
        c[-6:] == ["-u", "root", "main", "bash", "-c", "mkdir -p /app"]
        for c in sandbox.compose_calls()
    )


async def test_start_runs_compose_from_the_prebuilt_image(tmp_path, monkeypatch):
    _, sandbox, created = await _started(
        tmp_path,
        monkeypatch,
        docker_image="alexgshaw/mailman:20251031",
        cpus=1,
        memory_mb=2048,
    )
    assert created["template"] == "prized-docker-v1"
    up = next(c for c in sandbox.compose_calls() if _sub(c) == ["up", "-d"])
    assert f"{COMPOSE}/docker-compose-prebuilt.yaml" in _files_flags(up)
    compose_env = next(e["env"] for e in sandbox.execs if e["cmd"].endswith(" up -d"))
    assert compose_env["PREBUILT_IMAGE_NAME"] == "alexgshaw/mailman:20251031"
    assert compose_env["CONTEXT_DIR"] == PROJECT
    # The CLI keeps its own config dir whatever HOME the task's env sets.
    assert compose_env["DOCKER_CONFIG"] == f"{HOME}/.docker"


async def test_force_build_builds_a_dockerfile_even_with_an_image(
    tmp_path, monkeypatch
):
    _, sandbox, _ = await _started(
        tmp_path,
        monkeypatch,
        force_build=True,
        files=DOCKERFILE,
        docker_image="ubuntu:24.04",
    )
    up = next(c for c in sandbox.compose_calls() if _sub(c) == ["up", "-d"])
    assert f"{COMPOSE}/docker-compose-build.yaml" in _files_flags(up)


async def test_a_failed_build_fails_the_start_and_deletes(tmp_path, prized):
    environment = _environment(tmp_path, files=DOCKERFILE, ek={})
    original = _Sandbox.exec

    async def failing(self, cmd, **kwargs):
        if cmd.startswith("/usr/bin/docker compose") and cmd.endswith(" build"):
            return ExecResult(
                stdout="", stderr="E: Unable to locate package", exit_code=1
            )
        return await original(self, cmd, **kwargs)

    _Sandbox.exec = failing
    try:
        with pytest.raises(RuntimeError, match="docker compose build failed"):
            await environment.start(force_build=False)
    finally:
        _Sandbox.exec = original
    assert prized.sandboxes[0].deleted


async def test_a_failed_pull_is_left_to_up(tmp_path, prized):
    environment = _environment(tmp_path, files=DOCKERFILE, ek={})
    original = _Sandbox.exec

    async def failing(self, cmd, **kwargs):
        if " pull --ignore-buildable" in cmd:
            return ExecResult(stdout="", stderr="pull access denied", exit_code=1)
        return await original(self, cmd, **kwargs)

    _Sandbox.exec = failing
    try:
        await environment.start(force_build=False)
    finally:
        _Sandbox.exec = original
    assert any(_sub(c) == ["up", "-d"] for c in prized.sandboxes[0].compose_calls())


# -- network policies, enforced by Docker ---------------------------------------------


async def test_a_swe_marathon_trial_switches_the_network_by_phase(tmp_path, prized):
    # Public setup, no network for the agent (an empty allowlist) and for the
    # verifier: Harbor switches to each phase and back.
    environment = _environment(
        tmp_path,
        files=DOCKERFILE,
        network_policy=PUBLIC,
        phase_network_policies=[EMPTY_ALLOWLIST, NO_NETWORK],
        ek={},
    )
    await environment.start(force_build=False)
    (sandbox,) = prized.sandboxes
    docker = sandbox.docker
    # Built and pulled with the network; up with internal networks only.
    assert docker.networks["harbor_default"] is True
    assert docker.networks[EGRESS] is False
    override = json.loads(
        sandbox.files.content[f"{COMPOSE}/docker-compose-network.json"]
    )
    # Internal, and isolated: no address on the sandbox's side of the bridge.
    assert override == {"networks": {"default": _docker_module._CLOSED_NETWORK}}
    assert _docker_module._CLOSED_NETWORK["driver_opts"] == {
        "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
        "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
    }
    assert docker.can_reach_internet("main")  # public setup

    await environment.set_network_policy(EMPTY_ALLOWLIST)
    assert not docker.can_reach_internet("main")
    await environment.set_network_policy(PUBLIC)
    assert docker.can_reach_internet("main")
    await environment.set_network_policy(NO_NETWORK)
    assert not docker.can_reach_internet("main")
    assert docker.connects == [
        ("connect", "main"),
        ("disconnect", "main"),
        ("connect", "main"),
        ("disconnect", "main"),
    ]


async def test_a_static_no_network_trial_never_reaches_out(tmp_path, prized):
    environment = _environment(
        tmp_path,
        files=DOCKERFILE,
        network_policy=NO_NETWORK,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    await environment.start(force_build=False)
    docker = prized.sandboxes[0].docker
    assert not docker.can_reach_internet("main")
    await environment.set_network_policy(NO_NETWORK)  # the same: nothing to do
    assert docker.connects == []


async def test_sidecars_stay_reachable_and_closed_with_main(tmp_path, prized):
    compose = (
        "services:\n"
        "  main:\n    networks: [back]\n"
        "  db:\n    image: postgres:16-alpine\n    networks: [back]\n"
        "  probe:\n    image: alpine\n    network_mode: service:main\n"
        "networks:\n  back: {}\n"
    )
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    await environment.start(force_build=False)
    sandbox = prized.sandboxes[0]
    docker = sandbox.docker
    flags = _files_flags(
        next(c for c in sandbox.compose_calls() if _sub(c) == ["up", "-d"])
    )
    assert flags.index(f"{PROJECT}/docker-compose.yaml") == 2
    assert flags[-2:] == [
        f"{COMPOSE}/docker-compose-network.json",
        f"{COMPOSE}/docker-compose-mounts.json",
    ]
    assert all(docker.can_reach_internet(s) for s in ("main", "db", "probe"))
    await environment.set_network_policy(NO_NETWORK)
    assert not any(docker.can_reach_internet(s) for s in ("main", "db", "probe"))
    assert docker.can_reach("main", "db") and docker.can_reach("probe", "db")


@pytest.mark.parametrize(
    ("service", "match"),
    [
        ("    network_mode: host\n", "network_mode 'host'"),
        ("    privileged: true\n", "privileged"),
        ("    cap_add: [NET_ADMIN]\n", "privileged"),
        ("    cap_add: [CAP_SYS_ADMIN]\n", "privileged"),
        ("    pid: host\n", "privileged"),
        ("    volumes: [/var/run/docker.sock:/var/run/docker.sock]\n", "Docker socket"),
        (
            "    volumes:\n      - type: bind\n        source: /run\n        target: /h\n",
            "Docker socket",
        ),
        ("    volumes: ['/usr:/host-usr']\n", "binds /usr"),
        ("    volumes: ['../:/up']\n", "binds /home/prized/harbor "),
        ("    volumes: ['~/:/home-of-prized']\n", "binds /home/prized "),
        ("    devices: ['/dev/net/tun']\n", "binds devices"),
        ("    device_cgroup_rules: ['b *:* rwm']\n", "binds devices"),
        (
            (
                "    volumes: ['hostrun:/r']\nvolumes:\n  hostrun:\n    driver: local\n"
                "    driver_opts: {type: none, o: bind, device: /run}\n"
            ),
            "volume 'hostrun' mounts /run",
        ),
        (
            "    volumes: ['x:/x']\nvolumes:\n  x:\n    driver: nfs-plugin\n",
            "'nfs-plugin' driver",
        ),
        (
            "    secrets: [s]\nsecrets:\n  s:\n    file: /run/docker.sock\n",
            "secret 's' mounts /run/docker.sock",
        ),
    ],
)
async def test_restricted_trials_refuse_what_docker_could_not_hold_closed(
    tmp_path, prized, service, match
):
    # Refused where Harbor builds the environment: no sandbox is made.
    compose = "services:\n  helper:\n    image: alpine\n" + service
    with pytest.raises(ValueError, match=match):
        _environment(
            tmp_path,
            files={**DOCKERFILE, "docker-compose.yaml": compose},
            network_policy=PUBLIC,
            phase_network_policies=[NO_NETWORK],
            ek={},
        )
    assert prized.creates == []


async def test_a_symlink_out_of_the_task_folder_is_refused(tmp_path, prized):
    # The upload keeps symlinks: ./data may point at the Docker socket's folder.
    compose = "services:\n  main:\n    volumes: ['./data:/data']\n"
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=NO_NETWORK,
        ek={},
    )
    original = FakeDocker.__init__

    def init(self, *args):
        original(self, *args)
        self.symlinks = {f"{PROJECT}/data": "/run"}

    FakeDocker.__init__ = init
    try:
        with pytest.raises(ValueError, match="which is /run on the sandbox"):
            await environment.start(force_build=False)
    finally:
        FakeDocker.__init__ = original
    assert prized.sandboxes[0].deleted


async def test_a_home_that_is_a_link_is_not_a_reason_to_refuse(tmp_path, prized):
    compose = "services:\n  main:\n    volumes: ['./data:/data']\n"
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=NO_NETWORK,
        ek={},
    )
    original = FakeDocker.__init__

    def init(self, *args):
        original(self, *args)
        self.symlinks = {HOME: "/data/home/prized"}

    FakeDocker.__init__ = init
    try:
        await environment.start(force_build=False)
    finally:
        FakeDocker.__init__ = original
    assert not prized.sandboxes[0].docker.can_reach_internet("main")


async def test_variables_and_tmpfs_volumes_are_left_to_compose(tmp_path, prized):
    # A long-form bind from a variable resolves under Harbor's mounts, and a
    # tmpfs named volume mounts nothing from the sandbox: neither is refused.
    compose = (
        "services:\n  main:\n    volumes:\n"
        "      - {type: bind, source: '${HOST_VERIFIER_LOGS_PATH}', target: /v}\n"
        "      - pg:/pg\n"
        "volumes:\n  pg:\n    driver: local\n"
        "    driver_opts: {type: tmpfs, device: tmpfs, o: size=256m}\n"
    )
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=NO_NETWORK,
        ek={},
    )
    original = FakeDocker._long_volume

    def long(volume):
        v = original(volume)
        if "HOST_VERIFIER_LOGS_PATH" in str(v.get("source")):
            v = {**v, "source": f"{HOME}/harbor/mounts/logs/verifier"}
        return v

    FakeDocker._long_volume = staticmethod(long)
    try:
        await environment.start(force_build=False)
    finally:
        FakeDocker._long_volume = staticmethod(original)
    assert not prized.sandboxes[0].docker.can_reach_internet("main")


async def test_compose_reads_a_copy_of_the_tasks_env_file(tmp_path, prized):
    environment = _environment(tmp_path, files={**DOCKERFILE, ".env": "TAG=1\n"}, ek={})
    await environment.start(force_build=False)
    sandbox = prized.sandboxes[0]
    env_file = f"{HOME}/harbor/compose/project.env"
    assert any(f"cp {PROJECT}/.env {env_file}" in e["cmd"] for e in sandbox.execs)
    for argv in sandbox.compose_calls():
        assert argv[argv.index("--env-file") + 1] == env_file


async def test_compose_runs_against_the_sandboxs_own_daemon(tmp_path, prized):
    environment = _environment(tmp_path, files=DOCKERFILE, ek={})
    await environment.start(force_build=False)
    sandbox = prized.sandboxes[0]
    compose_env = next(e["env"] for e in sandbox.execs if e["cmd"].endswith(" up -d"))
    # A .env the task's containers could rewrite never picks the daemon.
    assert compose_env["DOCKER_HOST"] == "unix:///var/run/docker.sock"


async def test_the_projects_own_folders_may_be_bound(tmp_path, prized):
    compose = (
        "services:\n  main:\n    volumes: ['./data:/data', '.:/app', 'cache:/cache']\n"
        "  side:\n    image: alpine\n    volumes:\n"
        "      - {type: bind, source: ./conf, target: /conf}\n"
        "      - {type: bind, source: data2, target: /data2}\n"
        "      - {type: tmpfs, target: /tmp}\n"
        "      - ./scratch:/scratch:ro\n"
        "    secrets: [s]\n"
        "secrets:\n  s:\n    file: ./secret.txt\n"
    )
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=NO_NETWORK,
        ek={},
    )
    await environment.start(force_build=False)
    assert not prized.sandboxes[0].docker.can_reach_internet("main")


async def test_an_external_network_is_refused_for_restricted_trials(tmp_path, prized):
    compose = "services: {}\nnetworks:\n  shared:\n    external: true\n"
    with pytest.raises(ValueError, match="external network"):
        _environment(
            tmp_path,
            files={**DOCKERFILE, "docker-compose.yaml": compose},
            network_policy=NO_NETWORK,
            ek={},
        )


async def test_the_check_reads_the_project_as_compose_resolves_it(tmp_path, prized):
    # extends, include or a variable can bring what the task's YAML doesn't
    # say: the check reads `docker compose config`, not the files.
    compose = "services:\n  helper:\n    extends: {file: base.yaml, service: x}\n"
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    original = FakeDocker.__init__

    def init(self, *args):
        original(self, *args)
        self.resolved_extra = {"services": {"helper": {"privileged": True}}}

    FakeDocker.__init__ = init
    try:
        with pytest.raises(ValueError, match="'helper' is privileged"):
            await environment.start(force_build=False)
    finally:
        FakeDocker.__init__ = original
    assert prized.sandboxes[0].deleted


async def test_a_network_with_another_driver_is_refused(tmp_path, prized):
    compose = "services: {}\nnetworks:\n  lan:\n    driver: macvlan\n"
    with pytest.raises(ValueError, match="'macvlan' driver"):
        _environment(
            tmp_path,
            files={**DOCKERFILE, "docker-compose.yaml": compose},
            network_policy=NO_NETWORK,
            ek={},
        )


async def test_a_task_yaml_the_early_check_cant_read_is_left_to_the_sandbox(
    tmp_path, prized
):
    compose = "services:\n  main: !reset {}\n"  # a Compose tag safe_load refuses
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=NO_NETWORK,
        ek={},
    )
    original = FakeDocker._compose_docs

    def docs(self, files):
        # Compose reads the tag; the fake just leaves the task's file out.
        return original(self, [f for f in files if not f.startswith(PROJECT + "/")])

    FakeDocker._compose_docs = docs
    try:
        await environment.start(force_build=False)
    finally:
        FakeDocker._compose_docs = original
    assert not prized.sandboxes[0].docker.can_reach_internet("main")


async def test_every_task_network_is_closed(tmp_path, prized):
    compose = (
        "services:\n  main:\n    networks: [back]\n"
        "networks:\n  back:\n    driver_opts: {com.docker.network.bridge.name: br-x}\n"
    )
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=NO_NETWORK,
        ek={},
    )
    await environment.start(force_build=False)
    sandbox = prized.sandboxes[0]
    override = json.loads(
        sandbox.files.content[f"{COMPOSE}/docker-compose-network.json"]
    )
    assert override["networks"]["back"] == _docker_module._CLOSED_NETWORK
    assert not sandbox.docker.can_reach_internet("main")


async def test_an_unreadable_container_fails_the_switch_closed(tmp_path, prized):
    environment = _environment(
        tmp_path,
        files=DOCKERFILE,
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    await environment.start(force_build=False)
    prized.sandboxes[0].docker.garble_inspect = True
    with pytest.raises(RuntimeError, match="unexpected line"):
        await environment.set_network_policy(NO_NETWORK)


async def test_a_switch_without_main_fails(tmp_path, prized):
    environment = _environment(
        tmp_path,
        files=DOCKERFILE,
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    await environment.start(force_build=False)
    prized.sandboxes[0].docker.containers.clear()
    with pytest.raises(RuntimeError, match="main service's container is gone"):
        await environment.set_network_policy(NO_NETWORK)


async def test_a_main_without_a_network_of_its_own_switches_quietly(tmp_path, prized):
    compose = "services:\n  main:\n    network_mode: none\n"
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    await environment.start(force_build=False)
    await environment.set_network_policy(NO_NETWORK)
    await environment.set_network_policy(PUBLIC)
    assert not prized.sandboxes[0].docker.can_reach_internet("main")


async def test_public_trials_keep_the_tasks_own_networking(tmp_path, prized):
    compose = "services:\n  helper:\n    image: alpine\n    privileged: true\n"
    environment = _environment(
        tmp_path, files={**DOCKERFILE, "docker-compose.yaml": compose}, ek={}
    )
    await environment.start(force_build=False)
    assert (
        f"{COMPOSE}/docker-compose-network.json"
        not in prized.sandboxes[0].files.content
    )


async def test_a_switch_that_does_not_take_fails_loudly(tmp_path, prized):
    environment = _environment(
        tmp_path,
        files=DOCKERFILE,
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    await environment.start(force_build=False)
    prized.sandboxes[0].docker.stuck = True
    with pytest.raises(RuntimeError, match="still on"):
        await environment.set_network_policy(NO_NETWORK)


async def test_a_container_on_the_hosts_network_counts_as_open(tmp_path, prized):
    environment = _environment(
        tmp_path,
        files=DOCKERFILE,
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
        ek={},
    )
    await environment.start(force_build=False)
    docker = prized.sandboxes[0].docker
    docker.containers["rogue0123"] = {
        "service": "rogue",
        "mode": "host",
        "networks": set(),
    }
    with pytest.raises(RuntimeError, match="still on for rogue0123"):
        await environment.set_network_policy(NO_NETWORK)


async def _restricted_started(tmp_path, prized, **kwargs):
    environment = _environment(
        tmp_path,
        files=DOCKERFILE,
        network_policy=kwargs.pop("network_policy", PUBLIC),
        phase_network_policies=kwargs.pop("phases", [NO_NETWORK]),
        ek={},
    )
    await environment.start(force_build=False)
    return environment, prized.sandboxes[0].docker


async def test_a_container_on_a_network_that_reaches_out_fails_the_switch(
    tmp_path, prized
):
    environment, docker = await _restricted_started(tmp_path, prized)
    docker.containers[docker.container("main")]["networks"].add("bridge")
    with pytest.raises(RuntimeError, match="doesn't hold internal: bridge"):
        await environment.set_network_policy(NO_NETWORK)


async def test_a_privileged_container_fails_the_switch(tmp_path, prized):
    environment, docker = await _restricted_started(tmp_path, prized)
    docker.containers[docker.container("main")]["privileged"] = True
    with pytest.raises(RuntimeError, match="still on"):
        await environment.set_network_policy(NO_NETWORK)


async def test_a_container_inspect_leaves_out_fails_the_switch(tmp_path, prized):
    environment, docker = await _restricted_started(tmp_path, prized)
    docker.drop_inspect_line = True
    with pytest.raises(RuntimeError, match="described 0 of 1 containers"):
        await environment.set_network_policy(NO_NETWORK)


async def test_a_restricted_baseline_with_public_phases_closes_again(tmp_path, prized):
    # The baseline is closed and the phase open: after the phase, Harbor
    # restores the baseline, which must close the network again.
    environment, docker = await _restricted_started(
        tmp_path, prized, network_policy=NO_NETWORK, phases=[PUBLIC]
    )
    assert not docker.can_reach_internet("main")
    await environment.set_network_policy(PUBLIC)
    assert docker.can_reach_internet("main")
    await environment.set_network_policy(NO_NETWORK)
    assert not docker.can_reach_internet("main")


async def test_bind_mounts_live_under_the_home(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="ubuntu:24.04")
    environment._mounts = [
        {"type": "bind", "source": str(tmp_path / "agent"), "target": "/logs/agent"},
        {
            "type": "bind",
            "source": str(tmp_path / "x"),
            "target": "/data/../data",
            "read_only": True,
        },
    ]
    mounts = f"{HOME}/harbor/mounts"
    assert environment._compose._resolve_volumes() == [
        {"type": "bind", "source": f"{mounts}/logs/agent", "target": "/logs/agent"},
        {
            "type": "bind",
            "source": f"{mounts}/data",
            "target": "/data",
            "read_only": True,
        },
    ]
    sandbox = _Sandbox(tmp_path / "environment")
    _fake_create(monkeypatch, sandbox)
    await environment.start(force_build=False)
    both = f"{mounts}/logs/agent {mounts}/data"
    assert any(
        e["cmd"] == f"mkdir -p {both} && chmod 777 {both}" for e in sandbox.execs
    )


async def test_a_daemon_that_never_answers_fails_the_start_and_deletes(
    tmp_path, monkeypatch
):
    environment = _environment(
        tmp_path,
        docker_image="ubuntu:24.04",
        ek={"docker": "true", "dockerd_wait_sec": 0},
    )
    sandbox = _Sandbox(tmp_path / "environment")
    missing = ExecResult(
        stdout="",
        stderr="bash: /usr/bin/docker: No such file or directory",
        exit_code=127,
    )
    sandbox.answer = lambda cmd: (
        missing if cmd.startswith("/usr/bin/docker info") else None
    )
    _fake_create(monkeypatch, sandbox)
    with pytest.raises(RuntimeError, match="prized-docker-v1"):
        await environment.start(force_build=False)
    assert sandbox.deleted
    # No docker CLI at all (exit 127): not a Docker template; asked once.
    environment = _environment(tmp_path, docker_image="ubuntu:24.04")
    sandbox = _Sandbox(tmp_path / "environment")
    sandbox.answer = lambda cmd: (
        missing if cmd.startswith("/usr/bin/docker info") else None
    )
    _fake_create(monkeypatch, sandbox)
    with pytest.raises(RuntimeError, match="prized-docker-v1"):
        await environment.start(force_build=False)
    assert sum(e["cmd"].startswith("/usr/bin/docker info") for e in sandbox.execs) == 1


async def test_a_hung_docker_info_is_asked_again(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="ubuntu:24.04")
    sandbox = _Sandbox(tmp_path / "environment")
    infos = []

    def answer(cmd):
        if cmd.startswith("/usr/bin/docker info"):
            infos.append(cmd)
            if len(infos) == 1:
                return _module.CommandTimeoutError(
                    ExecResult(stdout="", stderr="", exit_code=-1), 30.0
                )
        return None

    real_sleep = _docker_module.asyncio.sleep

    async def no_sleep(_):
        await real_sleep(0)  # yields, so the heartbeat's loop can't spin

    sandbox.answer = answer
    _fake_create(monkeypatch, sandbox)
    monkeypatch.setattr(_docker_module.asyncio, "sleep", no_sleep)
    await environment.start(force_build=False)
    assert len(infos) == 2


async def test_a_failed_up_fails_the_start(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="ubuntu:24.04")
    sandbox = _Sandbox(tmp_path / "environment")
    refused = ExecResult(stdout="", stderr="toomanyrequests", exit_code=1)
    sandbox.answer = lambda cmd: refused if cmd.endswith(" up -d") else None
    _fake_create(monkeypatch, sandbox)
    with pytest.raises(
        RuntimeError, match=r"docker compose up failed: .*toomanyrequests"
    ):
        await environment.start(force_build=False)
    assert sandbox.deleted


# -- commands -----------------------------------------------------------------------


async def test_the_task_env_is_mains_environment(tmp_path, monkeypatch):
    _, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04", env={"TASK_VAR": "t"}
    )
    staged = sandbox.files.content[f"{COMPOSE}/docker-compose-environment.json"]
    assert json.loads(staged)["services"]["main"]["environment"]["TASK_VAR"] == "t"


async def test_exec_runs_a_non_login_bash_in_main(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04", workdir="/app"
    )
    sandbox.execs.clear()
    await environment.exec("echo hi", env={"A": "1"}, user="git", timeout_sec=30)
    argv = sandbox.compose_calls()[0]
    tail = argv[argv.index("exec") :]
    assert tail[:4] == ["exec", "-T", "-w", "/app"]
    assert "A=1" in tail
    # bash -c, not -lc: a login shell would replace the image's PATH (any Harbor).
    assert tail[-6:] == ["-u", "git", "main", "bash", "-c", "echo hi"]
    assert sandbox.execs[0]["timeout"] == 30


async def test_a_long_command_goes_through_a_staged_file(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    command = "echo '" + "x" * 40_000 + "'"
    await environment.exec(command)
    host = sandbox.execs[-1]["cmd"]
    staged = host.split("$(cat ", 1)[1].split(")", 1)[0]
    assert staged.startswith(STAGING)
    assert sandbox.files.content[staged] == command.encode()
    assert host.startswith(
        f'c="$(cat {staged})" && rm -f {staged} && exec /usr/bin/docker'
    )
    assert host.endswith(' main bash -c "$c"') and len(host) < 2000


async def test_sidecar_commands_use_sh(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    await environment.service_exec("redis-cli ping", service="db")
    argv = sandbox.compose_calls()[0]
    assert argv[-4:] == ["db", "sh", "-c", "redis-cli ping"]


# -- files --------------------------------------------------------------------------


async def test_upload_file_streams_a_root_owned_tar_into_main(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    source = tmp_path / "solve.sh"
    source.write_text("echo solved\n")
    source.chmod(0o755)
    seen = {}

    def answer(cmd):
        if UNTAR in cmd:
            argv = shlex.split(cmd)
            seen["argv"] = argv
            seen["tar"] = sandbox.files.content[_staged_after(argv, "<")]

    sandbox.answer = answer
    await environment.upload_file(source, "/solution/solve.sh")
    argv = seen["argv"]
    head = argv[argv.index("exec") :][:8]
    assert head == ["exec", "-T", "-u", "0", "main", "sh", "-c", UNTAR]
    assert argv[argv.index(UNTAR) + 1 : argv.index("<")] == ["sh", "/solution"]
    with tarfile.open(fileobj=io.BytesIO(seen["tar"])) as tar:
        (member,) = tar.getmembers()
        assert (member.name, member.uid, member.gid) == ("solve.sh", 0, 0)
        assert member.mode == 0o755
        assert tar.extractfile(member).read() == b"echo solved\n"
    # The staged archive is removed.
    assert sandbox.execs[-1]["cmd"].startswith(f"rm -f {STAGING}")


async def test_upload_falls_back_to_compose_cp_without_tar(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    no_tar = ExecResult(stdout="", stderr="sh: 1: tar: not found", exit_code=127)
    sandbox.answer = lambda cmd: no_tar if UNTAR in cmd else None
    source = tmp_path / "f.txt"
    source.write_text("x")
    await environment.upload_file(source, "/x/f.txt")
    cp = [c for c in sandbox.compose_calls() if "cp" in c]
    assert cp and cp[-1][-1] == "main:/x/f.txt"


async def test_upload_dir_streams_into_main(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("exit 0\n")
    await environment.upload_dir(tests, "/tests")
    argv = next(c for c in sandbox.compose_calls() if UNTAR in c)
    assert argv[argv.index(UNTAR) + 1 : argv.index("<")] == ["sh", "/tests"]


async def test_download_file_streams_out_of_main_as_root(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()

    def answer(cmd):
        if 'cat -- "$1"' in cmd:
            argv = shlex.split(cmd)
            assert argv[argv.index("exec") :][:5] == [
                "exec",
                "-T",
                "-u",
                "0",
                "main",
            ]
            assert argv[argv.index(">") - 1] == "/logs/verifier/reward.txt"
            sandbox.files.content[_staged_after(argv, ">")] = b"1\n"

    sandbox.answer = answer
    target = tmp_path / "out" / "reward.txt"
    await environment.download_file("/logs/verifier/reward.txt", target)
    assert target.read_bytes() == b"1\n"


async def test_download_dir_streams_a_tar_out_of_main(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        data = b"log line\n"
        info = tarfile.TarInfo("./agent.log")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    changed = ExecResult(
        stdout="", stderr="tar: ./agent.log: file changed as we read it", exit_code=1
    )

    def answer(cmd):
        if 'tar -C "$1" -czf - .' in cmd:
            argv = shlex.split(cmd)
            assert argv[argv.index(">") - 1] == "/logs/agent"
            sandbox.files.content[_staged_after(argv, ">")] = payload.getvalue()
            return changed  # logs still being written: accepted, as GNU tar warns
        return None

    sandbox.answer = answer
    await environment.download_dir("/logs/agent", tmp_path / "agent")
    assert (tmp_path / "agent" / "agent.log").read_bytes() == b"log line\n"


async def test_sidecar_downloads_use_compose_cp(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()

    def answer(cmd):
        if cmd.startswith("cp -L /tmp/harbor_"):
            sandbox.files.content[shlex.split(cmd)[3]] = b"state\n"

    sandbox.answer = answer
    await environment.service_download_file(
        "/data/state", tmp_path / "state", service="db"
    )
    cp = next(c for c in sandbox.compose_calls() if "cp" in c)
    assert cp[-2] == "db:/data/state" and cp[-1].startswith("/tmp/harbor_")
    assert (tmp_path / "state").read_bytes() == b"state\n"
