"""Starting and watching Cognition's ``devin-remote`` inside a session's sandbox.

The spawn contract (https://docs.devin.ai/cloud/outposts/reference#spawn-contract): run
``devin-remote serve`` from the session's working directory with only the three
``DEVIN_OUTPOST_*`` variables, a per-session ``DEVIN_REMOTE_STATE_DIR`` and the basic system
variables. The binary is pinned per session (``spec.remote_binary_sha``) and checked against its
published sha256, which this process fetches, so the sandbox never decides what is trusted.

Layout in the sandbox (``$HOME`` is ``/home/prized``):

- ``$HOME/work``: the working directory; Devin's repositories go in ``$HOME/work/repos``.
- ``$HOME/.devin/worker/cache``: downloaded binaries, the layout ``devin worker`` uses.
- ``$HOME/.devin/worker/sessions/<session>``: the remote's state directory.
- ``$HOME/.prized-outposts/<session>``: ``run.sh``, ``pid``, ``remote.pid``, ``exit``, ``remote.log``.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

STATIC_BASE = "https://static.devin.ai/devin-rs/remote"
HOME = "/home/prized"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe(session_id: str) -> str:
    out = _SAFE.sub("_", session_id)[:120]
    if not out or out in (".", ".."):
        raise ValueError(f"unusable session id {session_id!r}")
    return out


def shell_path(path: str) -> str:
    """A double-quoted shell word for ``path``, with ``/home/prized`` written as ``$HOME``."""
    prefix = ""
    if path == HOME or path.startswith(HOME + "/"):
        prefix, path = "$HOME", path[len(HOME) :]
    escaped = re.sub(r'(["\\$`])', r"\\\1", path)
    return f'"{prefix}{escaped}"'


def state_dir(session_id: str) -> str:
    return f"$HOME/.devin/worker/sessions/{safe(session_id)}"


def run_dir(session_id: str) -> str:
    return f"$HOME/.prized-outposts/{safe(session_id)}"


@dataclass(frozen=True)
class Binary:
    sha: str
    filename: str
    url: str | None
    digest: str
    local_path: str | None = None


class RemoteSource:
    """Resolves which ``devin-remote`` a session runs and its sha256, with a small cache."""

    def __init__(
        self,
        *,
        static_base: str = STATIC_BASE,
        local_file: str | None = None,
        platform: str = "linux_x64",
        http: httpx.Client | None = None,
    ) -> None:
        self.static_base = static_base.rstrip("/")
        self.local_file = local_file
        self.platform = platform
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._digests: dict[str, str] = {}

    def resolve(self, pinned_sha: str | None) -> Binary:
        if self.local_file:
            data = Path(self.local_file).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            sha = f"local-{digest[:12]}"
            return Binary(sha, f"devin-remote_{sha}_{self.platform}", None, digest, self.local_file)
        sha = pinned_sha or self._get(f"{self.static_base}/latest_{self.platform}").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            raise ValueError(f"unexpected devin-remote version {sha[:80]!r}")
        filename = f"devin-remote_{sha}_{self.platform}"
        if filename not in self._digests:
            text = self._get(f"{self.static_base}/{filename}.sha256").strip()
            digest = text.split()[0].lower() if text else ""
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"unexpected checksum file for {filename}")
            self._digests[filename] = digest
        return Binary(sha, filename, f"{self.static_base}/{filename}", self._digests[filename])

    def _get(self, url: str) -> str:
        resp = self.http.get(url)
        resp.raise_for_status()
        return resp.text


# The remote's environment, per the spawn contract. Everything else is unset before it starts.
KEEP = (
    "PATH HOME USER LOGNAME TMPDIR LANG TZ DISPLAY WAYLAND_DISPLAY XAUTHORITY "
    "DEVIN_OUTPOST_GATEWAY_URL DEVIN_OUTPOST_CONNECT_TOKEN DEVIN_OUTPOST_SESSION_ID "
    "DEVIN_REMOTE_STATE_DIR DEVIN_CHROME_PATH DEVIN_OUTPOST_DESKTOP"
)

RUN_SH = r"""#!/bin/bash
# Started in the background by prized-devin-outposts; serves one Devin session.
run=$PO_RUN; bin=$PO_BIN; wd=$PO_WORKDIR
echo $$ > "$run/pid.tmp" && mv -f "$run/pid.tmp" "$run/pid"
export USER=${USER:-$(id -un)} LOGNAME=${LOGNAME:-$(id -un)}
export TMPDIR=${TMPDIR:-/tmp} LANG=${LANG:-C.UTF-8} TZ=${TZ:-UTC}
keep=" __KEEP__ "
for v in $(compgen -e); do
  case "$keep" in *" $v "*) ;; *) unset "$v" 2>/dev/null || true ;; esac
done
cd "$wd" || { echo 97 > "$run/exit"; exit 0; }
trap 'kill -TERM "$rpid" 2>/dev/null' TERM INT HUP
"$bin" serve >> "$run/remote.log" 2>&1 < /dev/null &
rpid=$!
echo "$rpid" > "$run/remote.pid"
while :; do
  wait "$rpid"; code=$?
  kill -0 "$rpid" 2>/dev/null || break   # a trapped signal interrupts wait; wait again
