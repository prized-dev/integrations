# SPDX-License-Identifier: BSD-3-Clause

"""Prized Sandboxes container provider for running OpenEnv environments.

Install: ``pip install openenv-prized``

The provider boots an OpenEnv server inside a Prized sandbox (a Firecracker
microVM) and returns a base URL that ``EnvClient`` connects to. Two kinds of
source are accepted:

- An environment's source, run with ``uv`` the way ``UVProvider`` runs it
  locally, on the default ``prized-base-v1`` template (no Docker needed):
  ``"space:<owner>/<name>"`` (a Hugging Face Space), ``"git+https://<url>"``
  or ``"dir:<local path>"`` (uploaded).
- A Docker/OCI registry image (``"registry.hf.space/openenv-echo-env:latest"``),
  run with Docker inside the ``prized-docker-v1`` template. That template is not
  generally available yet; until it is, run a source.

By default port 8000 stays private: the returned URL is a loopback proxy that
adds the ``Prized-Key`` header Prized's edge requires (the same approach as
``HFSandboxProvider``), so any key with Sandboxes: Write works and nothing is
exposed publicly. ``public=True`` instead makes the port public and returns its
``https://`` URL directly.

Note: the ``RFC 002 security invariant S<n>`` references in this module point to
the **proposed** "Cloud Sandbox Providers" amendment to RFC 002, which is pending
review/sign-off by the RFC authors.
"""

from __future__ import annotations

import copy
import io
import ipaddress
import os
import re
import shlex
import tarfile
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from openenv.core.containers.runtime.providers import ContainerProvider

_DEFAULT_PORT = 8000
_BASE_TEMPLATE = "prized-base-v1"
_DOCKER_TEMPLATE = "prized-docker-v1"
# A sandbox left behind by a crashed trainer is deleted after this many minutes
# of running, or of being paused. Longer runs pass their own values (paid
# workspaces allow up to 7 days of running).
_DEFAULT_MAX_RUNTIME_MIN = 60
_DEFAULT_AUTO_DELETE_MIN = 60
_DEFAULT_INSTALL_TIMEOUT_S = 600.0
# The ASGI app when neither openenv.yaml nor a Dockerfile names one:
# UVProvider's default.
_DEFAULT_APP = "server.app:app"

# Everything the provider puts in the sandbox lives under the sandbox user's
# home: the SDK's file transfers are limited to /home/prized.
_ROOT = "/home/prized/openenv"
_CONTAINER = "openenv-server"

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
# Stop packing a dir: source early, before compressing a huge tree in memory.
_MAX_UNPACKED_BYTES = 512 * 1024 * 1024
_UPLOAD_EXCLUDES = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"}
_DOCKER_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUBDIRECTORY = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")

# Seam for tests: the readiness poll's pause between attempts.
_sleep = time.sleep


def _uv_setup(root: str) -> str:
    """Shell that sets ``$UV``: the template's own ``uv`` when it has one, else
    one installed into a private venv on first use (``prized-base-v1`` ships
    Python and git, not uv). Exits 3 if that install fails."""
    venv = shlex.quote(f"{root}/uv")
    return (
        f'UV="$(command -v uv || echo {venv}/bin/uv)"; '
        f'if [ ! -x "$UV" ]; then '
        f"python3 -m venv {venv} && {venv}/bin/pip install -q 'uv>=0.9' "
        f"|| exit 3; fi; "
    )


def _stop_gracefully(proxy: Any, timeout_s: float = 1.0) -> None:
    """Let the loopback proxy's server shut down on its own first.

    ``_LocalAuthProxy.stop()`` force-exits uvicorn, which cancels the app's
    lifespan task, and Starlette then logs that cancellation as an ERROR with
    a traceback: one per stopped sandbox. A graceful exit (clients have
    already disconnected) ends the lifespan cleanly; ``stop()`` still runs
    after it and forces whatever is left.
    """
    server = getattr(proxy, "_server", None)
    thread = getattr(proxy, "_thread", None)
    if server is None or thread is None:
        return
    server.should_exit = True
    thread.join(timeout=timeout_s)


def _raise_install_error(exc: ImportError) -> None:
    raise RuntimeError(
        "Prized sandbox support requires optional dependencies. "
        "Install them with `pip install openenv-prized`."
    ) from exc


def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_secure_url(url: str) -> str:
    """Enforce https/wss transport (RFC 002 security invariant S1).

    ``EnvClient`` derives its WebSocket URL from the base URL, so a plaintext
    URL would become a cleartext ``ws://`` connection. Plain http is accepted
    only to this machine, the same rule the ``prized`` SDK applies to its own
    API origin: Prized's API answers such a URL only when the caller pointed
    the SDK at a local proxy. The URL is omitted from the error because a
    public port URL is a bearer capability.
    """
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or not parts.netloc:
        raise RuntimeError("Prized returned an invalid port URL.")
    if parts.scheme == "https":
        return url.rstrip("/")
    if parts.scheme == "http" and _is_loopback(parts.hostname):
        return url.rstrip("/")
    raise RuntimeError(
        "Prized returned a non-HTTPS port URL. OpenEnv requires an https/wss "
        "base_url so EnvClient traffic is encrypted. Refusing to connect over "
        "plaintext."
    )


