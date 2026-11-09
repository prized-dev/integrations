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
import json
import math
import os
import re
import shlex
import tarfile
import threading
import time
import warnings
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlsplit, urlunsplit

from openenv.core.containers.runtime.providers import ContainerProvider

from . import _template
from ._template import log

_DEFAULT_PORT = 8000
_BASE_TEMPLATE = "prized-base-v1"
_DOCKER_TEMPLATE = "prized-docker-v1"
# A sandbox left behind by a crashed trainer is deleted after this many minutes
# of running, or of being paused. Longer runs pass their own values (paid
# workspaces allow up to 7 days of running).
_DEFAULT_MAX_RUNTIME_MIN = 60
_DEFAULT_AUTO_DELETE_MIN = 60
_DEFAULT_INSTALL_TIMEOUT_S = 600.0
# fork_base="ready": how long start_container() waits for /health inside the
# sandbox, unless the caller passes wait_timeout (AutoEnv.from_env does).
_DEFAULT_READY_TIMEOUT_S = 120.0
_DEFAULT_REBASE_TIMEOUT_S = 60.0
_FORK_BASES = ("install", "ready")
_AUTO = "auto"
_DEFAULT_API = "https://api.prized.dev"
_DEFAULT_TEMPLATE_AUTO_DELETE_MIN = 7 * 24 * 60
_WARM_TIMEOUT_S = 60.0
_RECHECK_TIMEOUT_S = 15.0
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


def _fork_base_mode(value: Union[None, bool, str]) -> Optional[str]:
    """``fork_base`` as None, ``"install"`` or ``"ready"`` (what True means)."""
    if value is None or value is False:
        return None
    if value is True:
        return "ready"
    if isinstance(value, str) and value in _FORK_BASES:
        return value
    raise ValueError(
        f"fork_base must be None, True, 'install' or 'ready' (got {value!r})"
    )


class _BaseGone(RuntimeError):
    """A template's base no longer exists."""


# A base in one of these states cannot be forked again.
_GONE_STATES = ("deleted", "deleting", "failed", "error")
_custom_cmd_warned: set = set()
_SERVER_GONE = "its process is gone"


