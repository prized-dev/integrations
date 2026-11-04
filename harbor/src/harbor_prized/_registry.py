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
import subprocess
from pathlib import Path

HUB = "index.docker.io"  # how crane names Docker Hub
HUB_KEY = "https://index.docker.io/v1/"  # Docker Hub's key in config.json
_HUB_HOSTS = {"docker.io", HUB, "registry-1.docker.io", "registry.hub.docker.com"}
_ENV_USER, _ENV_TOKEN = "DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"
_HELPER_TIMEOUT_SEC = 60


def image_registry(image: str) -> str:
    """The registry host of an image reference (``index.docker.io`` for Docker Hub)."""
    first, slash, _ = image.partition("/")
    if not slash or not ("." in first or ":" in first or first == "localhost"):
        return HUB  # python:3.12, alexgshaw/task:tag
    return _host(first)


def _host(key: str) -> str:
    """A config.json key (``https://index.docker.io/v1/``, ``ghcr.io``) as a host."""
    host = key.split("://", 1)[-1].split("/", 1)[0].lower()
    return HUB if host in _HUB_HOSTS else host


def config_key(registry: str) -> str:
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
            hint = " Run `docker login` first." if source == "docker" else ""
            raise ValueError(
                f"registry_auth={source}: no Docker config at {path}.{hint}"
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
        try:
            config = json.loads(self._path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"registry_auth: can't read {self._path} as a Docker config "
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
            for key in dict.fromkeys([*entries, config_key(registry)]):
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
    try:
        result = subprocess.run(
            [program, "get"],
            input=server,
            capture_output=True,
            text=True,
            timeout=_HELPER_TIMEOUT_SEC,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"registry_auth: Docker keeps the credentials for {server} in {program}, "
            "which is not on PATH."
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"registry_auth: {program} did not answer for {server} "
            f"in {_HELPER_TIMEOUT_SEC} seconds."
        ) from None
    if result.returncode != 0:
        if "credentials not found" in (result.stdout + result.stderr).lower():
            return None
        # A helper's errors name the server, never the secret.
        raise RuntimeError(
            f"registry_auth: {program} failed for {server}: "
            f"{(result.stderr or result.stdout).strip()[:200]}"
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


def docker_config(registry: str, entry: dict[str, str]) -> bytes:
    """A config.json with only ``entry``, for ``registry``."""
    return json.dumps({"auths": {config_key(registry): entry}}).encode()