def _parse_source(image: str) -> Dict[str, Optional[str]]:
    """Split an ``image`` argument into its kind and location.

    Returns ``{"kind": "oci" | "git" | "dir", "location", "ref", "subdirectory"}``.
    """
    if not isinstance(image, str) or not image.strip():
        raise ValueError("image must be a non-empty image reference or source")
    image = image.strip()
    source: Dict[str, Optional[str]] = {"ref": None, "subdirectory": None}

    if image.startswith("dir:"):
        path = image[len("dir:") :].strip()
        if not path:
            raise ValueError("dir: source must name a local directory")
        return {**source, "kind": "dir", "location": os.path.realpath(path)}

    if image.startswith("space:"):
        repo, _, ref = image[len("space:") :].strip().partition("@")
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise ValueError(
                "space: source must be a Hugging Face Space id like "
                "'space:openenv/echo_env' (optionally '@<revision>')"
            )
        return {
            **source,
            "kind": "git",
            "location": f"https://huggingface.co/spaces/{repo}",
            "ref": ref or None,
        }

    if image.startswith("git+"):
        # pip/uv syntax: git+https://host/owner/repo[@ref][#subdirectory=path].
        parts = urlsplit(image[len("git+") :].strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("git+ sources must use an https:// URL")
        if "@" in parts.netloc:
            # Never echo the URL: it holds a credential. A token in it would
            # reach the sandbox's command line and the clone's .git/config,
            # where the environment's code can read it.
            raise ValueError(
                "git+ URLs must not carry credentials; they would be readable "
                "inside the sandbox. Use a public repository or a dir: source."
            )
        if parts.query:
            raise ValueError("git+ URLs take no query string")
        subdirectory = None
        if parts.fragment:
            key, _, value = parts.fragment.partition("=")
            if key != "subdirectory" or not _SUBDIRECTORY.fullmatch(value):
                raise ValueError(
                    "the only git+ URL fragment supported is "
                    "#subdirectory=<relative path>"
                )
            if ".." in value.split("/"):
                raise ValueError("#subdirectory must stay inside the repository")
            subdirectory = value
        # The ref follows the repository path; it may contain slashes.
        path, _, ref = parts.path.partition("@")
        return {
            "kind": "git",
            "location": urlunsplit(("https", parts.netloc, path, "", "")),
            "ref": ref or None,
            "subdirectory": subdirectory,
        }

    return {**source, "kind": "oci", "location": image}


def _uvicorn_app(cmd: str) -> Optional[str]:
    """The ``module:attr`` a ``uvicorn ...`` command line serves, if any."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    if "uvicorn" not in [os.path.basename(word) for word in words]:
        return None
    for word in words:
        if re.fullmatch(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*", word):
            return word
    return None


def _tar_directory(path: str) -> bytes:
    """Pack *path* for upload, leaving out VCS data, venvs, caches, ``.env``
    files and symlinks (which could point outside the tree once unpacked)."""
    if not os.path.isdir(path):
        raise ValueError(f"dir: source does not exist or is not a directory: {path}")
    unpacked = 0

    def keep(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        nonlocal unpacked
        parts = info.name.split("/")
        if any(part in _UPLOAD_EXCLUDES for part in parts):
            return None
        base = parts[-1]
        if (
            base.endswith((".pyc", ".pyo"))
            or base == ".env"
            or base.startswith(".env.")
        ):
            return None
        if info.issym():
            return None
        unpacked += info.size
        if unpacked > _MAX_UNPACKED_BYTES:
            raise ValueError(
                "dir: source is larger than 512 MiB unpacked. Push it to a git "
                "repo or a Space instead."
            )
        return info

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(path, arcname=".", filter=keep)
    data = buffer.getvalue()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ValueError(
            f"dir: source packs to {len(data)} bytes; Prized uploads are limited "
            "to 100 MiB per file. Push it to a git repo or a Space instead."
        )
    return data


def _check_docker_env(env_vars: Optional[Dict[str, str]]) -> None:
    """Refuse what docker's ``--env-file`` format cannot carry unchanged."""
    for key, value in (env_vars or {}).items():
        if not _DOCKER_ENV_KEY.fullmatch(key):
            raise ValueError(f"env var name {key!r} cannot be passed to docker run")
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(
                f"env var {key!r} has a line break or NUL, which docker's "
                "--env-file cannot carry"
            )


class _DefaultPrizedAdapter:
    """Thin adapter over the ``prized`` SDK.

    The provider talks to this private adapter instead of spreading SDK details
    through its own logic; tests inject a duck-typed fake in its place.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str],
        base_url: Optional[str],
        edge_url: Optional[str],
    ):
        try:
            from prized import Sandbox
        except ImportError as exc:  # pragma: no cover - exercised via provider tests
            _raise_install_error(exc)

        self._sandbox_cls = Sandbox
        # None means "use PRIZED_API_KEY" and the SDK's default origins.
        self._connection = {
            key: value
            for key, value in (
                ("api_key", api_key),
                ("base_url", base_url),
                ("edge_url", edge_url),
            )
            if value is not None
        }

    def create_sandbox(
        self,
        *,
        template: str,
        vcpu: Optional[int],
        memory_gb: Optional[float],
        disk_gb: Optional[int],
        labels: Optional[Dict[str, str]],
        max_runtime_min: Optional[int],
        auto_pause_min: Optional[int],
        auto_delete_min: Optional[int],
    ) -> Any:
        kwargs: Dict[str, Any] = {
            "vcpu": vcpu,
            "memory_gb": memory_gb,
            "disk_gb": disk_gb,
            "labels": labels,
            "max_runtime_min": max_runtime_min,
            "auto_pause_min": auto_pause_min,
            "auto_delete_min": auto_delete_min,
        }
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        return self._sandbox_cls.create(template, **kwargs, **self._connection)

    def exec(
        self,
        sandbox: Any,
        command: str,
        *,
        timeout: Optional[float] = 60,
        env: Optional[Dict[str, str]] = None,
        background: bool = False,
    ) -> "tuple[int, str]":
        """Run a shell command; return ``(exit_code, stdout)``.

        A non-zero exit is a result, not an exception, in the SDK; callers
        decide what it means. ``background=True`` returns at once with
        ``(0, "")`` while the command keeps running in its own session.
        """
        if background:
            sandbox.exec(command, env=env, background=True)
            return 0, ""
        result = sandbox.exec(command, env=env, timeout=timeout)
        return result.exit_code, result.stdout or ""

    def write(self, sandbox: Any, path: str, data: bytes, *, mode: int) -> None:
        sandbox.files.write(path, data, mode=mode)

    def port_url(self, sandbox: Any, port: int) -> str:
        return sandbox.preview_url(port)

    def port_headers(self, sandbox: Any) -> Dict[str, str]:
        return dict(sandbox.preview_headers())

    def set_port_public(
        self, sandbox: Any, port: int, *, expires_in: Optional[int]
    ) -> str:
        return sandbox.set_port_public(port, True, expires_in=expires_in).url

    def pause(self, sandbox: Any) -> None:
        sandbox.pause()

    def resume(self, sandbox: Any) -> None:
        sandbox.resume()

    def fork(
        self, sandbox: Any, *, count: int, labels: Optional[Dict[str, str]]
    ) -> List[Any]:
        return sandbox.fork(count, labels=labels)

    def sandbox_id(self, sandbox: Any) -> str:
        return sandbox.id

    def delete(self, sandbox: Any) -> None:
        # Close the handle only once the delete went through, so a failed
        # delete can be retried with the same handle.
        sandbox.delete()
        sandbox.close()


class PrizedProvider(ContainerProvider):
    """
    Container provider that runs environments in Prized sandboxes.

    ``start_container``'s ``image`` is one of:

    - ``"space:<owner>/<name>[@<revision>]"``: a Hugging Face Space's source.
    - ``"git+https://<url>[@<ref>][#subdirectory=<path>]"``: a public git
      repository, in pip's syntax (e.g. one environment of a monorepo).
    - ``"dir:<local path>"``: a local environment directory, uploaded
      (without ``.git``, ``.venv``, caches, ``.env`` files or symlinks).
    - A Docker/OCI registry reference (e.g.
      ``"registry.hf.space/openenv-echo-env:latest"``), the form the
      ``ContainerProvider`` contract specifies. It runs with ``docker run``
      inside the ``prized-docker-v1`` template, published on port 8000. That
      template is not generally available yet.

    A source runs on ``prized-base-v1`` without Docker: the provider fetches
    it, runs ``uv sync`` in the directory holding ``openenv.yaml`` (or the
    source root), and starts ``uvicorn`` on the ``app`` that file names (else
    the one its Dockerfile's ``CMD`` serves, else ``server.app:app``), the way
    ``UVProvider`` runs it locally.

    The environment runs untrusted code, so the provider is secure by default:
    https/wss transport (S1); no public ingress unless ``public=True`` is
    passed (S2); the sandbox holds no Prized key, since the key travels only in
    request headers the edge strips; sandbox output is withheld from errors
    unless ``surface_server_logs=True`` (S4); environment variables never go on
    a command line (S5); the sandbox is deleted after ``max_runtime_min``
    minutes of running or ``auto_delete_min`` minutes paused (S6). Prized
    sandboxes have open egress: there is no egress allowlist yet (S3).

    Only one sandbox is active per provider: calling ``start_container`` again
    before ``stop_container()``/``close()`` raises ``RuntimeError`` rather than
    orphaning the running sandbox.

    Beyond the contract, a running environment can be paused (memory and
    processes kept, compute not billed) and resumed, and forked into copies
    that start with the server already running (see :meth:`fork`).

    Examples:

        ```python
        from echo_env import EchoEnv

        # From a Hugging Face Space's source, no Docker needed
        env = await EchoEnv.from_docker_image(
            "space:openenv/echo_env", provider=PrizedProvider()
        )
        async with env:
            await env.reset()
            await env.call_tool("echo_message", message="hi")
        # closing the client deletes the sandbox
        ```
    """

    def __init__(
        self,
        *,
        image: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        api_key: Optional[str] = None,
        template: Optional[str] = None,
        vcpu: Optional[int] = None,
        memory_gb: Optional[float] = None,
        disk_gb: Optional[int] = None,
        labels: Optional[Dict[str, str]] = None,
        max_runtime_min: Optional[int] = _DEFAULT_MAX_RUNTIME_MIN,
        auto_pause_min: Optional[int] = 0,
        auto_delete_min: Optional[int] = _DEFAULT_AUTO_DELETE_MIN,
        public: bool = False,
        public_expires_in: Optional[int] = None,
        cmd: Optional[str] = None,
        install_cmd: Optional[str] = None,
        install_timeout_s: float = _DEFAULT_INSTALL_TIMEOUT_S,
        surface_server_logs: bool = False,
        base_url: Optional[str] = None,
        edge_url: Optional[str] = None,
        _adapter: Any = None,
    ):
        """
        Args:
            image (`str`, *optional*):
                A ``space:``/``git+``/``dir:`` source or a registry image (see the
                class docstring), used when ``start_container()`` is called
                without one.
            env_vars (`dict`, *optional*):
                Environment variables for the server, used when
                ``start_container()`` is called without explicit ``env_vars``.
                They are never written into a command line.
            api_key (`str`, *optional*):
                Prized API key with Sandboxes: Write. Falls back to the
                ``PRIZED_API_KEY`` environment variable.
            template (`str`, *optional*):
                Sandbox template. Defaults to ``prized-base-v1`` for a source and
                ``prized-docker-v1`` for a registry image.
            vcpu, memory_gb, disk_gb (*optional*):
                Sandbox shape; the template's default when omitted.
            labels (`dict`, *optional*):
                Labels on the sandbox (shown in ``prized sandbox ls`` and the
                dashboard; usable for bulk cleanup).
            max_runtime_min (`int`, *optional*, defaults to `60`):
                The sandbox is deleted after this many minutes of running, so a
                crashed trainer cannot leave it billing. ``None`` uses the
                workspace default (24 hours on paid plans).
            auto_pause_min (`int`, *optional*, defaults to `0`):
                Pause after this many minutes without a command or file
                transfer; ``0`` turns it off. Traffic to the server's port does
                not count as activity, and a pause drops open WebSockets, so
                turning it on can end a live episode.
            auto_delete_min (`int`, *optional*, defaults to `60`):
                Delete the sandbox after it has been paused this many minutes
                (a paused sandbox's run time does not count down); ``0`` keeps a
                paused sandbox until it is deleted.
            public (`bool`, *optional*, defaults to `False`):
                Make the port public (anyone with the URL reaches it) and return
                its ``https://`` URL instead of a loopback proxy. Needs the
                workspace owner's key and a paid workspace, and counts toward
                the workspace's 10 public ports (RFC 002 S2: explicit opt-in).
            public_expires_in (`int`, *optional*):
                Seconds a public port stays public (300 to 2592000; Prized's
                default is 7 days). Only with ``public=True``.
            cmd (`str`, *optional*):
                Shell command that starts the server on port 8000. For a source
                it runs in the environment directory; when omitted it is
                ``uv run --no-sync python -m uvicorn <app> ...``. For a registry
                image it replaces the image's command.
            install_cmd (`str`, *optional*):
                Shell command that installs a source's dependencies, run in the
                environment directory with ``$UV`` set. Defaults to
                ``"$UV" sync``.
            install_timeout_s (`float`, *optional*, defaults to `600`):
                Bound on fetching and installing a source, or pulling an image.
            surface_server_logs (`bool`, *optional*, defaults to `False`):
                When `False` (default), sandbox output is withheld from raised
                errors so secrets the workload printed cannot leak into
                orchestrator/CI logs. When `True`, a best-effort redacted,
                length-bounded excerpt is included (S4).
            base_url, edge_url (`str`, *optional*):
                Prized API and edge origins, for a local proxy or another
                deployment. Defaults to Prized's.
            _adapter (`Any`, *optional*):
                Injection seam for tests; a duck-typed fake replaces the SDK.
        """
        if public_expires_in is not None and not public:
            raise ValueError("public_expires_in applies only with public=True")

        self._image = image
        self._env_vars = dict(env_vars) if env_vars is not None else None
        self._template = template
        self._vcpu = vcpu
        self._memory_gb = memory_gb
        self._disk_gb = disk_gb
        self._labels = dict(labels or {})
        self._max_runtime_min = max_runtime_min
        self._auto_pause_min = auto_pause_min
        self._auto_delete_min = auto_delete_min
        self.public = public
        self._public_expires_in = public_expires_in
        self._cmd = cmd
        self._install_cmd = install_cmd
        self._install_timeout_s = install_timeout_s
        self.surface_server_logs = surface_server_logs
        # Where the provider works inside the sandbox (a seam for tests).
        self._root = _ROOT

        self._sandbox: Any = None
        self._base_url: Optional[str] = None
        self._proxy: Any = None
        self._docker = False
        # Injected env-var values, used to scrub captured server output before
        # it is ever surfaced in an error (S4).
        self._redact_values: set[str] = set()

        if _adapter is None:
            self._adapter: Any = _DefaultPrizedAdapter(
                api_key=api_key, base_url=base_url, edge_url=edge_url
            )
        else:
            self._adapter = _adapter

    # ------------------------------------------------------------------
    # ContainerProvider contract
    # ------------------------------------------------------------------

    def start_container(
        self,
        image: Optional[str] = None,
        port: Optional[int] = None,
        env_vars: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> str:
        """
        Create a Prized sandbox and start an OpenEnv server inside it.

        Args:
            image (`str`, *optional*):
                A ``space:``/``git+``/``dir:`` source or a registry image. May be
                omitted when supplied to the constructor.
            port (`int`, *optional*):
                Must be `None` or `8000`, the port OpenEnv servers listen on.
            env_vars (`dict`, *optional*):
                Environment variables for the server, overriding the
                constructor's.
            **kwargs:
                ``cmd`` (`str`) overrides the server command. Unknown options
                raise `ValueError` so typos cannot silently change behavior.

        Returns:
            `str`: The base URL: a loopback proxy URL by default, or the public
            ``https://`` URL with ``public=True``.

        Raises:
            RuntimeError: If a sandbox is already active, or the server could
                not be installed or started.
            ValueError: If no image is available, the port is unsupported, or an
                unknown option is passed.
        """
        if self._sandbox is not None or self._proxy is not None:
            raise RuntimeError(
                "PrizedProvider already has an active sandbox, or one still "
                "awaits cleanup. Call stop_container() (or close()) before "
                "starting another; a second start would orphan it."
            )
        if port is not None and port != _DEFAULT_PORT:
            raise ValueError(
                f"PrizedProvider only supports port {_DEFAULT_PORT} (got {port}), "
                "the port OpenEnv servers listen on."
            )

        effective_image = image if image is not None else self._image
        if effective_image is None:
            raise ValueError(
                "PrizedProvider requires an image or source. Pass it to the "
                "constructor or start_container()."
            )
        effective_env_vars = self._env_vars if env_vars is None else dict(env_vars)

        cmd = kwargs.pop("cmd", None) or self._cmd
        # AutoEnv.from_env() forwards this unconditionally via
        # from_docker_image; it never reaches a provider's wait, so drop it.
        kwargs.pop("wait_timeout", None)
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise ValueError(f"Unsupported PrizedProvider start options: {unknown}")

        # Everything that can be checked locally is checked before a sandbox
        # exists, so a bad argument never costs one.
        source = _parse_source(effective_image)
        upload = _tar_directory(source["location"]) if source["kind"] == "dir" else None
        docker = source["kind"] == "oci"
        if docker:
            _check_docker_env(effective_env_vars)
        template = self._template or (_DOCKER_TEMPLATE if docker else _BASE_TEMPLATE)

        self._redact_values = {
            value for value in (effective_env_vars or {}).values() if value
        }

        # A create failure created nothing, so just drop the recorded secrets.
        try:
            self._sandbox = self._adapter.create_sandbox(
                template=template,
                vcpu=self._vcpu,
                memory_gb=self._memory_gb,
                disk_gb=self._disk_gb,
                labels={"openenv": "server", **self._labels},
                max_runtime_min=self._max_runtime_min,
                auto_pause_min=self._auto_pause_min,
                auto_delete_min=self._auto_delete_min,
            )
        except BaseException:
            self._redact_values = set()
            raise

        self._docker = docker
        try:
            if docker:
                self._start_image(source["location"], cmd, effective_env_vars)
            else:
                env_root = self._fetch_and_install(source, upload)
                self._start_source(env_root, cmd, effective_env_vars)
            self._base_url = self._open_endpoint()
        except BaseException:
            # Interrupted or failed: delete the sandbox. A cleanup failure must
            # not mask the original error.
            try:
                self.stop_container()
            except Exception:
                pass
            raise

        return self._base_url

    def wait_for_ready(self, base_url: str, timeout_s: float = 120.0) -> None:
        """
        Poll ``/health`` until the server answers 200.

        A `200` on `/health` proves HTTP reachability; the `/ws` WebSocket
        upgrade `EnvClient` needs goes through the same proxy or public port.

        Raises:
            TimeoutError: If the server does not become ready in time.
            RuntimeError: If the server process died (detected in the sandbox).
        """
        import requests

        deadline = time.time() + timeout_s
        health_url = f"{base_url.rstrip('/')}/health"
        # A loopback proxy URL must not be sent to an HTTP(S)_PROXY.
        proxies = (
            {"http": None, "https": None}
            if _is_loopback(urlsplit(health_url).hostname)
            else None
        )

        while time.time() < deadline:
            try:
                response = requests.get(health_url, timeout=5.0, proxies=proxies)
                if response.status_code == 200:
                    return
            except requests.RequestException:
                pass

            if self._sandbox is not None:
                try:
                    running = self._server_running()
                except Exception:
                    # A failed check is not a dead server; keep polling.
                    running = True
                if not running:
                    raise RuntimeError(self._server_died_message())

            _sleep(1.0)

        raise TimeoutError(f"Prized sandbox did not become ready within {timeout_s}s.")

    def stop_container(self) -> None:
        """Stop the loopback proxy and delete the sandbox. Safe to call twice.

        If the delete fails, the sandbox is kept so a later call can retry it
        (``EnvClient`` retries a failed stop on ``close()``).
        """
        proxy_error: Optional[RuntimeError] = None
        if self._proxy is not None:
            try:
                _stop_gracefully(self._proxy)
                self._proxy.stop()
                self._proxy = None
            except RuntimeError as exc:
                # The proxy keeps its handles so a later call can retry.
                proxy_error = exc

        try:
            if self._sandbox is not None:
                self._adapter.delete(self._sandbox)
                self._sandbox = None
        finally:
            self._base_url = None
            self._redact_values = set()

        if proxy_error is not None:
            raise proxy_error

    def close(self) -> None:
        """Stop the active sandbox (also invoked on context-manager exit)."""
        self.stop_container()

    @property
    def base_url(self) -> str:
        """URL returned by the last `start_container`."""
        if self._base_url is None:
            raise RuntimeError(
                "PrizedProvider has no active base_url. Start the provider "
                "before reading base_url."
            )
        return self._base_url

    # ------------------------------------------------------------------
    # Beyond the contract: pause, resume, fork
    # ------------------------------------------------------------------

    @property
    def sandbox_id(self) -> Optional[str]:
        """The active sandbox's id, or `None`."""
        if self._sandbox is None:
            return None
        return self._adapter.sandbox_id(self._sandbox)

    def pause(self) -> None:
        """Pause the sandbox: memory, disk and every process are kept, compute
        stops and is not billed. Open WebSockets close; reconnect after
        :meth:`resume`. With the default private port, the next request
        through the base URL also resumes it (a public port never does). A
        paused sandbox is deleted after ``auto_delete_min`` minutes."""
        self._adapter.pause(self._require_sandbox())

    def resume(self) -> None:
        """Resume a paused sandbox, the server where it left off."""
        self._adapter.resume(self._require_sandbox())

    def fork(
        self, count: int = 1, *, labels: Optional[Dict[str, str]] = None
    ) -> List["PrizedProvider"]:
        """Copy the running environment into ``count`` new sandboxes.

        Each copy starts with the parent's memory, disk and processes, so its
        server is already installed, running and warm; no install or start runs
        again. Clients connect to a copy's own ``base_url`` and start new
        sessions there. The parent pauses briefly (usually under a second)
        while it is copied, then keeps running.

        Returns one started provider per copy. Each owns its sandbox: close it
        (or pass it to ``EnvClient(base_url=p.base_url, provider=p)``, which
        closes it) when done.
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive int")
        sandbox = self._require_sandbox()
        copies = self._adapter.fork(
            sandbox,
            count=count,
            labels={"openenv": "server", **self._labels, **(labels or {})},
        )
        providers: List[PrizedProvider] = []
        try:
            for sandbox_copy in copies:
                child = self._child(sandbox_copy)
                providers.append(child)
                child._base_url = child._open_endpoint()
        except BaseException:
            for child in providers:
                try:
                    child.stop_container()
                except Exception:
                    pass
            for sandbox_copy in copies[len(providers) :]:
                try:
                    self._adapter.delete(sandbox_copy)
                except Exception:
                    pass
            raise
        return providers

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_sandbox(self) -> Any:
        if self._sandbox is None:
            raise RuntimeError("PrizedProvider has no active sandbox.")
        return self._sandbox

    def _child(self, sandbox: Any) -> "PrizedProvider":
        """A provider for a fork copy: same configuration, its own state."""
        child = copy.copy(self)
        child._env_vars = dict(self._env_vars) if self._env_vars is not None else None
        child._labels = dict(self._labels)
        child._redact_values = set(self._redact_values)
        child._sandbox = sandbox
        child._proxy = None
        child._base_url = None
        return child

    def _path(self, name: str) -> str:
        return f"{self._root}/{name}"

    def _run(self, command: str, *, timeout: Optional[float] = 60) -> "tuple[int, str]":
        return self._adapter.exec(self._sandbox, command, timeout=timeout)

    def _install_script(
        self, source: Dict[str, Optional[str]], upload: Optional[bytes]
    ) -> str:
        """Shell that fetches and installs a source and prints its env root.

        Exit codes: 3 installing uv, 4 fetching, 5 installing, 6 the
        ``#subdirectory`` is not in the source.
        """
        root = shlex.quote(self._root)
        src = shlex.quote(self._path("env"))
        log = shlex.quote(self._path("install.log"))
        if upload is not None:
            tgz = shlex.quote(self._path("src.tgz"))
            fetch = f"mkdir -p {src} && tar -xzf {tgz} -C {src}"
        else:
            url = shlex.quote(str(source["location"]))
            if source["ref"]:
                ref = shlex.quote(str(source["ref"]))
                fetch = (
                    f"git clone --quiet {url} {src} && "
                    f"git -C {src} checkout --quiet {ref}"
                )
            else:
                fetch = f"git clone --quiet --depth 1 {url} {src}"

        base = src
        if source.get("subdirectory"):
            base = shlex.quote(f"{self._path('env')}/{source['subdirectory']}")
        # The env root is the shallowest directory holding openenv.yaml.
        find_root = (
            f'BASE={base}; [ -d "$BASE" ] || exit 6; '
            'if [ -f "$BASE/openenv.yaml" ]; then ROOT="$BASE"; else '
            'ROOT=$(find "$BASE" -maxdepth 3 -name openenv.yaml '
            "-not -path '*/.venv/*' | awk -F/ '{print NF\" \"$0}' | sort -n "
            "| head -1 | cut -d' ' -f2-); "
            'ROOT="${ROOT%/openenv.yaml}"; [ -n "$ROOT" ] || ROOT="$BASE"; fi; '
        )
        install = self._install_cmd or '"$UV" sync --quiet'
        return (
            f"mkdir -p {root} && cd {root} && "
            f"( {fetch} ) > {log} 2>&1 || exit 4; "
            f"{find_root}"
            f"{{ {_uv_setup(self._root)}}} >> {log} 2>&1; "
            f'export UV; cd "$ROOT" && ( {install} ) >> {log} 2>&1 || exit 5; '
            'echo "OPENENV_ROOT=$ROOT"'
        )

    def _fetch_and_install(
        self, source: Dict[str, Optional[str]], upload: Optional[bytes]
    ) -> str:
        """Put the source in the sandbox, install it, return the env root."""
        if upload is not None:
            self._adapter.write(
                self._sandbox, self._path("src.tgz"), upload, mode=0o600
            )
        code, out = self._run(
            self._install_script(source, upload), timeout=self._install_timeout_s
        )
        if code != 0:
            step = {
                3: "installing uv",
                4: "fetching the source",
                5: "installing it",
                6: "finding #subdirectory in the source",
            }
            raise RuntimeError(
                f"Prized sandbox failed {step.get(code, 'preparing the source')} "
                f"(exit {code}). {self._log_hint(self._path('install.log'))}"
            )
        for line in out.splitlines():
            if line.startswith("OPENENV_ROOT="):
                return line[len("OPENENV_ROOT=") :].strip()
        raise RuntimeError("Prized sandbox did not report the environment root.")

    def _discover_app(self, env_root: str) -> str:
        """The ASGI app to serve: ``openenv.yaml``'s ``app``, else the app
        the environment's Dockerfile ``CMD`` hands to uvicorn, else
        ``server.app:app`` (``UVProvider``'s default).

        Older environments (e.g. the ``coding_env`` Space) have an
        ``openenv.yaml`` without ``app``, and some of them only import as a
        package (``coding_env.server.app:app``), which their Dockerfile names.
        """
        from openenv.core.containers.runtime._server_config import (
            parse_dockerfile_cmd,
            parse_openenv_app_field,
        )

        root = shlex.quote(env_root)
        code, content = self._run(f"cat {root}/openenv.yaml 2>/dev/null")
        app = parse_openenv_app_field(content) if code == 0 else None
        if app:
            return app
        for dockerfile in ("Dockerfile", "server/Dockerfile"):
            code, content = self._run(f"cat {root}/{dockerfile} 2>/dev/null")
            cmd = parse_dockerfile_cmd(content) if code == 0 and content else None
            app = _uvicorn_app(cmd) if cmd else None
            if app:
                return app
        return _DEFAULT_APP

    def _start_source(
        self, env_root: str, cmd: Optional[str], env_vars: Optional[Dict[str, str]]
    ) -> None:
        if cmd is None:
            app = self._discover_app(env_root)
            cmd = (
                f'"$UV" run --no-sync python -m uvicorn {shlex.quote(app)} '
                f"--host 0.0.0.0 --port {_DEFAULT_PORT}"
            )
        command = (
            f"{_uv_setup(self._root)}export UV; cd {shlex.quote(env_root)} && {cmd}"
        )
        self._launch(command, env_vars)

    def _launch(self, command: str, env_vars: Optional[Dict[str, str]]) -> None:
        """Start *command* in the background, recording its PID.

        ``cmd`` is trusted orchestrator configuration, not agent or environment
        input (S5); environment variables travel in the exec request, never on
        a command line.
        """
        launch = (
            f"echo $$ > {shlex.quote(self._path('server.pid'))}; "
            f"exec sh -c {shlex.quote(command)} "
            f"> {shlex.quote(self._path('server.log'))} 2>&1"
        )
        self._adapter.exec(
            self._sandbox,
            f"sh -c {shlex.quote(launch)}",
            env=dict(env_vars) if env_vars else None,
            background=True,
        )

    def _start_image(
        self, image: str, cmd: Optional[str], env_vars: Optional[Dict[str, str]]
    ) -> None:
        code, _ = self._run(
            "docker version --format '{{.Server.Version}}' >/dev/null 2>&1"
        )
        if code != 0:
            raise RuntimeError(
                "Running a registry image needs Docker in the sandbox: use the "
                f"{_DOCKER_TEMPLATE} template (the default for images), or pass a "
                "space:, git+ or dir: source to run without Docker."
            )
        # Values go through a 0600 file, removed once the container exists,
        # not docker's command line.
        env_file = self._path("server.env")
        env_lines = "".join(
            f"{key}={value}\n" for key, value in (env_vars or {}).items()
        )
        self._adapter.write(self._sandbox, env_file, env_lines.encode(), mode=0o600)

        log = shlex.quote(self._path("install.log"))
        run = (
            f"docker run -d --name {_CONTAINER} -p {_DEFAULT_PORT}:{_DEFAULT_PORT} "
            f"--env-file {shlex.quote(env_file)} {shlex.quote(image)}"
        )
        if cmd:
            run += f" sh -c {shlex.quote(cmd)}"
        code, _ = self._run(
            f"docker pull --quiet {shlex.quote(image)} > {log} 2>&1 "
            f"&& {run} >> {log} 2>&1; status=$?; "
            f"rm -f {shlex.quote(env_file)}; exit $status",
            timeout=self._install_timeout_s,
        )
        if code != 0:
            raise RuntimeError(
                f"Prized sandbox failed to pull or start the image (exit {code}). "
                f"{self._log_hint(self._path('install.log'))}"
            )

    def _open_endpoint(self) -> str:
        """Return the URL EnvClient connects to: public port or local proxy."""
        if self.public:
            return _require_secure_url(
                self._adapter.set_port_public(
                    self._sandbox, _DEFAULT_PORT, expires_in=self._public_expires_in
                )
            )
        target = _require_secure_url(
            self._adapter.port_url(self._sandbox, _DEFAULT_PORT)
        )
        from openenv.core.containers.runtime.hf_sandbox_provider import _LocalAuthProxy

        proxy = _LocalAuthProxy(
            target_url=target, headers=self._adapter.port_headers(self._sandbox)
        )
        url = proxy.start()
        self._proxy = proxy
        return url

    def _server_running(self) -> bool:
        """False only when the server is known to have exited."""
        if self._docker:
            code, out = self._run(
                f"docker inspect -f '{{{{.State.Running}}}}' {_CONTAINER} 2>/dev/null"
            )
            return code != 0 or "false" not in out
        pid_file = shlex.quote(self._path("server.pid"))
        code, out = self._run(
            f"if test -s {pid_file}; then "
            f"kill -0 $(cat {pid_file}) 2>/dev/null && echo RUNNING || echo DEAD; "
            "else echo STARTING; fi"
        )
        return "DEAD" not in out

    def _redact(self, text: str, *, max_chars: int = 2000) -> str:
        redacted = text or ""
        # Longest first, so a value that contains another is replaced whole.
        for value in sorted(self._redact_values, key=len, reverse=True):
            redacted = redacted.replace(value, "***")
        if len(redacted) > max_chars:
            redacted = "...(truncated)...\n" + redacted[-max_chars:]
        return redacted

    def _log_hint(self, path: str) -> str:
        if not self.surface_server_logs or self._sandbox is None:
            return (
                f"Output is not surfaced; read {path} in the sandbox, or construct "
                "the provider with surface_server_logs=True."
            )
        _, log = self._run(f"tail -c 4000 {shlex.quote(path)} 2>/dev/null")
        return f"Log (redacted):\n{self._redact(log)}"

    def _server_died_message(self) -> str:
        base = "Prized sandbox server process died during startup."
        if self._docker:
            if self.surface_server_logs and self._sandbox is not None:
                _, log = self._run(f"docker logs --tail 200 {_CONTAINER} 2>&1")
                return f"{base} Log (redacted):\n{self._redact(log)}"
            return (
                f"{base} Output is not surfaced; run `docker logs {_CONTAINER}` in "
                "the sandbox, or construct the provider with surface_server_logs=True."
            )
        return f"{base} {self._log_hint(self._path('server.log'))}"


__all__ = ["PrizedProvider"]
