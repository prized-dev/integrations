# SPDX-License-Identifier: Apache-2.0

"""``PrizedProvider(template="auto")``: one paused, warmed base per environment.

The first ``start_container`` for an environment builds a base (install,
start, a fork to drop the install's memory, one throwaway ``reset()``,
pause) and records its id in a local state file. Every later start forks
that base, which takes seconds instead of an install. This module holds the
pieces that don't need OpenEnv: the cache key, the state file and its locks,
and the launcher that lets a fork copy reseed its server's random generators.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import json
import logging
import os
import tarfile
import tempfile
from typing import Any, Dict, Iterator, Optional

log = logging.getLogger("openenv_prized")

#: Bumped when what a base holds changes (the build steps, the launcher), so
#: bases built by an older openenv-prized are not reused.
RECIPE = 1

_STATE_ENV = "OPENENV_PRIZED_STATE"


class StateFileError(RuntimeError):
    """The state file or its lock could not be read or written."""


_FORMAT = 1


def default_state_path() -> str:
    """Where bases are recorded: ``$OPENENV_PRIZED_STATE``, else
    ``$XDG_STATE_HOME/openenv-prized/templates.json`` (``~/.local/state``)."""
    explicit = os.environ.get(_STATE_ENV)
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    root = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(root, "openenv-prized", "templates.json")


def dir_digest(tgz: bytes) -> str:
    """A content hash of a packed ``dir:`` source: names, modes and bytes,
    not modification times, so touching a file does not force a rebuild."""
    h = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as archive:
        members = sorted(archive.getmembers(), key=lambda m: m.name)
        for member in members:
            h.update(f"{member.name}\0{member.type!r}\0{member.mode:o}\0".encode())
            if member.isfile():
                data = archive.extractfile(member)
                h.update(hashlib.sha256(data.read() if data else b"").digest())
    return h.hexdigest()


def key_fingerprint(api_key: Optional[str]) -> str:
    """12 hex digits of the API key's SHA-256: enough to keep two workspaces'
    bases apart in one state file, nothing that leads back to the key."""
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


def cache_key(parts: Dict[str, Any]) -> str:
    """The base's key: a hash of everything that shapes what the base holds."""
    blob = json.dumps({"recipe": RECIPE, **parts}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def env_digest(env_vars: Optional[Dict[str, str]]) -> str:
    """Env vars are in the base's server process, so they are part of the key;
    only a hash of them is ever written down."""
    if not env_vars:
        return ""
    blob = json.dumps(sorted(env_vars.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@contextlib.contextmanager
def _flock(path: str) -> Iterator[None]:
    """An exclusive lock between processes (and threads: each call opens its
    own file description). Without fcntl (Windows) it does nothing."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not on POSIX
        yield
        return
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StateFile:
    """The JSON file that maps a key to the paused base's sandbox id.

    It is meant to be read and deleted by people: one entry per base, with
    the source, the shape and when it was built. Deleting the file forgets
    the bases without deleting them (they are labelled ``openenv=template``;
    ``prized sandbox ls --label openenv=template`` lists them).
    """

    def __init__(self, path: Optional[str] = None):
        self.path = os.path.abspath(os.path.expanduser(path or default_state_path()))

    @contextlib.contextmanager
    def _lock(self, path: str) -> Iterator[None]:
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            lock = _flock(path)
            lock.__enter__()
        except OSError as exc:
            raise StateFileError(f"cannot lock {path}: {exc}") from exc
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def building(self, key: str) -> "contextlib.AbstractContextManager[None]":
        """Held while a base is built, so concurrent starts of the same
        environment wait for one base instead of building one each."""
        return self._lock(f"{self.path}.{key}.lock")

    def _locked(self) -> "contextlib.AbstractContextManager[None]":
        return self._lock(f"{self.path}.lock")

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            # Not "empty": a write after this would drop every other base.
            raise StateFileError(f"cannot read {self.path}: {exc}") from exc
        except ValueError as exc:
            log.warning(
                "openenv-prized: ignoring unreadable template state %s (%s)",
                self.path,
                exc,
            )
            return {}
        templates = doc.get("templates") if isinstance(doc, dict) else None
        return templates if isinstance(templates, dict) else {}

    def _write(self, templates: Dict[str, Any]) -> None:
        doc = {
            "format": _FORMAT,
            "about": "Paused Prized sandboxes that PrizedProvider(template='auto') "
            "forks new environments from. Delete an entry (or this file) to "
            "forget a base; PrizedProvider.delete_template() also deletes it.",
            "templates": templates,
        }
        try:
            fd, tmp = tempfile.mkstemp(
                prefix=".templates.", dir=os.path.dirname(self.path), text=True
            )
        except OSError as exc:
            raise StateFileError(f"cannot write {self.path}: {exc}") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, self.path)
        except BaseException as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            if isinstance(exc, OSError):
                raise StateFileError(f"cannot write {self.path}: {exc}") from exc
            raise

    def all(self) -> Dict[str, Any]:
        with self._locked():
            return self._read()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self.all().get(key)
        if isinstance(entry, dict) and isinstance(entry.get("sandbox_id"), str):
            return entry
        return None

    def put(self, key: str, entry: Dict[str, Any]) -> None:
        with self._locked():
            templates = self._read()
            templates[key] = entry
            self._write(templates)

    def forget(self, key: str, sandbox_id: str) -> bool:
        """Drop ``key`` if it still names ``sandbox_id`` (another process may
        have rebuilt it meanwhile). True if an entry was dropped."""
        with self._locked():
            templates = self._read()
            entry = templates.get(key)
            if not isinstance(entry, dict) or entry.get("sandbox_id") != sandbox_id:
                return False
            del templates[key]
            self._write(templates)
            return True


# ---------------------------------------------------------------------------
# Reseeding a fork copy's server
# ---------------------------------------------------------------------------
#: Written into the sandbox as ``<root>/prized_launch.py`` and run instead of
#: ``python -m uvicorn``. On SIGUSR2 it reseeds the process's ``random``
#: module and NumPy's global generator from ``os.urandom`` (fresh in every
#: copy: Prized gives each copy its own kernel RNG), then calls the app
#: module's ``prized_reseed(seed)`` if it defines one, and writes what it did
#: to ``<root>/reseed.json``.
LAUNCHER = '''\
"""Started by openenv-prized: runs uvicorn as `python -m uvicorn` does, and
reseeds this process's random generators on SIGUSR2. A Prized fork copy
starts with its template's memory, generator states included."""
import json
import os
import random
import signal
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE = sys.argv[1].partition(":")[0]
_generation = [0]


def _write(name, text):
    tmp = os.path.join(_HERE, name + ".tmp")
    with open(tmp, "w") as handle:
        handle.write(text)
    os.replace(tmp, os.path.join(_HERE, name))


def _reseed(signum, frame):
    _generation[0] += 1
    seed = int.from_bytes(os.urandom(8), "big")
    done = {"generation": _generation[0], "seed": seed, "random": False}
    try:
        random.seed(seed)
        done["random"] = True
        numpy = sys.modules.get("numpy")
        if numpy is not None and hasattr(numpy, "random"):
            numpy.random.seed(seed % 2**32)
            done["numpy"] = True
        hook = getattr(sys.modules.get(_MODULE), "prized_reseed", None)
        done["hook"] = "none"
        if callable(hook):
            hook(seed)
            done["hook"] = "called"
    except Exception as exc:
        done["error"] = ("%s: %s" % (type(exc).__name__, exc))[:300]
    try:  # never let the handler raise into the server
        _write("reseed.json", json.dumps(done))
    except Exception:
        pass


signal.signal(signal.SIGUSR2, _reseed)
_write("reseed.pid", str(os.getpid()))
sys.path[0] = os.getcwd()  # as `python -m` puts it
sys.argv = ["uvicorn"] + sys.argv[1:]
from uvicorn.main import main  # noqa: E402

main()
'''


def reseed_script(root: str) -> str:
    """Shell that signals the launcher in a copy and prints its reseed.json.

    Exit codes: 10 no launcher (the server was started with a custom
    ``cmd``), 11 the process is gone, 12 it did not answer within 5 s.
    """
    import shlex

    d = shlex.quote(root)
    return (
        f'pid=$(cat {d}/reseed.pid 2>/dev/null); [ -n "$pid" ] || exit 10; '
        f"before=$(cat {d}/reseed.json 2>/dev/null); "
        'kill -USR2 "$pid" 2>/dev/null || exit 11; i=0; '
        "while [ $i -lt 100 ]; do "
        f"now=$(cat {d}/reseed.json 2>/dev/null); "
        'if [ -n "$now" ] && [ "$now" != "$before" ]; then echo "$now"; exit 0; fi; '
        "sleep 0.05; i=$((i+1)); done; exit 12"
    )