def _gone(exc: BaseException) -> bool:
    """True when a failure says the sandbox is not there any more."""
    return (
        isinstance(exc, _BaseGone)
        or getattr(exc, "status", None) == 404
        # A base whose memory Prized discarded (or that was resized) cannot
        # be forked again: as good as gone.
        or getattr(exc, "code", None)
        in ("not_found", "sandbox_deleted", "parent_not_forkable")
    )


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

    def connect(self, sandbox_id: str) -> Any:
        """A handle to an existing sandbox (a template's paused base)."""
        return self._sandbox_cls.connect(sandbox_id, **self._connection)

    def state(self, sandbox: Any) -> Optional[str]:
        return getattr(sandbox, "state", None)

    def release(self, sandbox: Any) -> None:
        sandbox.close()

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
    that start with the server already running (see :meth:`fork`). For an
    environment you will fork many times, ``fork_base=True`` starts it in a
    fork of the sandbox that installed it, which makes every later fork
    faster (see :meth:`rebase`).

    ``template="auto"`` makes starting many environments fast: the first
    start builds a paused, warmed base of the environment and every later
    start (in any process sharing the state file) forks it, reseeding the
    copy's random generators (``reseed_after_fork``). See
    :meth:`delete_template`.

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
        fork_base: Union[None, bool, str] = None,
        reseed_after_fork: bool = True,
        template_state: Optional[str] = None,
        template_auto_delete_min: int = _DEFAULT_TEMPLATE_AUTO_DELETE_MIN,
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
                ``prized-docker-v1`` for a registry image. ``"auto"`` makes
                one of the environment itself: the first ``start_container``
                installs and starts it, warms it with one throwaway
                ``reset()``, pauses it as a base and records the base's id
                in a local state file (``template_state``); every later
                start, in this process or another, forks that base, which
                takes seconds instead of an install. Bases are kept per
                source and revision, shape, ``cmd``, ``install_cmd``, env
                vars and API key; a paused base bills storage only (about
                $0.65 a month at 2 vCPU/4 GB). If a fork fails the
                environment starts fresh, with a warning in the
                ``openenv_prized`` log. :meth:`delete_template` deletes the
                base. Sources only (``space:``, ``git+``, ``dir:``).
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
            fork_base (`bool` or `str`, *optional*):
                For environments you will fork (:meth:`fork`, ``fork_group``):
                run the server in a fork of the sandbox that installed it,
                and delete that sandbox. A fork then copies only the memory
                the environment has touched since, not the whole install,
                so it pauses the parent for less time and its copies start
                faster. ``True`` or ``"ready"``: fork once the server
                answers ``/health`` (checked inside the sandbox, up to
                ``wait_timeout`` seconds, default 120). ``"install"``: fork
                after the install, then start the server in the copy.
                Costs one extra fork and a slower first episode, about
                12-14 s once per environment. Default `None`: no base. If
                the base fork fails, the environment runs in the original
                sandbox and a ``RuntimeWarning`` says so.
            reseed_after_fork (`bool`, *optional*, defaults to `True`):
                With ``template="auto"``: after forking the base, reseed the
                server's random generators, which would otherwise draw the
                same numbers in every copy. The provider starts the server
                through a small launcher that, when signalled in a copy,
                reseeds Python's ``random`` module and NumPy's global
                generator from ``os.urandom`` and calls ``prized_reseed(seed)``
                in the app's module if it defines one (put generators your
                environment holds itself there). The seed is in
                :attr:`last_template`. With your own ``cmd`` there is no
                launcher: a ``RuntimeWarning`` says so, and reseeding is up to
                your server.
            template_state (`str`, *optional*):
                The state file ``template="auto"`` records its bases in.
                Defaults to ``$OPENENV_PRIZED_STATE``, else
                ``~/.local/state/openenv-prized/templates.json``. It is plain
                JSON: read it, or delete it to forget every base (the bases
                themselves are labelled ``openenv=template``).
            template_auto_delete_min (`int`, *optional*, defaults to `10080`):
                A ``template="auto"`` base is deleted after this many minutes
                paused (7 days), so a forgotten one stops billing; ``0`` keeps
                it until :meth:`delete_template`. Fork copies inherit it as
                their own ``auto_delete_min``.
            base_url, edge_url (`str`, *optional*):
                Prized API and edge origins, for a local proxy or another
                deployment. Defaults to Prized's.
            _adapter (`Any`, *optional*):
                Injection seam for tests; a duck-typed fake replaces the SDK.
        """
        if public_expires_in is not None and not public:
            raise ValueError("public_expires_in applies only with public=True")
        self._fork_base = _fork_base_mode(fork_base)
        self._template_auto = template == _AUTO
        if self._template_auto and self._fork_base is not None:
            raise ValueError(
                "template='auto' already starts every environment from a "
                "rebased base; drop fork_base"
            )
        if template_auto_delete_min is None or template_auto_delete_min < 0:
            raise ValueError("template_auto_delete_min must be 0 or more minutes")
        self._reseed_after_fork = bool(reseed_after_fork)
        self._template_state = _template.StateFile(template_state)
        self._template_auto_delete_min = template_auto_delete_min
        # Which API and key a base belongs to, for its cache key.
        self._api_origin = (base_url or _DEFAULT_API).rstrip("/")
        self._key_fp = _template.key_fingerprint(
            api_key if api_key is not None else os.environ.get("PRIZED_API_KEY")
        )

        self._image = image
        self._env_vars = dict(env_vars) if env_vars is not None else None
        self._template = None if self._template_auto else template
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
        # fork() and rebase() take turns, so a fork never copies a sandbox
        # that a rebase is about to delete.
        self._fork_lock = threading.Lock()
        # Originals a rebase could not delete; stop_container() retries them.
        self._orphans: List[Any] = []
        #: Seconds per phase of the last ``start_container``: ``create_s``,
        #: ``install_s`` (a source), ``ready_s`` (``fork_base="ready"``),
        #: ``rebase_s`` (with ``fork_base``), ``endpoint_s``, ``total_s``.
        self.timings: Dict[str, float] = {}
        #: What the last :meth:`rebase` did: the sandbox ids, the sockets it
        #: moved and its phases in seconds.
        self.last_rebase: Optional[Dict[str, Any]] = None
        #: With ``template="auto"``, what the last ``start_container`` did:
        #: ``status`` (``"built"``, ``"forked"`` or ``"fresh"`` after a failed
        #: fork), ``base`` (the base's sandbox id), ``key``, ``state`` (the
        #: state file), ``reseed`` (what the copy's launcher reported, with
        #: its ``seed``) and ``error`` for a fallback.
        self.last_template: Optional[Dict[str, Any]] = None

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
                ``cmd`` (`str`) overrides the server command. ``wait_timeout``
                (seconds) bounds ``fork_base="ready"``'s wait for the server.
                Unknown options raise `ValueError` so typos cannot silently
                change behavior.

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
        # from_docker_image; it never reaches a provider's wait. It bounds
        # fork_base="ready"'s wait for the server, which happens in here.
        wait_timeout = kwargs.pop("wait_timeout", None)
        ready_timeout_s = (
            float(wait_timeout)
            if isinstance(wait_timeout, (int, float))
            and not isinstance(wait_timeout, bool)
            and 0 < wait_timeout < math.inf
            else _DEFAULT_READY_TIMEOUT_S
        )
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise ValueError(f"Unsupported PrizedProvider start options: {unknown}")

        # Everything that can be checked locally is checked before a sandbox
        # exists, so a bad argument never costs one.
        source = _parse_source(effective_image)
        upload = _tar_directory(source["location"]) if source["kind"] == "dir" else None
        docker = source["kind"] == "oci"
        if docker:
            if self._template_auto:
                raise ValueError(
                    "template='auto' runs space:, git+ and dir: sources, not "
                    "registry images"
                )
            _check_docker_env(effective_env_vars)
        template = self._template or (_DOCKER_TEMPLATE if docker else _BASE_TEMPLATE)

        self._redact_values = {
            value for value in (effective_env_vars or {}).values() if value
        }
        self.timings = {}
        self.last_rebase = None
        self.last_template = None
        if self._template_auto:
            return self._start_from_template(
                source,
                upload,
                effective_image,
                template,
                cmd,
                effective_env_vars,
                ready_timeout_s,
            )
        return self._start_fresh(
            source, upload, template, cmd, effective_env_vars, ready_timeout_s
        )

    def _start_fresh(
        self,
        source: Dict[str, Optional[str]],
        upload: Optional[bytes],
        template: str,
        cmd: Optional[str],
        effective_env_vars: Optional[Dict[str, str]],
        ready_timeout_s: float,
    ) -> str:
        """Create a sandbox, install and start the environment in it."""
        docker = source["kind"] == "oci"
        started = time.monotonic()
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
        self._lap("create_s", started)
        try:
            t = time.monotonic()
            if docker:
                self._start_image(source["location"], cmd, effective_env_vars)
            else:
                env_root = self._fetch_and_install(source, upload)
                self._lap("install_s", t)
                if self._fork_base == "install":
                    self._rebase_base()
                self._start_source(env_root, cmd, effective_env_vars)
            if self._fork_base == "ready":
                t = time.monotonic()
                self._wait_ready_inside(ready_timeout_s)
                self._lap("ready_s", t)
                self._rebase_base()
            t = time.monotonic()
            self._base_url = self._open_endpoint()
            self._lap("endpoint_s", t)
            self._lap("total_s", started)
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
        (``EnvClient`` retries a failed stop on ``close()``). Originals that a
        :meth:`rebase` could not delete are deleted here too.
        """
        with self._fork_lock:  # not while a rebase swaps the sandbox
            self._stop()

    def _stop(self) -> None:
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
            self._orphans = [s for s in self._orphans if not self._deleted(s)]

        if proxy_error is not None:
            raise proxy_error
        if self._orphans:
            ids = " ".join(self._adapter.sandbox_id(s) for s in self._orphans)
            raise RuntimeError(
                f"Could not delete the sandbox(es) a rebase left behind: {ids}. "
                "stop_container() again retries."
            )

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
        with self._fork_lock:
            copies = self._adapter.fork(
                self._require_sandbox(),
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

    @property
    def fork_base(self) -> Optional[str]:
        """``"ready"``, ``"install"`` or `None`: where ``start_container``
        rebases the environment (see ``fork_base=`` and :meth:`rebase`)."""
        return self._fork_base

    def rebase(self, *, timeout_s: float = _DEFAULT_REBASE_TIMEOUT_S) -> Optional[str]:
        """Move the environment into a fork of its own sandbox and delete the
        original.

        Prized forks a running sandbox by copying the memory it has touched
        since it started. A sandbox that installed its environment has
        touched all of it (on a 4 GB ``openapp_env``, the whole 4 GiB), so
        every fork of it pays for that. A copy has touched only what it has
        used since the fork, so its own forks are faster: on ``openapp_env``
        the parent paused for 0.9-1.3 s per fork instead of 4.4-5.1 s.
        ``fork_base=`` does this once in ``start_container``; call
        ``rebase()`` yourself if a long-lived environment's forks grow slow
        again.

        The environment carries on where it was: the copy has the server's
        memory and processes, so its sessions and episodes are kept. With
        the default loopback proxy the base URL does not change and open
        WebSockets stay open: each is detached from its session (a call in
        flight finishes first, up to ``timeout_s``; past that the rebase
        is called off) and joined to the same session in the copy.
        WebSocket messages sent meanwhile wait; HTTP requests don't (MCP
        tool calls go over HTTP). A WebSocket that owns its episode
        (OpenEnv's default, without :func:`openenv_prized.forkable`) cannot
        move, so ``rebase()`` refuses with ``RuntimeError`` while one is open.
        With ``public=True`` the copy gets its own public URL, returned here:
        disconnect clients first and point them at it.

        The copy keeps the original's labels and settings; its
        ``max_runtime_min`` starts again from the fork (each copy gets the
        full window, as a new sandbox would). If the fork fails, nothing
        changes; if the original cannot be deleted, it is
        retried in :meth:`stop_container` and a ``RuntimeWarning`` says so.
        Blocks for a few seconds; from async code, ``await
        asyncio.to_thread(provider.rebase)``. Don't make calls on the
        environment while it runs.

        Returns the base URL (unchanged unless ``public=True``; `None` while
        ``start_container`` has not opened it yet). :attr:`last_rebase` has
        the sandbox ids and the time each phase took.
        """
        with self._fork_lock:
            return self._rebase(timeout_s)

    def delete_template(
        self,
        image: Optional[str] = None,
        *,
        env_vars: Optional[Dict[str, str]] = None,
        cmd: Optional[str] = None,
    ) -> Optional[str]:
        """Delete the ``template="auto"`` base that ``start_container(image,
        env_vars=env_vars, cmd=cmd)`` forks from, and forget it in the state
        file. The arguments default to the constructor's, as in
        ``start_container``. Environments already forked from it are not
        touched. Returns the base's sandbox id, or `None` if there was no
        base. Every base is labelled ``openenv=template``, so
        ``Sandbox.delete_by_labels({"openenv": "template"})`` (or
        ``prized sandbox ls --label openenv=template``) finds any this
        state file has forgotten.
        """
        effective = image if image is not None else self._image
        if effective is None:
            raise ValueError("delete_template needs an image or source")
        source = _parse_source(effective)
        if source["kind"] == "oci":
            raise ValueError("template='auto' bases are only built for sources")
        upload = _tar_directory(source["location"]) if source["kind"] == "dir" else None
        key, _ = self._template_key(
            source,
            upload,
            effective,
            self._template or _BASE_TEMPLATE,
            cmd if cmd is not None else self._cmd,
            dict(env_vars) if env_vars is not None else self._env_vars,
        )
        state = self._template_state
        with state.building(key):  # not while another process builds it
            entry = state.get(key)
            if entry is None:
                return None
            sandbox_id = entry["sandbox_id"]
            handle = None
            try:
                handle = self._adapter.connect(sandbox_id)
                self._adapter.delete(handle)
            except Exception as exc:
                if not _gone(exc):
                    raise
            finally:
                self._release(handle)
            state.forget(key, sandbox_id)
        log.info("openenv-prized: deleted template base %s (%s)", sandbox_id, effective)
        return sandbox_id

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lap(self, name: str, since: float) -> None:
        self.timings[name] = round(time.monotonic() - since, 3)

    def _deleted(self, sandbox: Any) -> bool:
        """Delete *sandbox*; False if that failed."""
        try:
            self._adapter.delete(sandbox)
            return True
        except Exception:
            return False

    def _rebase(self, timeout_s: float) -> Optional[str]:
        started = time.monotonic()
        original = self._require_sandbox()
        proxy = self._proxy
        record: Dict[str, Any] = {"from": self._adapter.sandbox_id(original)}
        try:
            t = time.monotonic()
            record["sockets"] = proxy.hold(timeout_s) if proxy is not None else 0
            record["detach_s"] = round(time.monotonic() - t, 3)
            t = time.monotonic()
            # No labels: the copy keeps the original's, whatever they are.
            copies = self._adapter.fork(original, count=1, labels=None)
            record["fork_s"] = round(time.monotonic() - t, 3)
            if len(copies) != 1:
                self._orphans += [c for c in copies if not self._deleted(c)]
                raise RuntimeError(f"fork(1) returned {len(copies)} sandboxes")
            copy_ = copies[0]
            try:
                public_url = None
                if proxy is not None:
                    target = _require_secure_url(
                        self._adapter.port_url(copy_, _DEFAULT_PORT)
                    )
                    headers = self._adapter.port_headers(copy_)
                elif self.public and self._base_url is not None:
                    public_url = _require_secure_url(
                        self._adapter.set_port_public(
                            copy_, _DEFAULT_PORT, expires_in=self._public_expires_in
                        )
                    )
            except BaseException:
                if not self._deleted(copy_):
                    self._orphans.append(copy_)
                raise
        except BaseException:
            if proxy is not None:  # back to the original
                try:
                    lost, _ = proxy.release(timeout_s)
                except Exception:
                    lost = -1
                if lost:
                    warnings.warn(
                        "rebase() failed, and some open WebSockets could not rejoin "
                        "their sessions in the original sandbox; they were closed.",
                        RuntimeWarning,
                        stacklevel=3,
                    )
            raise

        # The copy is the environment from here on. Until it is deleted, the
        # original is an orphan that stop_container() deletes.
        self._sandbox = copy_
        self._orphans.append(original)
        record["to"] = self._adapter.sandbox_id(copy_)
        t = time.monotonic()
        try:
            if proxy is not None:
                proxy.retarget(target, headers)
                record["sockets_lost"], record["sockets_later"] = proxy.release(
                    timeout_s
                )
            elif public_url is not None:
                self._base_url = public_url
        except Exception as exc:
            record["sockets_lost"] = record["sockets"]
            warnings.warn(
                f"rebase() moved to {record['to']} but could not rejoin open "
                f"WebSockets ({type(exc).__name__}: {exc}); they were closed.",
                RuntimeWarning,
                stacklevel=3,
            )
        finally:
            record["attach_s"] = round(time.monotonic() - t, 3)
            t = time.monotonic()
            if self._deleted(original) or self._deleted(original):
                self._orphans.remove(original)
            else:
                warnings.warn(
                    f"rebase() could not delete the original sandbox "
                    f"{record['from']}; stop_container() retries it.",
                    RuntimeWarning,
                    stacklevel=3,
                )
            record["delete_s"] = round(time.monotonic() - t, 3)
            record["total_s"] = round(time.monotonic() - started, 3)
            self.last_rebase = record
        if record.get("sockets_lost"):
            warnings.warn(
                f"rebase() could not rejoin {record['sockets_lost']} open "
                "WebSocket(s) to their sessions in the copy; they were closed.",
                RuntimeWarning,
                stacklevel=3,
            )
        return self._base_url

    def _rebase_base(self, what: Optional[str] = None) -> None:
        """start_container's rebase: if it fails, carry on in the original."""
        t = time.monotonic()
        try:
            self.rebase()
        except Exception as exc:
            warnings.warn(
                f"{what or f'fork_base={self._fork_base!r}'}: the base fork failed "
                f"({type(exc).__name__}: {exc}); the environment runs in the "
                "sandbox that installed it, whose forks are slower.",
                RuntimeWarning,
                stacklevel=4,
            )
        self._lap("rebase_s", t)

    # ------------------------------------------------------------------
    # template="auto"
    # ------------------------------------------------------------------

    def _template_key(
        self,
        source: Dict[str, Optional[str]],
        upload: Optional[bytes],
        image: str,
        template: str,
        cmd: Optional[str],
        env_vars: Optional[Dict[str, str]],
    ) -> "tuple[str, Dict[str, Any]]":
        """The base's cache key, and the readable part of its state entry."""
        if source["kind"] == "dir":
            assert upload is not None
            version = "sha256:" + _template.dir_digest(upload)
            where: Dict[str, Any] = {}  # the content, wherever it lives
        else:
            version = source["ref"] or "unpinned"
            where = {
                "location": source["location"],
                "subdirectory": source["subdirectory"],
            }
        about = {
            "image": image.strip(),
            "version": version,
            "sandbox_template": template,
            "shape": {
                "vcpu": self._vcpu,
                "memory_gb": self._memory_gb,
                "disk_gb": self._disk_gb,
            },
        }
        parts = {
            "kind": source["kind"],
            **where,
            "version": version,
            "sandbox_template": template,
            "shape": about["shape"],
            "cmd": cmd,
            "install_cmd": self._install_cmd,
            "env": _template.env_digest(env_vars),
            # Copies inherit these from the base.
            "max_runtime_min": self._max_runtime_min,
            "auto_pause_min": self._auto_pause_min,
            "auto_delete_min": self._template_auto_delete_min,
            "api": self._api_origin,
            "key": self._key_fp,
        }
        return _template.cache_key(parts), about

    def _start_from_template(
        self,
        source: Dict[str, Optional[str]],
        upload: Optional[bytes],
        image: str,
        template: str,
        cmd: Optional[str],
        env_vars: Optional[Dict[str, str]],
        ready_timeout_s: float,
    ) -> str:
        started = time.monotonic()
        key, about = self._template_key(source, upload, image, template, cmd, env_vars)
        state = self._template_state
        record: Dict[str, Any] = {"key": key, "state": state.path, "image": image}
        self.last_template = record

        def fresh(why: str, exc: BaseException) -> str:
            log.warning(
                "openenv-prized: %s (%s: %s); starting a fresh environment instead.",
                why,
                type(exc).__name__,
                exc,
            )
            record.update(status="fresh", error=f"{type(exc).__name__}: {exc}"[:300])
            self.timings = {}
            return self._start_fresh(
                source, upload, template, cmd, env_vars, ready_timeout_s
            )

        base = None
        try:
            entry = state.get(key)
            if entry is None:
                with state.building(key):
                    # Another process may have built it while this one waited.
                    entry = state.get(key)
                    if entry is None:
                        base, entry = self._build_template(
                            key,
                            about,
                            source,
                            upload,
                            template,
                            cmd,
                            env_vars,
                            ready_timeout_s,
                        )
                        record["status"] = "built"
        except _template.StateFileError as exc:
            return fresh(f"the template state {state.path} is unusable", exc)
        record["base"] = entry["sandbox_id"]
        record.setdefault("status", "forked")

        t = time.monotonic()
        base_state = None
        try:
            if base is None:
                base = self._adapter.connect(entry["sandbox_id"])
                self._lap("connect_s", t)
                base_state = self._adapter_state(base)
                if base_state in _GONE_STATES:
                    raise _BaseGone(f"the base is {base_state}")
            t = time.monotonic()
            copies = self._adapter.fork(
                base, count=1, labels={"openenv": "server", **self._labels}
            )
            if len(copies) != 1:
                self._orphans += [c for c in copies if not self._deleted(c)]
                raise RuntimeError(f"fork(1) returned {len(copies)} sandboxes")
        except Exception as exc:
            retired = _gone(exc) and self._retire_base(key, entry, base)
            self._release(base)
            return fresh(
                f"could not fork template base {entry['sandbox_id']} for {image}"
                + (
                    "; it is gone or unusable, so the next start builds a new one"
                    if retired
                    else ""
                ),
                exc,
            )
        if base_state == "running":
            # Someone resumed the base: pause it again so it doesn't bill compute.
            try:
                self._adapter.pause(base)
            except Exception:
                pass
        self._release(base)
        self._lap("fork_s", t)

        self._docker = False
        self._sandbox = copies[0]
        try:
            if self._reseed_after_fork:
                t = time.monotonic()
                record["reseed"] = self._reseed_copy(image, cmd)
                self._lap("reseed_s", t)
                if record["reseed"].get("error") == _SERVER_GONE:
                    raise _BaseGone("the server is not running in the copy")
            else:
                record["reseed"] = "off"
            t = time.monotonic()
            self._base_url = self._open_endpoint()
            self._lap("endpoint_s", t)
            self._lap("total_s", started)
        except _BaseGone as exc:
            try:
                self.stop_container()
            except Exception:
                pass
            retired = self._retire_base(key, entry, None)
            return fresh(
                f"template base {entry['sandbox_id']} for {image} is broken"
                + ("; the next start builds a new one" if retired else ""),
                exc,
            )
        except BaseException:
            try:
                self.stop_container()
            except Exception:
                pass
            raise
        log.info(
            "openenv-prized: %s %s from template base %s in %.1f s",
            "built and forked" if record["status"] == "built" else "forked",
            self.sandbox_id,
            record["base"],
            self.timings["total_s"],
        )
        return self._base_url

    def _retire_base(self, key: str, entry: Dict[str, Any], base: Any) -> bool:
        """Delete an unusable base (best effort) and forget it; True if forgotten."""
        handle = base
        try:
            if handle is None:
                handle = self._adapter.connect(entry["sandbox_id"])
            self._adapter.delete(handle)
        except Exception:
            pass  # gone already, or the delete failed: its auto-delete still runs
        finally:
            if base is None:
                self._release(handle)
        try:
            return self._template_state.forget(key, entry["sandbox_id"])
        except _template.StateFileError:
            return False

    def _build_template(
        self,
        key: str,
        about: Dict[str, Any],
        source: Dict[str, Optional[str]],
        upload: Optional[bytes],
        template: str,
        cmd: Optional[str],
        env_vars: Optional[Dict[str, str]],
        ready_timeout_s: float,
    ) -> "tuple[Any, Dict[str, Any]]":
        """Install, start, rebase, warm and pause a base; record it."""
        log.info(
            "openenv-prized: building a template base for %s (first use; later "
            "starts fork it). State: %s",
            about["image"],
            self._template_state.path,
        )
        started = time.monotonic()
        self._sandbox = self._adapter.create_sandbox(
            template=template,
            vcpu=self._vcpu,
            memory_gb=self._memory_gb,
            disk_gb=self._disk_gb,
            # Not the caller's labels: the base outlives this run, and a
            # cleanup by this run's labels must not delete it.
            labels={"openenv": "template", "openenv-template": key[:12]},
            max_runtime_min=self._max_runtime_min,
            # Copies inherit these settings from the base.
            auto_pause_min=self._auto_pause_min,
            auto_delete_min=self._template_auto_delete_min,
        )
        self._docker = False
        self._lap("create_s", started)
        try:
            t = time.monotonic()
            env_root = self._fetch_and_install(source, upload)
            self._lap("install_s", t)
            commit = self._source_commit(source)
            self._start_source(env_root, cmd, env_vars, launcher=cmd is None)
            t = time.monotonic()
            self._wait_ready_inside(ready_timeout_s)
            self._lap("ready_s", t)
            # Drop the install's memory, so each fork copies only the server's.
            self._rebase_base("template='auto'")
            t = time.monotonic()
            warmed = self._warm(ready_timeout_s)
            # Never save a base whose server the warm-up broke.
            self._wait_ready_inside(_RECHECK_TIMEOUT_S)
            self._lap("warm_s", t)
            t = time.monotonic()
            self._adapter.pause(self._sandbox)
            self._lap("pause_s", t)
        except BaseException:
            try:
                self.stop_container()
            except Exception:
                pass
            raise
        base, self._sandbox = self._sandbox, None
        try:
            # An original the rebase could not delete: one more try, then say so.
            self._orphans = [o for o in self._orphans if not self._deleted(o)]
            if self._orphans:
                ids = " ".join(self._adapter.sandbox_id(o) for o in self._orphans)
                warnings.warn(
                    f"template='auto' could not delete the sandbox(es) the base was "
                    f"forked from: {ids}. stop_container() retries.",
                    RuntimeWarning,
                    stacklevel=4,
                )
            entry = {
                "sandbox_id": self._adapter.sandbox_id(base),
                **about,
                "commit": commit,
                "built_at": _template.now_iso(),
                "build_s": round(time.monotonic() - started, 1),
                "warmed": warmed,
                "reseed": "launcher" if cmd is None else "none (custom cmd)",
                "api": self._api_origin,
                "auto_delete_min": self._template_auto_delete_min,
            }
            self._template_state.put(key, entry)
        except BaseException:
            # Unrecorded, nothing would ever fork or delete it.
            if not self._deleted(base):
                self._orphans.append(base)
            raise
        log.info(
            "openenv-prized: template base %s for %s built in %.1f s and paused",
            entry["sandbox_id"],
            about["image"],
            entry["build_s"],
        )
        return base, entry

    def _source_commit(self, source: Dict[str, Optional[str]]) -> Optional[str]:
        """The commit a git source was built from (unpinned sources move)."""
        if source["kind"] != "git":
            return None
        try:
            code, out = self._run(
                f"git -C {shlex.quote(self._path('env'))} rev-parse HEAD 2>/dev/null"
            )
        except Exception:
            return None
        out = out.strip()
        return out if code == 0 and re.fullmatch(r"[0-9a-f]{40,64}", out) else None

    def _warm(self, timeout_s: float) -> bool:
        """One throwaway ``reset()``, so copies start with its code paths warm.
        A failure only costs the copies' first reset some speed."""
        try:
            self._warm_reset(min(timeout_s, _WARM_TIMEOUT_S))
            return True
        except Exception as exc:
            log.warning(
                "openenv-prized: the template base's warm-up reset failed (%s: %s); "
                "copies will run their first reset() colder.",
                type(exc).__name__,
                exc,
            )
            return False

    def _warm_reset(self, timeout_s: float) -> None:
        """reset() over a WebSocket, as a client's first call does, then close
        the session so the base holds no episode."""
        import requests
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        target = _require_secure_url(
            self._adapter.port_url(self._sandbox, _DEFAULT_PORT)
        )
        headers = self._adapter.port_headers(self._sandbox)
        loopback = _is_loopback(urlsplit(target).hostname)
        deadline = time.monotonic() + timeout_s
        not_found = 0
        # The edge opens a port on its first request, and may answer 429.
        while True:
            status, delay = None, 0.5
            try:
                response = requests.get(
                    f"{target}/health",
                    headers=headers,
                    timeout=10,
                    proxies={"http": None, "https": None} if loopback else None,
                )
                status = response.status_code
                if status == 200:
                    break
                not_found += status == 404
                if status in (401, 402, 403) or not_found >= 2:
                    # Asking again won't change it (and repeated 404s cost
                    # the key its standing at the edge).
                    raise RuntimeError(f"the edge refused the port: {status}")
                if status == 429:
                    try:
                        delay = max(float(response.headers.get("Retry-After", 1)), 0.5)
                    except ValueError:
                        delay = 1.0
            except requests.RequestException:
                pass
            if time.monotonic() + delay > deadline:
                raise TimeoutError(f"/health did not answer 200 (last {status})")
            _sleep(delay)

        parts = urlsplit(target)
        ws_url = urlunsplit(
            parts._replace(
                scheme="wss" if parts.scheme == "https" else "ws",
                path=parts.path.rstrip("/") + "/ws",
            )
        )
        options: Dict[str, Any] = {
            "additional_headers": headers,
            "open_timeout": timeout_s,
            "max_size": None,
        }
        if loopback:
            options["proxy"] = None
        try:
            ws = connect(ws_url, **options)
        except TypeError:  # websockets < 15 has no proxy option
            options.pop("proxy", None)
            ws = connect(ws_url, **options)
        with ws:
            ws.send(json.dumps({"type": "reset", "data": {}}))
            reply = json.loads(ws.recv(timeout=timeout_s))
            if reply.get("type") == "error":
                raise RuntimeError(
                    f"reset failed: {self._redact(str(reply.get('data')), max_chars=200)}"
                )
            ws.send(json.dumps({"type": "close"}))
            try:  # the server ends the session, then closes the socket
                while True:
                    ws.recv(timeout=10)
            except (ConnectionClosed, TimeoutError):
                pass

    def _reseed_copy(self, image: str, cmd: Optional[str]) -> Dict[str, Any]:
        """Signal the launcher in a fresh copy to reseed its server. Best
        effort: a failure warns (the copy keeps the base's generator states)."""
        if cmd is not None:
            if image not in _custom_cmd_warned:
                _custom_cmd_warned.add(image)
                warnings.warn(
                    "reseed_after_fork: with your own cmd the server is not "
                    "started by openenv-prized's launcher, so copies keep the "
                    "template's random generator states. Reseed from os.urandom "
                    "in your server, or pass reseed_after_fork=False.",
                    RuntimeWarning,
                    stacklevel=4,
                )
            return {"error": "custom cmd: no launcher"}
        try:
            code, out = self._run(_template.reseed_script(self._root), timeout=30)
        except Exception as exc:
            code, out = -1, f"{type(exc).__name__}"
        done: Dict[str, Any] = {}
        if code == 0:
            lines = out.strip().splitlines()
            try:
                parsed = json.loads(lines[-1]) if lines else None
            except ValueError:
                parsed = None
            done = parsed if isinstance(parsed, dict) else {"error": "no report"}
            if done.get("error"):
                done["error"] = self._redact(str(done["error"]), max_chars=300)
            if done.get("random") is not True or done.get("error"):
                warnings.warn(
                    "reseed_after_fork: the copy's server did not fully reseed "
                    f"({done.get('error', 'random not reseeded')}).",
                    RuntimeWarning,
                    stacklevel=4,
                )
            return done
        reason = {
            10: "it was not started by openenv-prized's launcher",
            11: _SERVER_GONE,
            12: "it did not answer the reseed signal within 5 s",
        }.get(code, f"the reseed command failed ({out[:80] or code})")
        if code != 11:  # a dead server is handled by the caller
            warnings.warn(
                f"reseed_after_fork: could not reseed the copy's server ({reason}); "
                "it keeps the template's random generator states.",
                RuntimeWarning,
                stacklevel=4,
            )
        return {"error": reason}

    def _adapter_state(self, sandbox: Any) -> Optional[str]:
        state = getattr(self._adapter, "state", None)
        return state(sandbox) if state is not None else None

    def _release(self, sandbox: Any) -> None:
        """Let go of a handle to a sandbox that stays (a base)."""
        release = getattr(self._adapter, "release", None)
        if sandbox is not None and release is not None:
            try:
                release(sandbox)
            except Exception:
                pass

    def _wait_ready_inside(self, timeout_s: float) -> None:
        """Wait, inside the sandbox, until the server answers ``/health``.

        One command polls ``127.0.0.1:8000`` every 0.2 s, so no port is
        opened at the edge for a sandbox that is about to be replaced.
        Exit codes: 7 the server died, 8 timed out, 9 neither curl nor
        python3 to ask with.
        """
        url = f"http://127.0.0.1:{_DEFAULT_PORT}/health"
        if self._docker:
            alive = (
                f"[ \"$(docker inspect -f '{{{{.State.Running}}}}' {_CONTAINER} "
                '2>/dev/null)" != false ]'
            )
        else:
            pid = shlex.quote(self._path("server.pid"))
            alive = f"{{ ! test -s {pid} || kill -0 $(cat {pid}) 2>/dev/null; }}"
        python_probe = shlex.quote(
            "import urllib.request as u; "
            f"u.build_opener(u.ProxyHandler({{}})).open({url!r}, timeout=2)"
        )
        script = (
            f"end=$(( $(date +%s) + {int(math.ceil(timeout_s))} )); "
            "if command -v curl >/dev/null 2>&1; then "
            f"probe() {{ curl -fsS -o /dev/null --noproxy '*' --max-time 2 {url}; }}; "
            "elif command -v python3 >/dev/null 2>&1; then "
            f"probe() {{ python3 -c {python_probe}; }}; "
            "else exit 9; fi; "
            "while :; do probe >/dev/null 2>&1 && exit 0; "
            f"{alive} || exit 7; "
            '[ "$(date +%s)" -lt "$end" ] || exit 8; sleep 0.2; done'
        )
        code, _ = self._run(script, timeout=timeout_s + 30)
        if code == 7:
            raise RuntimeError(self._server_died_message())
        if code == 9:
            raise RuntimeError(
                "fork_base='ready' checks the server with curl or python3 inside "
                "the sandbox, and it has neither. Use fork_base='install'."
            )
        if code != 0:
            raise TimeoutError(
                f"Prized sandbox did not become ready within {timeout_s}s."
            )

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
        child._fork_lock = threading.Lock()
        child._orphans = []
        child.timings = {}
        child.last_rebase = None
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
        self,
        env_root: str,
        cmd: Optional[str],
        env_vars: Optional[Dict[str, str]],
        *,
        launcher: bool = False,
    ) -> None:
        if cmd is None:
            app = self._discover_app(env_root)
            runner = "-m uvicorn"
            if launcher:
                # uvicorn behind a launcher a fork copy can signal to reseed.
                path = self._path("prized_launch.py")
                self._adapter.write(
                    self._sandbox, path, _template.LAUNCHER.encode(), mode=0o644
                )
                runner = shlex.quote(path)
            cmd = (
                f'"$UV" run --no-sync python {runner} {shlex.quote(app)} '
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
        log = shlex.quote(self._path("install.log"))
        if self._fork_base == "install":
            # Pull, rebase, then run in the copy (its pull below is a no-op).
            code, _ = self._run(
                f"docker pull --quiet {shlex.quote(image)} > {log} 2>&1",
                timeout=self._install_timeout_s,
            )
            if code != 0:
                raise RuntimeError(
                    f"Prized sandbox failed to pull the image (exit {code}). "
                    f"{self._log_hint(self._path('install.log'))}"
                )
            self._rebase_base()
        # Values go through a 0600 file, removed once the container exists,
        # not docker's command line.
        env_file = self._path("server.env")
        env_lines = "".join(
            f"{key}={value}\n" for key, value in (env_vars or {}).items()
        )
        self._adapter.write(self._sandbox, env_file, env_lines.encode(), mode=0o600)

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
        from ._proxy import _SwitchingProxy

        proxy = _SwitchingProxy(
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
