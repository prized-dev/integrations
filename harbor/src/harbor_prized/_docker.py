"""Docker mode: the task's containers run under Docker inside the sandbox.

With ``--ek docker=true`` (or a ``prized-docker-*`` template), each trial's
sandbox starts from ``prized-docker-v1``, whose root ``dockerd`` runs at boot
and whose socket the sandbox user can reach. The task's environment then runs
the way Harbor's Docker provider runs it, with ``docker compose`` inside the
sandbox: the task's Dockerfile is built there (or its ``docker_image`` pulled),
a task's ``docker-compose.yaml`` brings up its sidecars, every command runs in
the ``main`` service with ``docker compose exec``, and files move through
``docker compose cp``. Real users, setuid programs, ``--network none`` for
no-network tasks and per-container resource limits all come from Docker.

Topology::

    Harbor (this machine)
      └── Prized sandbox (prized-docker-v1 microVM)
            ├── dockerd (root, started at boot)
            └── docker compose (run as the sandbox user, through the socket)
                  ├── main      <- the agent and the verifier run here
                  └── sidecars  <- a task's other services

The sandbox user reaches only its home through the files API, so Harbor's
compose files and staged transfers live under ``~/harbor`` and moves to other
paths on the sandbox go through a shell ``mv``/``cp``.
"""

from __future__ import annotations

import asyncio
import io
import json
import posixpath
import shlex
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, override

import yaml
from harbor.constants import MAIN_SERVICE_NAME
from harbor.environments.base import ExecResult
from harbor.environments.definition import should_use_prebuilt_docker_image
from harbor.environments.dind_compose import DinDComposeOps
from harbor.environments.docker import (
    COMPOSE_BUILD_PATH,
    COMPOSE_PREBUILT_PATH,
    ENV_COMPOSE_NAME,
    RESOURCES_COMPOSE_NAME,
    write_mounts_compose_file,
    write_resources_compose_file,
)
from harbor.environments.docker.compose_env import (
    ComposeInfraEnvVars,
    legacy_log_mount_env_vars,
    merge_compose_env,
)
from harbor.environments.docker.docker import _sanitize_docker_image_name
from harbor.environments.tar_transfer import (
    extract_dir_from_bytes,
    pack_dir_to_bytes,
    remote_pack_command,
    remote_unpack_command,
)
from harbor.models.trial.config import ResourceMode, ServiceVolumeConfig
from harbor.utils.env import resolve_env_vars

if TYPE_CHECKING:
    from ._environment import PrizedEnvironment

DOCKER_TEMPLATE = "prized-docker-v1"
_HOME = "/home/prized"
_BASE = f"{_HOME}/harbor"
_DOCKER = "/usr/bin/docker"
_DOCKERD_POLL_SEC = 1.0
# Longer commands go through a staged file: toolboxd runs each command as one
# bash argument (at most 128 KiB), and quoting for compose and the SDK's
# detached runs makes a command several times longer.
_INLINE_COMMAND_LIMIT = 32_000
_NO_NETWORK_NAME = "docker-compose-no-network.json"
# Network modes that would give a service the sandbox's own network.
_OPEN_NETWORK_MODES = ("host", "bridge", "default")