done
echo "$code" > "$run/exit.tmp" && mv -f "$run/exit.tmp" "$run/exit"
""".replace("__KEEP__", KEEP)


def prepare_script(session_id: str, binary: Binary, workdir: str) -> str:
    """Make the directories, fetch and check the binary, write run.sh. Prints one status line:
    ``alive <pid>`` (a remote is already serving), or ``ready <path>``."""
    run = run_dir(session_id)
    fetch = (
        f'curl -fsSL --retry 3 --retry-delay 2 {shlex.quote(binary.url)} -o "$bin.partial" '
        f'|| {{ echo "download failed: {binary.filename}" >&2; exit 5; }}'
        if binary.url
        else f'{{ echo "missing {binary.filename}" >&2; exit 4; }}'
    )
    return f"""set -eu
run="{run}"; cache="$HOME/.devin/worker/cache"; bin="$cache/{binary.filename}"
mkdir -p "$run" "$cache" "{state_dir(session_id)}" {shell_path(workdir)}
ok() {{ [ -f "$1" ] && printf '%s  %s\\n' {binary.digest} "$1" | sha256sum -c --status; }}
if ! ok "$bin"; then
  {fetch}
  if [ -f "$bin.partial" ]; then
    ok "$bin.partial" || {{ rm -f "$bin.partial"; echo "checksum mismatch: {binary.filename}" >&2; exit 6; }}
    mv -f "$bin.partial" "$bin"
  fi
  ok "$bin" || {{ echo "checksum mismatch: {binary.filename}" >&2; exit 6; }}
fi
chmod 0755 "$bin"
if [ ! -f "$run/exit" ] && [ -s "$run/pid" ] && kill -0 "$(cat "$run/pid")" 2>/dev/null; then
  echo "alive $(cat "$run/pid")"; exit 0
fi
rm -f "$run/pid" "$run/remote.pid" "$run/exit"
cat > "$run/run.sh" <<'PRIZED_RUN_SH'
{RUN_SH}PRIZED_RUN_SH
chmod 0700 "$run/run.sh"
echo "ready $bin"
"""


def launch_command(session_id: str) -> str:
    return f'exec bash "{run_dir(session_id)}/run.sh"'


def launch_env(
    session_id: str,
    *,
    gateway_url: str,
    connect_token: str,
    binary_path: str,
    workdir: str,
    chrome_path: str | None = None,
    desktop: bool = False,
) -> dict[str, str]:
    env = {
        "PO_RUN": run_dir(session_id).replace("$HOME", HOME),
        "PO_BIN": binary_path,
        "PO_WORKDIR": workdir,
        "DEVIN_OUTPOST_GATEWAY_URL": gateway_url,
        "DEVIN_OUTPOST_CONNECT_TOKEN": connect_token,
        "DEVIN_OUTPOST_SESSION_ID": session_id,
        "DEVIN_REMOTE_STATE_DIR": state_dir(session_id).replace("$HOME", HOME),
    }
    if chrome_path:
        env["DEVIN_CHROME_PATH"] = chrome_path
    if desktop:
        env["DEVIN_OUTPOST_DESKTOP"] = "true"
    return env


def status_script(session_id: str) -> str:
    run = run_dir(session_id)
    return f"""run="{run}"
if [ -f "$run/exit" ]; then echo "exited $(cat "$run/exit")"
elif [ -s "$run/pid" ]; then
  p=$(cat "$run/pid")
  if kill -0 "$p" 2>/dev/null && [ "$(cut -d' ' -f3 "/proc/$p/stat" 2>/dev/null)" != Z ]; then echo "alive $p"
  elif [ -f "$run/exit" ]; then echo "exited $(cat "$run/exit")"  # it ended between the two checks
  else echo gone; fi
else echo none; fi
"""


def stop_script(session_id: str, only_pid: int | None = None) -> str:
    """Stop the session's worker; with ``only_pid``, only if it is still the run.sh with that pid."""
    run = run_dir(session_id)
    guard = (
        f'[ "$(cat "$run/pid" 2>/dev/null)" = "{int(only_pid)}" ] || {{ echo "not ours"; exit 0; }}\n'
        if only_pid is not None
        else ""
    )
    return f"""run="{run}"
{guard}for f in remote.pid pid; do
  [ -s "$run/$f" ] || continue
  p=$(cat "$run/$f"); kill -TERM "$p" 2>/dev/null || continue
  for i in $(seq 50); do kill -0 "$p" 2>/dev/null || break; sleep 0.1; done
  kill -KILL "$p" 2>/dev/null || true
done
echo stopped
"""


def parse_status(stdout: str) -> tuple[str, int | None]:
    """``("alive"|"exited"|"gone"|"none"|"unknown", number)`` from status_script's line."""
    words = (stdout.strip().splitlines() or [""])[-1].split()
    if not words:
        return "unknown", None
    word = words[0]
    num = int(words[1]) if len(words) > 1 and words[1].lstrip("-").isdigit() else None
    if word in ("alive", "exited", "gone", "none", "ready"):
        return word, num
    return "unknown", None


def ensure_binary_uploaded(sb: Any, binary: Binary) -> None:
    """With a local binary (``PRIZED_OUTPOSTS_REMOTE_FILE``), copy it in before prepare checks it."""
    if binary.local_path:
        sb.files.upload(binary.local_path, f"{HOME}/.devin/worker/cache/{binary.filename}", mode=0o755)
