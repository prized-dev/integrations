"""Registry credentials for the image pull (``--ek registry_auth=...``).

Harbor writes ``--ek`` values into the job's ``config.json``, so the option
names where the credentials are, never the credentials themselves:

- ``env``: ``DOCKERHUB_USERNAME`` and ``DOCKERHUB_TOKEN`` (a Docker Hub
  personal access token), for images on Docker Hub.
- ``docker``: your Docker login, ``$DOCKER_CONFIG/config.json`` or else
  ``~/.docker/config.json``. Credentials Docker keeps in a credential store
  (``credsStore``, ``credHelpers``) are read with its ``docker-credential-*``
  helper on this machine.
- a path: a Docker ``config.json``, or a directory holding one.

Only the entry for the image's own registry goes to the sandbox, as a
``config.json`` crane reads, and it is removed as soon as the pull is done.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

HUB = "index.docker.io"  # how crane names Docker Hub
HUB_KEY = "https://index.docker.io/v1/"  # Docker Hub's key in config.json
_HUB_HOSTS = {"docker.io", HUB, "registry-1.docker.io", "registry.hub.docker.com"}
_ENV_USER, _ENV_TOKEN = "DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"
_HELPER_TIMEOUT_SEC = 60
# A credential store is asked once a run per registry (a keychain may prompt),
# not once a trial; short enough for tokens that expire, as ECR's do.
_CACHE_SEC = 300
_CACHE: dict[tuple[str, str], tuple[float, dict[str, str] | None]] = {}
# Tokens people paste where a source belongs; never echoed back.
_TOKEN_LIKE = re.compile(r"(dckr_pat_|dckr_oat_|ghp_|github_pat_|gho_|glpat-)")


def _registry_part(image: str) -> str | None:
    """The registry component of an image reference as written, None for Docker Hub's default."""
    first, slash, _ = image.partition("/")
    if not slash or not ("." in first or ":" in first or first == "localhost"):
        return None  # python:3.12, alexgshaw/task:tag
    return first


def image_registry(image: str) -> str:
    """The registry of an image reference, lowercased (``index.docker.io`` for Docker Hub)."""
    first = _registry_part(image)
    return HUB if first is None else (_host(first) or first.lower())


def crane_key(image: str) -> str:
    """The config.json key crane looks up for ``image``: Docker Hub's key for
    ``docker.io`` and ``index.docker.io``, otherwise the registry exactly as
    written (crane matches keys case and all)."""
    first = _registry_part(image)
    if first is None or first in ("docker.io", HUB):
        return HUB_KEY
    return first


def _host(key: str) -> str | None:
    """A config.json key (``https://index.docker.io/v1/``, ``ghcr.io``) as a
    registry; None for a key with any other path, such as the
    ``.../v1/access-token`` and ``.../v1/refresh-token`` entries ``docker
    login`` keeps beside Docker Hub's own."""
    host, _, path = key.split("://", 1)[-1].partition("/")
    if path.strip("/") not in ("", "v1", "v2"):
        return None
    host = host.lower()
    return HUB if host in _HUB_HOSTS else host


def _store_key(registry: str) -> str:
    return HUB_KEY if registry == HUB else registry


class RegistryAuth:
    """Where ``registry_auth`` points; ``for_registry`` reads the credentials."""

    def __init__(self, source: str) -> None:
        self.source = source
        self._path: Path | None = None
        if source == "env":
            missing = [n for n in (_ENV_USER, _ENV_TOKEN) if not os.environ.get(n)]
            if missing:
                raise ValueError(
                    f"registry_auth=env needs {' and '.join(missing)} in the "
                    "environment (a Docker Hub username and personal access token)."
                )
            return
        if source == "docker":
            path = Path(os.environ.get("DOCKER_CONFIG") or "~/.docker").expanduser()
        else:
            path = Path(source).expanduser()
        if path.is_dir():
            path = path / "config.json"
        if not path.is_file():
            if source == "docker":
                raise ValueError(
                    f"registry_auth=docker: no Docker config at {path}. "
                    "Run `docker login` first."
                )
            # Not echoed: a value that is no file may be a pasted token.
            hint = (
                " That looks like a token: set it as DOCKERHUB_TOKEN, with "
                "DOCKERHUB_USERNAME, and pass registry_auth=env."
                if _TOKEN_LIKE.search(source)
                else ""
            )
            raise ValueError(
                "registry_auth: no Docker config at the path given; it takes env, "
                f"docker, or the path of a Docker config.json.{hint}"
            )
        self._path = path

    def describe(self) -> str:
        return f"{_ENV_USER}/{_ENV_TOKEN}" if self._path is None else str(self._path)

    def for_registry(self, registry: str) -> dict[str, str] | None:
        """The ``auths`` entry crane needs for ``registry``, or None if there is none."""
        if self._path is None:
            if registry != HUB:
                return None
            return _basic(os.environ[_ENV_USER], os.environ[_ENV_TOKEN])
        key = (str(self._path), registry)
        cached = _CACHE.get(key)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_SEC:
            return cached[1]
        entry = _read(self._path, registry)
        _CACHE[key] = (time.monotonic(), entry)
        return entry