def _single_file_tar(source: Path, name: str) -> bytes:
    """A gzipped tar holding ``source`` as ``name``, owned by root, mode kept."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tar.gettarinfo(str(source), arcname=name)
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        with source.open("rb") as f:
            tar.addfile(info, f)
    return buffer.getvalue()


def _under_home(path: str) -> bool:
    return path == _HOME or path.startswith(_HOME + "/")


class PrizedDocker(DinDComposeOps):
    """Docker Compose inside a ``prized-docker-v1`` sandbox (see module doc)."""

    _COMPOSE_DIR = f"{_BASE}/compose"
    _ENVIRONMENT_DIR = f"{_BASE}/environment"
    _MOUNTS_DIR = f"{_BASE}/mounts"
    _STAGING_DIR = f"{_BASE}/staging"
    _MOUNTS_COMPOSE_NAME = "docker-compose-mounts.json"
    # Downloads never read bind-mount folders on the sandbox directly: files a
    # container made there belong to the container's users. Main's files come
    # out through a root tar stream (download_*), sidecars' through compose cp.
    _SELF_BIND_LOG_DIRS = False
    # A non-login bash keeps the image's PATH and ENV, as `docker exec` does
    # (exec below; Harbor 0.24's DinDComposeOps always used a login shell).
    _MAIN_LOGIN_SHELL = False
    # Large artifact directories take longer than the shared defaults.
    _CP_FILE_TIMEOUT_SEC = 1800
    _CP_DIR_TIMEOUT_SEC = 1800

    def __init__(self, env: PrizedEnvironment):
        self._env = env
        self._use_prebuilt = False

    # -- the sandbox ------------------------------------------------------

    async def _sh(
        self,
        command: str,
        timeout_sec: int | None = 600,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        return await self._env._host_exec(command, timeout_sec=timeout_sec, env=env)

    async def _sh_checked(
        self, command: str, what: str, timeout_sec: int = 600
    ) -> None:
        result = await self._sh(command, timeout_sec=timeout_sec)
        if result.return_code != 0:
            raise RuntimeError(f"{what}: {result.stderr or result.stdout}")

    def _staging_path(self, suffix: str = "") -> str:
        return f"{self._STAGING_DIR}/{uuid.uuid4().hex}{suffix}"

    # -- DinDComposeOps primitives ----------------------------------------

    @override
    async def _host_exec(
        self, command: str, timeout_sec: int | None = None
    ) -> ExecResult:
        return await self._sh(command, timeout_sec=timeout_sec)

    @override
    async def _stage_file_to_host(self, source_path: Path | str, host_path: str):
        data = await asyncio.to_thread(Path(source_path).read_bytes)
        if _under_home(host_path):
            await self._sh_checked(
                f"mkdir -p {shlex.quote(str(Path(host_path).parent))}",
                f"Failed to create the folder for {host_path}",
            )
            await self._env._put(host_path, data)
            return
        staged = self._staging_path()
        await self._env._put(staged, data)
        q = shlex.quote
        await self._sh_checked(
            f"mkdir -p {q(str(Path(host_path).parent))} && mv -f {q(staged)} {q(host_path)}"
            f" || {{ rm -f {q(staged)}; exit 1; }}",
            f"Failed to stage {source_path} at {host_path}",
        )

    @override
    async def _stage_dir_to_host(self, source_dir: Path | str, host_dir: str):
        buffer = await asyncio.to_thread(pack_dir_to_bytes, source_dir, compress=True)
        staged = self._staging_path(".tar.gz")
        await self._env._put(staged, buffer.getvalue())
        try:
            await self._sh_checked(
                remote_unpack_command(staged, host_dir),
                f"Failed to unpack {source_dir} into {host_dir}",
            )
        finally:
            await self._env._host_rm(shlex.quote(staged))

    @override
    async def _fetch_file_from_host(self, host_path: str, target_path: Path | str):
        if _under_home(host_path):
            data = await self._env._get(host_path)
        else:
            staged = self._staging_path()
            try:
                await self._sh_checked(
                    f"cp -L {shlex.quote(host_path)} {shlex.quote(staged)}",
                    f"Failed to read {host_path}",
                )
                data = await self._env._get(staged)
            finally:
                await self._env._host_rm(shlex.quote(staged))
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)

    @override
    async def _fetch_dir_from_host(self, host_dir: str, target_dir: Path | str):
        staged = self._staging_path(".tar.gz")
        try:
            await self._sh_checked(
                remote_pack_command(host_dir, staged), f"Failed to archive {host_dir}"
            )
            data = await self._env._get(staged)
        finally:
            await self._env._host_rm(shlex.quote(staged))
        await asyncio.to_thread(extract_dir_from_bytes, data, target_dir)

    # -- compose ----------------------------------------------------------

    @property
    def _project_name(self) -> str:
        return "harbor"

    def _resolve_volumes(self) -> list[ServiceVolumeConfig]:
        """Each bind mount's source becomes a folder under ~/harbor/mounts on the
        sandbox (Trial's sources are paths on this machine)."""
        volumes: list[ServiceVolumeConfig] = []
        for mount in self._env._mounts:
            if mount.get("type") != "bind":
                volumes.append(mount)
                continue
            target = posixpath.normpath(mount["target"])
            if not target.startswith("/") or target == "/":
                raise ValueError(
                    f"A bind mount needs an absolute target, not {target!r}"
                )
            if not target.startswith("/logs/"):
                self._env.logger.warning(
                    f"Docker mode: the bind mount on {target} starts empty in the "
                    f"sandbox; {mount.get('source')} on this machine is not copied."
                )
            new: ServiceVolumeConfig = {
                "type": "bind",
                "source": f"{self._MOUNTS_DIR}/{target.strip('/')}",
                "target": target,
            }
            if mount.get("read_only"):
                new["read_only"] = True
            if "bind" in mount:
                new["bind"] = mount["bind"]
            volumes.append(new)
        return volumes

    def _infra_env_vars(self) -> dict[str, str]:
        env = self._env
        memory_mb = env._effective_memory_mb
        values = ComposeInfraEnvVars(
            main_image_name=_sanitize_docker_image_name(f"hb__{env.environment_name}"),
            context_dir=self._ENVIRONMENT_DIR,
            prebuilt_image_name=(
                env.task_env_config.docker_image if self._use_prebuilt else None
            ),
            cpus=env._effective_cpus,
            memory=f"{memory_mb}M" if memory_mb else None,
        ).to_env_dict()
        values.update(
            legacy_log_mount_env_vars(self._resolve_volumes(), host_value="source")
        )
        # The CLI's own config, whatever HOME the task's env sets (an unreadable
        # HOME would stop it loading the compose plugin).
        values["DOCKER_CONFIG"] = f"{_HOME}/.docker"
        return values

    def _compose_env_vars(self) -> dict[str, str]:
        env = self._env
        return merge_compose_env(
            user_env={
                **resolve_env_vars(env.task_env_config.env or {}),
                **env._persistent_env,
            },
            infra_env=self._infra_env_vars(),
            logger=env.logger,
        )

    def _extra_compose_paths(self) -> list[str]:
        return [
            f"{self._COMPOSE_DIR}/docker-compose-extra-{index}.yaml"
            for index, _ in enumerate(self._env.extra_docker_compose_paths)
        ]

    def _compose_files(self) -> list[str]:
        """Every compose file on the sandbox, in the order compose reads them."""
        built = COMPOSE_PREBUILT_PATH if self._use_prebuilt else COMPOSE_BUILD_PATH
        files = [
            f"{self._COMPOSE_DIR}/{RESOURCES_COMPOSE_NAME}",
            f"{self._COMPOSE_DIR}/{built.name}",
        ]
        if (self._env.environment_dir / "docker-compose.yaml").exists():
            files.append(f"{self._ENVIRONMENT_DIR}/docker-compose.yaml")
        files += self._extra_compose_paths()
        files.append(f"{self._COMPOSE_DIR}/{ENV_COMPOSE_NAME}")
        if self._env._network_disabled:
            files.append(f"{self._COMPOSE_DIR}/{_NO_NETWORK_NAME}")
        # Last: compose merges volumes by target, and Harbor's log mounts must win.
        files.append(f"{self._COMPOSE_DIR}/{self._MOUNTS_COMPOSE_NAME}")
        return files

    def compose_command(self, subcommand: list[str]) -> str:
        parts = [
            _DOCKER,
            "compose",
            "-p",
            self._project_name,
            "--project-directory",
            self._ENVIRONMENT_DIR,
        ]
        for path in self._compose_files():
            parts += ["-f", path]
        return shlex.join([*parts, *subcommand])

    @override
    async def _compose_exec(
        self, subcommand: list[str], timeout_sec: int | None = None
    ) -> ExecResult:
        return await self._sh(
            self.compose_command(subcommand),
            timeout_sec=timeout_sec,
            env=self._compose_env_vars(),
        )

    async def _write_json_override(self, name: str, write) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            local = Path(temp_dir) / name
            write(local)
            await self._stage_file_to_host(local, f"{self._COMPOSE_DIR}/{name}")

    def no_network_override(self) -> dict:
        """No egress for the whole project: every network it uses becomes
        internal (no route out, service names still resolve), so main still
        reaches its sidecars and the sidecars get no internet either."""
        networks: dict[str, dict] = {"default": {"internal": True}}
        paths = [self._env.environment_dir / "docker-compose.yaml"]
        paths += list(self._env.extra_docker_compose_paths)
        for path in paths:
            if not path.exists():
                continue
            doc = yaml.safe_load(path.read_text()) or {}
            for name, spec in (doc.get("networks") or {}).items():
                if isinstance(spec, dict) and spec.get("external"):
                    raise ValueError(
                        f"allow_internet = false: {path.name} uses the external "
                        f"network {name!r}, which Prized can't close."
                    )
                networks[name] = {"internal": True}
            for service, spec in (doc.get("services") or {}).items():
                mode = str((spec or {}).get("network_mode") or "")
                if mode in _OPEN_NETWORK_MODES:
                    raise ValueError(
                        f"allow_internet = false: service {service!r} in {path.name} "
                        f"uses network_mode {mode!r}, which reaches the internet."
                    )
        return {"networks": networks}

    # -- commands and files in main -----------------------------------------

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
        *,
        service: str | None = None,
    ) -> ExecResult:
        """`docker compose exec` in a service (default main): bash -c in main,
        sh -c in sidecars, as Harbor's Docker provider runs them."""
        service = service or MAIN_SERVICE_NAME
        parts = ["exec", "-T"]
        if cwd:
            parts += ["-w", cwd]
        for key, value in (env or {}).items():
            parts += ["-e", f"{key}={value}"]
        if user is not None:
            parts += ["-u", str(user)]
        shell = ["bash", "-c"] if service == MAIN_SERVICE_NAME else ["sh", "-c"]
        parts += [service, *shell]
        if len(command) <= _INLINE_COMMAND_LIMIT:
            return await self._compose_exec([*parts, command], timeout_sec=timeout_sec)
        staged = self._staging_path(".sh")
        await self._env._put(staged, command.encode())
        q = shlex.quote(staged)
        # The script removes its file before compose runs, so nothing is left.
        return await self._sh(
            f'c="$(cat {q})" && rm -f {q} && exec {self.compose_command(parts)} "$c"',
            timeout_sec=timeout_sec,
            env=self._compose_env_vars(),
        )

    async def _main_root_stream(
        self, shell: str, args: list[str], redirect: str, timeout_sec: int
    ) -> ExecResult:
        """Runs ``sh -c SHELL sh ARGS...`` in main as root, with the sandbox-side
        redirect (``< staged`` or ``> staged``) applied to compose's stdio."""
        # uid 0, not "root": an image without /etc/passwd has no user named root.
        argv = ["exec", "-T", "-u", "0", MAIN_SERVICE_NAME, "sh", "-c", shell, "sh"]
        return await self._sh(
            f"{self.compose_command([*argv, *args])} {redirect}",
            timeout_sec=timeout_sec,
            env=self._compose_env_vars(),
        )

    @staticmethod
    def _missing_tar(result: ExecResult) -> bool:
        return result.return_code == 127 and "tar" in (result.stderr or "")

    async def _upload_tar(self, data: bytes, target_dir: str, what: str) -> bool:
        """Unpacks a gzipped tar into main as root (files root-owned, modes kept,
        parents made). False if the image has no tar."""
        staged = self._staging_path(".tar.gz")
        await self._env._put(staged, data)
        try:
            result = await self._main_root_stream(
                'mkdir -p "$1" && tar --no-same-owner -xzf - -C "$1"',
                [target_dir],
                f"< {shlex.quote(staged)}",
                self._CP_DIR_TIMEOUT_SEC,
            )
        finally:
            await self._env._host_rm(shlex.quote(staged))
        if self._missing_tar(result):
            return False
        if result.return_code != 0:
            raise RuntimeError(
                f"Failed to upload {what}: {result.stderr or result.stdout}"
            )
        return True

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        target = posixpath.normpath(target_path)
        data = await asyncio.to_thread(
            _single_file_tar, source, posixpath.basename(target)
        )
        if not await self._upload_tar(
            data, posixpath.dirname(target) or "/", f"{source} to {target}"
        ):
            await super().upload_file(source_path, target_path)

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        buffer = await asyncio.to_thread(pack_dir_to_bytes, source_dir, compress=True)
        if not await self._upload_tar(
            buffer.getvalue(), target_dir, f"{source_dir} to {target_dir}"
        ):
            await super().upload_dir(source_dir, target_dir)

    @override
    async def download_file(
        self, source_path: str, target_path: Path | str, *, service: str | None = None
    ) -> None:
        if service not in (None, MAIN_SERVICE_NAME):
            return await super().download_file(
                source_path, target_path, service=service
            )
        staged = self._staging_path()
        try:
            result = await self._main_root_stream(
                'cat -- "$1"',
                [source_path],
                f"> {shlex.quote(staged)}",
                self._CP_FILE_TIMEOUT_SEC,
            )
            if result.return_code != 0:
                raise RuntimeError(
                    f"Failed to download {source_path}: {result.stderr or result.stdout}"
                )
            data = await self._env._get(staged)
        finally:
            await self._env._host_rm(shlex.quote(staged))
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)

    @override
    async def download_dir(
        self, source_dir: str, target_dir: Path | str, *, service: str | None = None
    ) -> None:
        if service not in (None, MAIN_SERVICE_NAME):
            return await super().download_dir(source_dir, target_dir, service=service)
        from ._environment import tar_pack_succeeded

        staged = self._staging_path(".tar.gz")
        try:
            result = await self._main_root_stream(
                'tar -C "$1" -czf - .',
                [source_dir],
                f"> {shlex.quote(staged)}",
                self._CP_DIR_TIMEOUT_SEC,
            )
            if self._missing_tar(result):
                return await super().download_dir(source_dir, target_dir)
            if not tar_pack_succeeded(
                result.return_code, stderr=result.stderr, logger=self._env.logger
            ):
                raise RuntimeError(
                    f"Failed to download {source_dir}: {result.stderr or result.stdout}"
                )
            data = await self._env._get(staged)
        finally:
            await self._env._host_rm(shlex.quote(staged))
        await asyncio.to_thread(extract_dir_from_bytes, data, target_dir)

    # -- lifecycle --------------------------------------------------------

    async def wait_for_docker(self, budget_sec: float) -> None:
        """Polls `docker info` until the daemon answers; the sandbox's dockerd
        starts at boot and is normally up before the first command."""
        deadline = asyncio.get_running_loop().time() + budget_sec
        last = ""
        while True:
            try:
                result = await self._sh(
                    f"{_DOCKER} info --format '{{{{.ServerVersion}}}}'", 30
                )
            except RuntimeError as exc:  # a `docker info` that hung: ask again
                last = str(exc)
            else:
                if result.return_code == 0:
                    return
                last = (result.stderr or result.stdout or "").strip()[-500:]
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(_DOCKERD_POLL_SEC)
        hint = ""
        if "No such file" in last or "not found" in last:
            hint = f" Is the template {DOCKER_TEMPLATE} (or another with Docker)?"
        raise RuntimeError(
            f"Docker in the sandbox did not answer within {budget_sec:g} s: {last}.{hint}"
        )

    async def start(self, force_build: bool) -> None:
        env = self._env
        await self.wait_for_docker(env._dockerd_wait_sec)
        await self._sh_checked(
            f"rm -rf {_BASE} && mkdir -p {self._COMPOSE_DIR} {self._STAGING_DIR}",
            "Failed to prepare ~/harbor",
        )
        for path in (COMPOSE_BUILD_PATH, COMPOSE_PREBUILT_PATH):
            await self._stage_file_to_host(path, f"{self._COMPOSE_DIR}/{path.name}")
        if env._network_disabled:
            override = self.no_network_override()
            await self._write_json_override(
                _NO_NETWORK_NAME, lambda path: path.write_text(json.dumps(override))
            )
        await self._write_json_override(
            RESOURCES_COMPOSE_NAME,
            # As Harbor's Docker provider: the task's cpus and memory cap main,
            # which also keeps the daemons' share of the sandbox free.
            lambda path: write_resources_compose_file(
                path,
                cpu_request=env._resource_request_value(
                    "cpu", auto_mode=ResourceMode.LIMIT
                ),
                cpu_limit=env._resource_limit_value(
                    "cpu", auto_mode=ResourceMode.LIMIT
                ),
                memory_request_mb=env._resource_request_value(
                    "memory", auto_mode=ResourceMode.LIMIT
                ),
                memory_limit_mb=env._resource_limit_value(
                    "memory", auto_mode=ResourceMode.LIMIT
                ),
            ),
        )
        await self._stage_env_compose_file(self._COMPOSE_DIR)
        if env.environment_dir.is_dir():
            await self._stage_dir_to_host(env.environment_dir, self._ENVIRONMENT_DIR)
        else:
            await self._sh_checked(
                f"mkdir -p {self._ENVIRONMENT_DIR}",
                "Failed to create the project folder",
            )
        for source, target in zip(
            env.extra_docker_compose_paths, self._extra_compose_paths(), strict=True
        ):
            await self._stage_file_to_host(source, target)
        volumes = self._resolve_volumes()
        await self._write_json_override(
            self._MOUNTS_COMPOSE_NAME,
            lambda path: write_mounts_compose_file(path, volumes),
        )
        binds = [v["source"] for v in volumes if v.get("type") == "bind"]
        if binds:
            # Writable by the container's non-root users too, as on Docker.
            quoted = shlex.join(binds)
            await self._sh_checked(
                f"mkdir -p {quoted} && chmod 777 {quoted}",
                "Failed to create mount folders",
            )

        self._use_prebuilt = should_use_prebuilt_docker_image(
            env.environment_dir,
            docker_image=env.task_env_config.docker_image,
            force_build=force_build,
        )
        build_timeout = round(env.task_env_config.build_timeout_sec)
        result = await self._compose_exec(["build"], timeout_sec=build_timeout)
        if result.return_code != 0:
            raise RuntimeError(
                f"docker compose build failed: {result.stdout} {result.stderr}"
            )
        # up pulls what the build did not make; pulls of big images take a while.
        result = await self._compose_exec(
            ["up", "-d"], timeout_sec=max(build_timeout, 600)
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"docker compose up failed: {result.stdout} {result.stderr}"
            )
        await self._wait_for_main(60)
        workdir = env.task_env_config.workdir
        if workdir and workdir != "/":
            created = await self.exec(
                f"mkdir -p {shlex.quote(workdir)}", cwd="/", user="root"
            )
            if created.return_code != 0:
                raise RuntimeError(f"Failed to create {workdir}: {created.stderr}")

    async def _wait_for_main(self, budget_sec: int) -> None:
        for _ in range(max(1, budget_sec // 2)):
            result = await self._compose_exec(
                ["exec", "-T", MAIN_SERVICE_NAME, "true"], timeout_sec=30
            )
            if result.return_code == 0:
                return
            await asyncio.sleep(2)
        ps = await self._compose_exec(["ps", "-a"], timeout_sec=30)
        raise RuntimeError(
            f"The main service is not running after {budget_sec} s: {ps.stdout}"
        )
