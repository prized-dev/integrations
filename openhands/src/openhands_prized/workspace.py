"""PrizedWorkspace: an OpenHands remote workspace in a Prized sandbox."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

import httpx
from openhands.sdk.workspace import CommandResult, FileOperationResult, RemoteWorkspace
from pydantic import Field, PrivateAttr, SecretStr

from . import _server
from ._proxy import ExecUpstream, IngressUpstream, ProxyServer, ingress_base

logger = logging.getLogger("openhands_prized")

DEFAULT_LABELS = {"openhands": "workspace"}
Connection = Literal["auto", "exec", "ingress"]
OnExit = Literal["delete", "pause", "keep"]


def _sdk_version() -> str:
    return importlib.metadata.version("openhands-sdk")


class PrizedWorkspace(RemoteWorkspace):
    """Runs the OpenHands Agent Server in a Prized sandbox and connects to it.

    The sandbox is created (or forked, or attached to), the Agent Server of the same
    version as your ``openhands-sdk`` is installed and started in it, and a proxy on
    ``127.0.0.1`` carries the SDK's requests and WebSockets to it. Use it like any
    remote workspace::

        with PrizedWorkspace() as workspace:
            conversation = Conversation(agent=agent, workspace=workspace)
            conversation.send_message("Fix the failing test")
            conversation.run()

    The Prized API key comes from ``prized_api_key`` or ``PRIZED_API_KEY``. It stays
    in this process: the sandbox and the Agent Server never see it.
    """

    working_dir: str = Field(default="/home/prized/work", description="Working directory in the sandbox.")
    host: str = Field(default="", description="Set automatically: the local proxy's URL.")
    api_key: str | None = Field(
        default=None, exclude=True, repr=False, description="Set automatically: the Agent Server's session key."
    )

    prized_api_key: SecretStr | None = Field(
        default=None, exclude=True, repr=False, description="Prized API key. Default: PRIZED_API_KEY."
    )
    prized_base_url: str | None = Field(default=None, description="Prized API origin (default https://api.prized.dev).")

    template: str = Field(default="prized-base-v1", description="Sandbox template for a new sandbox.")
    vcpu: int | None = Field(default=None, description="vCPUs (default: the template's).")
    memory_gb: float | None = Field(default=None, description="Memory in GB (default: the template's).")
    disk_gb: int | None = Field(default=None, description="Disk in GB (default: the template's).")
    labels: dict[str, str] = Field(default_factory=dict, description="Labels for a new sandbox.")
    max_runtime_min: int | None = Field(default=None, description="Run time before Prized deletes the sandbox.")
    auto_pause_min: int | None = Field(
        default=None, description="Pause after this long without activity (the workspace keeps it awake while open)."
    )

    sandbox_id: str | None = Field(
        default=None, description="Attach to this existing sandbox instead of creating one (kept on exit)."
    )
    fork_from: str | None = Field(
        default=None,
        description="Fork this running or paused sandbox instead of creating one: with the Agent Server "
        "already installed in it, startup skips the install.",
    )

    agent_server_version: str | None = Field(
        default=None, description="openhands-agent-server version to run (default: your openhands-sdk version)."
    )
    extra_packages: list[str] = Field(
        default_factory=list, description="More pip requirements for the Agent Server's environment."
    )
    server_args: list[str] = Field(default_factory=list, description="More agent-server command-line arguments.")
    server_port: int = Field(default=8000, description="Port of the Agent Server inside the sandbox.")
    env: dict[str, str] = Field(
        default_factory=dict, exclude=True, repr=False, description="Variables for the Agent Server and commands."
    )
    forward_env: list[str] = Field(
        default_factory=list, description="Names of local environment variables to pass to the Agent Server."
    )

    connection: Connection = Field(
        default="exec",
        description="How the proxy reaches the sandbox: 'exec' (Prized's command API, every plan; the "
        "default), 'ingress' (the sandbox's port URL, where turned on; not yet run live), or 'auto' "
        "(ingress if it answers, else exec).",
    )
    host_port: int | None = Field(default=None, description="Local proxy port (default: a free one).")
    on_exit: OnExit | None = Field(
        default=None,
        description="What happens to the sandbox on exit: 'delete' (default for one this workspace made), "
        "'pause' (memory and processes kept, storage billed) or 'keep' (default for an attached one).",
    )
    startup_timeout: float = Field(default=600.0, description="Seconds for install and start.")
    keep_alive_interval: float | None = Field(
        default=120.0, description="Seconds between keep-alives while open (None: off)."
    )

    _sandbox: Any = PrivateAttr(default=None)
    _owned: bool = PrivateAttr(default=False)
    _proxy: ProxyServer | None = PrivateAttr(default=None)
    _transport: str | None = PrivateAttr(default=None)
    _paused: bool = PrivateAttr(default=False)
    _closed: bool = PrivateAttr(default=False)
    _stop_beat: threading.Event = PrivateAttr(default_factory=threading.Event)
    _beat: threading.Thread | None = PrivateAttr(default=None)
    _beat_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _timings: dict[str, float] = PrivateAttr(default_factory=dict)
    _home: str = PrivateAttr(default="/home/prized")

    # ---- startup -------------------------------------------------------------

    def model_post_init(self, context: Any) -> None:
        key = self._key()
        version = _server.check_version(self.agent_server_version or _sdk_version())
        t0 = time.monotonic()
        try:
            self._open_sandbox(key)
            self._timings["sandbox_s"] = round(time.monotonic() - t0, 2)
            t1 = time.monotonic()
            session_key, python = self._ensure_server(version)
            self._timings["server_s"] = round(time.monotonic() - t1, 2)
            self._start_proxy(key, python)
            object.__setattr__(self, "host", self._proxy.url if self._proxy else "")
            object.__setattr__(self, "api_key", session_key)
            self._wait_proxied_health(60.0)
            self._timings["ready_s"] = round(time.monotonic() - t0, 2)
            self._start_heartbeat()
        except BaseException:
            self._teardown(failed=True)
            raise
        logger.info(
            "Prized workspace ready: sandbox %s, transport %s, %s (%.1f s)",
            self._sandbox.id,
            self._transport,
            self.host,
            self._timings["ready_s"],
        )
        super().model_post_init(context)

    def _key(self) -> str:
        key = self.prized_api_key.get_secret_value() if self.prized_api_key else os.environ.get("PRIZED_API_KEY", "")
        if not key:
            raise ValueError(
                "No Prized API key: pass prized_api_key= or set PRIZED_API_KEY "
                "(https://prized.dev/onboarding/sandboxes?via=openhands)"
            )
        return key

    def _connect_kwargs(self, key: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"api_key": key}
        if self.prized_base_url:
            kw["base_url"] = self.prized_base_url
        return kw

    def _open_sandbox(self, key: str) -> None:
        from prized import Sandbox

        kw = self._connect_kwargs(key)
        labels = {**DEFAULT_LABELS, **self.labels}
        if self.sandbox_id:
            self._sandbox, self._owned = Sandbox.connect(self.sandbox_id, **kw), False
        elif self.fork_from:
            parent = Sandbox.connect(self.fork_from, **kw)
            try:
                self._sandbox = parent.fork(1, labels=labels)[0]
            finally:
                parent.close()
            self._owned = True
        else:
            self._sandbox = Sandbox.create(
                self.template,
                vcpu=self.vcpu,
                memory_gb=self.memory_gb,
                disk_gb=self.disk_gb,
                labels=labels,
                max_runtime_min=self.max_runtime_min,
                auto_pause_min=self.auto_pause_min,
                **kw,
            )
            self._owned = True

    def _command_env(self) -> dict[str, str]:
        """Variables for commands: forward_env from this process, then env."""
        env = {name: os.environ[name] for name in self.forward_env if name in os.environ}
        env.update(self.env)
        return env

    def _server_env(self, session_key: str) -> dict[str, str]:
        env = self._command_env()
        env["OH_SESSION_API_KEYS_0"] = session_key
        env.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
        return env

    def _config_hash(self) -> str:
        """What a running server must have been started with to be reused as is."""
        env = self._server_env("")
        env.pop("OH_SESSION_API_KEYS_0")
        blob = json.dumps([sorted(env.items()), self.server_args, self.extra_packages])
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _ensure_server(self, version: str) -> tuple[str, str]:
        sb = self._sandbox
        probe = _server.parse_probe(sb.exec(_server.probe_script(self.server_port), timeout=30).stdout)
        t = time.monotonic()
        install = sb.exec(_server.install_script(version, self.extra_packages), timeout=max(60.0, self.startup_timeout))
        self._timings["install_s"] = round(time.monotonic() - t, 2)
        if install.exit_code != 0:
            raise RuntimeError(f"installing openhands-agent-server {version} failed: {install.stderr[-1500:]}")
        paths = _server.parse_install(install.stdout)
        self._home = paths["HOME"]
        relay = importlib.resources.files("openhands_prized").joinpath("ws_relay.py").read_bytes()
        sb.files.write(f"{self._home}/{_server.STATE}/ws_relay.py", relay, mode=0o644)
        config = self._config_hash()
        if probe.reusable(version, self.server_port) and probe.state.get("config") == config:
            logger.info("Reusing the Agent Server already running in %s", sb.id)
            return probe.state["key"], paths["PY"]
        sb.exec(_server.stop_script(), timeout=30)
        session_key = secrets.token_urlsafe(32)
        t = time.monotonic()
        handle = sb.exec(
            _server.start_command(self.server_port, self.server_args),
            background=True,
            env=self._server_env(session_key),
        )
        state = {
            "pid": handle.pid,
            "run": handle.id,
            "port": self.server_port,
            "version": version,
            "config": config,
            "key": session_key,
        }
        sb.files.write(f"{self._home}/{_server.STATE}/server.json", json.dumps(state), mode=0o600)
        deadline = time.monotonic() + max(30.0, self.startup_timeout)
        while True:
            if sb.exec(_server.health_script(self.server_port), timeout=10).stdout.strip() == "200":
                self._timings["start_s"] = round(time.monotonic() - t, 2)
                return session_key, paths["PY"]
            status = handle.status()
            if not status.running:
                logs = handle.logs()
                raise RuntimeError(
                    f"the Agent Server exited ({status.exit_code}) before it answered: "
                    f"{(logs.stdout + logs.stderr)[-2000:]}"
                )
            if time.monotonic() > deadline:
                raise RuntimeError("the Agent Server did not answer /health in time")
            time.sleep(0.5)

    def _start_proxy(self, key: str, python: str) -> None:
        upstream: Any = None
        template = self._sandbox.info.raw.get("previewUrlTemplate")
        if self.connection in ("auto", "ingress"):
            if template:
                api = self.prized_base_url or "https://api.prized.dev"
                base = ingress_base(template, self.server_port, api)
                status = self._probe_ingress(base, key)
                if status == 200:
                    upstream = IngressUpstream(base, key)
                elif self.connection == "ingress":
                    raise RuntimeError(f"the sandbox's port URL answered {status}; use connection='exec'")
                else:
                    logger.info("Sandbox port URL answered %s; using the exec transport", status)
            elif self.connection == "ingress":
                raise RuntimeError("sandbox port URLs are not turned on for this Prized deployment")
        if upstream is None:
            sandbox_id, kw = self._sandbox.id, self._connect_kwargs(key)

            async def connect() -> Any:
                from prized import AsyncSandbox

                return await AsyncSandbox.connect(sandbox_id, **kw)

            state = f"{self._home}/{_server.STATE}"
            upstream = ExecUpstream(connect, self.server_port, python, f"{state}/ws_relay.py", tmp=f"{state}/tmp")
        self._transport = upstream.name
        self._proxy = ProxyServer(upstream, port=self.host_port)
        self._proxy.start()

    @staticmethod
    def _probe_ingress(base: str, key: str) -> int | str:
        try:
            r = httpx.get(base + "/health", headers={"Prized-Key": key}, timeout=15.0, follow_redirects=False)
            return r.status_code
        except httpx.HTTPError as exc:
            return type(exc).__name__

    def _wait_proxied_health(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last: object = None
        while time.monotonic() < deadline:
            try:
                r = httpx.get(self.host + "/health", timeout=30.0)
                if r.status_code == 200:
                    return
                last = f"HTTP {r.status_code}: {r.text[:200]}"
            except httpx.HTTPError as exc:
                last = exc
            time.sleep(0.5)
        raise RuntimeError(f"the Agent Server did not answer through the proxy: {last}")

    def _start_heartbeat(self) -> None:
        if not self.keep_alive_interval:
            return
        interval = float(self.keep_alive_interval)
        if self.auto_pause_min:  # beat well inside the idle window, or the sandbox pauses mid-run
            interval = min(interval, self.auto_pause_min * 60 / 3)

        def beat() -> None:
            while not self._stop_beat.wait(interval):
                with self._beat_lock:  # never a keep-alive while pause() runs
                    if self._paused:
                        continue
                    try:
                        self._sandbox.keep_alive()
                    except Exception as exc:  # a blip; the next beat tries again
                        logger.debug("keep-alive failed: %s", exc)

        self._beat = threading.Thread(target=beat, name="openhands-prized-keepalive", daemon=True)
        self._beat.start()

    # ---- what it exposes -----------------------------------------------------

    @property
    def sandbox(self) -> Any:
        """The ``prized.Sandbox`` this workspace runs in."""
        return self._sandbox

    @property
    def transport(self) -> str | None:
        """``exec`` or ``ingress``."""
        return self._transport

    @property
    def timings(self) -> dict[str, float]:
        """Seconds spent getting the sandbox, getting the server up, and in all."""
        return dict(self._timings)

    def _abs(self, path: str | Path) -> str:
        p = str(path)
        return p if p.startswith("/") else f"{self.working_dir.rstrip('/')}/{p}"

    def execute_command(self, command: str, cwd: str | Path | None = None, timeout: float = 30.0) -> CommandResult:
        """Run a shell command in the sandbox (straight through Prized, not the proxy)."""
        from prized import CommandTimeoutError

        try:
            self._ensure_running()
            r = self._sandbox.exec(
                command, cwd=self._abs(cwd or self.working_dir), timeout=timeout, env=self._command_env()
            )
        except CommandTimeoutError as exc:
            return CommandResult(
                command=command,
                exit_code=-1,
                stdout=exc.result.stdout,
                stderr=exc.result.stderr + f"\nCommand timed out after {timeout} seconds",
                timeout_occurred=True,
            )
        except Exception as exc:
            return CommandResult(
                command=command, exit_code=-1, stdout="", stderr=f"Prized exec error: {exc}", timeout_occurred=False
            )
        return CommandResult(
            command=command, exit_code=r.exit_code, stdout=r.stdout, stderr=r.stderr, timeout_occurred=r.timed_out
        )

    def file_upload(self, source_path: str | Path | bytes, destination_path: str | Path) -> FileOperationResult:
        """Write a local file (or bytes) into the sandbox."""
        dest = self._abs(destination_path)
        src = "<bytes>" if isinstance(source_path, bytes) else str(source_path)
        try:
            data = source_path if isinstance(source_path, bytes) else Path(source_path).read_bytes()
            self._ensure_running()
            self._sandbox.files.write(dest, data)
            return FileOperationResult(success=True, source_path=src, destination_path=dest, file_size=len(data))
        except Exception as exc:
            return FileOperationResult(success=False, source_path=src, destination_path=dest, error=str(exc))

    def file_download(self, source_path: str | Path, destination_path: str | Path) -> FileOperationResult:
        """Copy a file out of the sandbox."""
        src = self._abs(source_path)
        try:
            self._ensure_running()
            size = self._sandbox.files.download(src, str(destination_path))
            return FileOperationResult(
                success=True, source_path=src, destination_path=str(destination_path), file_size=size
            )
        except Exception as exc:
            return FileOperationResult(
                success=False, source_path=src, destination_path=str(destination_path), error=str(exc)
            )

    def _ensure_running(self) -> None:
        """A command or file call would resume a paused sandbox behind our back (and the
        proxy would keep answering 503): resume it properly first."""
        if self._paused:
            self.resume()

    def pause(self) -> None:
        """Pause the sandbox: compute stops, memory and processes (the Agent Server too) are kept.

        Open WebSockets are closed first (the SDK reconnects after ``resume()``) and the proxy
        answers 503 meanwhile: any command reaching the sandbox would resume it."""
        with self._beat_lock:
            self._paused = True
        if self._proxy is not None and not self._proxy.quiesce(30.0):
            logger.warning("proxied requests still running after 30 s; pausing anyway")
        try:
            self._sandbox.pause()
        except BaseException:
            self._paused = False
            if self._proxy is not None:
                self._proxy.unquiesce()
            raise

    def resume(self) -> None:
        """Resume a paused sandbox and wait for the Agent Server."""
        self._sandbox.resume()
        self._paused = False
        if self._proxy is not None:
            self._proxy.unquiesce()
        self._wait_proxied_health(60.0)

    # ---- shutdown ------------------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.cleanup()

    def __del__(self) -> None:
        if getattr(self, "__pydantic_private__", None) is not None:
            try:
                self.cleanup()
            except Exception:
                pass

    def cleanup(self) -> None:
        """Stop the proxy, then delete, pause or keep the sandbox (``on_exit``)."""
        self._teardown(failed=False)

    def _teardown(self, failed: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_beat.set()
        if self._proxy is not None:
            try:
                # Let open WebSockets' relays stop and clean up before the sandbox is paused
                # (or deleted): one cut off mid-cleanup would be frozen into a paused sandbox.
                self._proxy.quiesce(15.0)
            except Exception as exc:
                logger.debug("quiesce before stop failed: %s", exc)
            try:
                self._proxy.stop()
            except Exception as exc:
                logger.warning("stopping the proxy failed: %s", exc)
            self._proxy = None
        sb = self._sandbox
        if sb is None:
            return
        action = self.on_exit or ("delete" if self._owned else "keep")
        if failed and self._owned:
            action = "delete"  # never leave a half-set-up sandbox billing
        try:
            if action == "delete":
                sb.delete()
            elif action == "pause":
                sb.pause()
        except Exception as exc:
            logger.warning("%s of sandbox %s failed: %s", action, sb.id, exc)
        finally:
            try:
                sb.close()
            except Exception:
                pass