def _read(path: Path, registry: str) -> dict[str, str] | None:
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"registry_auth: can't read {path} as a Docker config "
            f"({type(exc).__name__})"
        ) from None
    if not isinstance(config, dict):
        return None
    # As Docker does: a registry's own helper, else the credential store,
    # else the file's own entry.
    for key, helper in (config.get("credHelpers") or {}).items():
        if _host(key) == registry and helper:
            return _from_helper(str(helper), key)
    entries = {
        key: entry
        for key, entry in (config.get("auths") or {}).items()
        if _host(key) == registry and isinstance(entry, dict)
    }
    store = config.get("credsStore")
    if store:
        for key in dict.fromkeys([*entries, _store_key(registry)]):
            found = _from_helper(str(store), key)
            if found is not None:
                return found
    for entry in entries.values():
        found = _from_entry(entry)
        if found is not None:
            return found
    return None


def _basic(username: str, password: str) -> dict[str, str]:
    return {"auth": base64.b64encode(f"{username}:{password}".encode()).decode()}


def _from_entry(entry: dict) -> dict[str, str] | None:
    if entry.get("identitytoken"):
        return {"identitytoken": str(entry["identitytoken"])}
    if entry.get("registrytoken"):
        return {"registrytoken": str(entry["registrytoken"])}
    if entry.get("auth"):
        return {"auth": str(entry["auth"])}
    if entry.get("username") and entry.get("password"):
        return _basic(str(entry["username"]), str(entry["password"]))
    return None


def _from_helper(helper: str, server: str) -> dict[str, str] | None:
    """Asks ``docker-credential-<helper>`` for ``server``'s credentials, as Docker does."""
    program = f"docker-credential-{helper}"
    # which() finds a helper as Docker does, by PATHEXT on Windows (.exe, .cmd).
    resolved = shutil.which(program)
    if resolved is None:
        raise RuntimeError(
            f"registry_auth: Docker keeps the credentials for {server} in {program}, "
            "which is not on PATH."
        )
    try:
        result = subprocess.run(
            [resolved, "get"],
            input=server,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_HELPER_TIMEOUT_SEC,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"registry_auth: could not run {program} ({type(exc).__name__})."
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"registry_auth: {program} did not answer for {server} "
            f"in {_HELPER_TIMEOUT_SEC} seconds."
        ) from None
    if result.returncode != 0:
        if "credentials not found" in (result.stdout + result.stderr).lower():
            return None
        # Helpers print their errors, not secrets; still, output that carries
        # a Secret is never shown.
        message = result.stderr.strip() or result.stdout.strip()
        if '"Secret"' in message:
            message = f"exit {result.returncode}"
        raise RuntimeError(
            f"registry_auth: {program} failed for {server}: {message[:200]}"
        )
    try:
        answer = json.loads(result.stdout)
        username, secret = answer.get("Username") or "", answer.get("Secret") or ""
    except (ValueError, AttributeError):
        raise RuntimeError(
            f"registry_auth: {program} gave no credentials for {server}."
        ) from None
    if not secret:
        return None
    if username == "<token>":  # how Docker stores an identity token
        return {"identitytoken": secret}
    return _basic(username, secret)


def docker_config(image: str, entry: dict[str, str]) -> bytes:
    """A config.json with only ``entry``, under the key crane looks up for ``image``."""
    return json.dumps({"auths": {crane_key(image): entry}}).encode()
