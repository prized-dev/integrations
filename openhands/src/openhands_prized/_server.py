"""The Agent Server's life inside the sandbox: install, start, find again.

Everything lives under ``~/.openhands-prized`` in the sandbox:

    bin/ (uv's tool links)     agent-server, uv
    version                    the installed openhands-agent-server version
    python                     the tool environment's Python (it has websockets)
    ws_relay.py                the WebSocket relay of the exec transport
    server.json                pid, port, version and session key of the running server
    tmp/                       the proxy's request and WebSocket files
    server/                    the server's working directory (its conversations live here,
                               not in your project)
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from dataclasses import dataclass

STATE = ".openhands-prized"
_VERSION = re.compile(r"[0-9A-Za-z.+!-]{1,64}")


def check_version(version: str) -> str:
    if not _VERSION.fullmatch(version):
        raise ValueError(f"not a package version: {version!r}")
    return version


def install_script(version: str, packages: list[str] | None = None) -> str:
    """Install uv and openhands-agent-server (with the SDK, tools and workspace
    packages pinned to the same version, as Agent Canvas does) unless that version
    is already there. Prints ``HOME=``, ``PY=`` and ``INSTALLED=`` lines."""
    v = check_version(version)
    pins = [f"openhands-sdk=={v}", f"openhands-tools=={v}", f"openhands-workspace=={v}", *(packages or [])]
    withs = " ".join(f"--with {shlex.quote(p)}" for p in pins)
    return f"""set -eu
S="$HOME/{STATE}"
mkdir -p "$S/bin" "$S/server" "$S/tmp"
export UV_TOOL_BIN_DIR="$S/bin" UV_TOOL_DIR="$S/tools" PATH="$S/bin:$HOME/.local/bin:$PATH"
installed=0
if [ "$(cat "$S/version" 2>/dev/null)" != {shlex.quote(v)} ] || [ ! -x "$S/bin/agent-server" ]; then
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$S/bin" UV_NO_MODIFY_PATH=1 sh >"$S/uv-install.log" 2>&1 \\
      || {{ tail -20 "$S/uv-install.log" >&2; exit 91; }}
  fi
  uv tool install --force --python '>=3.12' {shlex.quote(f"openhands-agent-server=={v}")} {withs} >"$S/install.log" 2>&1 \\
    || {{ tail -40 "$S/install.log" >&2; exit 92; }}
  printf '%s\\n' {shlex.quote(v)} > "$S/version"
  installed=1
fi
py="$S/tools/openhands-agent-server/bin/python"
[ -x "$py" ] || {{ echo "no python in the agent-server tool environment" >&2; exit 93; }}
printf '%s\\n' "$py" > "$S/python"
echo "HOME=$HOME"
echo "PY=$py"
echo "INSTALLED=$installed"
"""


def start_command(port: int, args: list[str]) -> str:
    """The background command that runs the server (session key and other variables
    come in its environment, not its arguments)."""
    extra = " ".join(shlex.quote(a) for a in args)
    return (
        f'cd "$HOME/{STATE}/server" && exec "$HOME/{STATE}/bin/agent-server" '
        f"--host 127.0.0.1 --port {int(port)} {extra}"
    ).rstrip()


def probe_script(port: int) -> str:
    """Prints server.json (base64), whether its pid is still an agent-server, and whether
    the server answers an authenticated call with the recorded key."""
    return f"""S="$HOME/{STATE}"
printf 'STATE '; base64 -w0 < "$S/server.json" 2>/dev/null; echo
pid=$(sed -n 's/.*"pid": *\\([0-9]*\\).*/\\1/p' "$S/server.json" 2>/dev/null)
key=$(sed -n 's/.*"key": *"\\([^"]*\\)".*/\\1/p' "$S/server.json" 2>/dev/null)
# The pid must still be our agent-server (a pid can be reused), and the key must work.
if [ -n "$pid" ] && grep -qa agent-server "/proc/$pid/cmdline" 2>/dev/null; then echo ALIVE=1; else echo ALIVE=0; fi
code=$(curl -sS -m 5 -o /dev/null -w '%{{http_code}}' -H "X-Session-API-Key: $key" \\
  "http://127.0.0.1:{int(port)}/api/conversations/search?limit=1" 2>/dev/null)
if [ "$code" = 200 ]; then echo HEALTH=1; else echo HEALTH=0; fi
"""


def stop_script() -> str:
    """Stop the server server.json names (its whole process group)."""
    return f"""S="$HOME/{STATE}"
pid=$(sed -n 's/.*"pid": *\\([0-9]*\\).*/\\1/p' "$S/server.json" 2>/dev/null)
if [ -n "$pid" ] && grep -qa agent-server "/proc/$pid/cmdline" 2>/dev/null; then
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  # Wait for it to go (it holds the port the next server binds), then insist. A zombie
  # (exited, not yet reaped) counts as gone.
  running() {{ kill -0 "$1" 2>/dev/null && ! grep -q '^State:.*Z' "/proc/$1/status" 2>/dev/null; }}
  i=0
  while running "$pid" && [ $i -lt 50 ]; do sleep 0.2; i=$((i + 1)); done
  if running "$pid"; then kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true; fi
fi
rm -f "$S/server.json"
"""


def health_script(port: int) -> str:
    """HTTP status of an authenticated call with the key in server.json: 200 only from
    the server just started (an old one still shutting down has another key)."""
    return f"""S="$HOME/{STATE}"
key=$(sed -n 's/.*"key": *"\\([^"]*\\)".*/\\1/p' "$S/server.json" 2>/dev/null)
curl -sS -m 3 -o /dev/null -w '%{{http_code}}' -H "X-Session-API-Key: $key" \\
  "http://127.0.0.1:{int(port)}/api/conversations/search?limit=1" 2>/dev/null
"""


@dataclass
class Probe:
    state: dict
    alive: bool
    healthy: bool

    def reusable(self, version: str, port: int) -> bool:
        s = self.state
        return (
            self.alive
            and self.healthy
            and s.get("version") == version
            and s.get("port") == port
            and isinstance(s.get("key"), str)
            and bool(s.get("key"))
        )


def parse_probe(stdout: str) -> Probe:
    state: object = {}
    for line in stdout.splitlines():
        if line.startswith("STATE "):
            try:
                state = json.loads(base64.b64decode(line[6:]))
            except ValueError:
                state = {}
    lines = set(stdout.splitlines())
    return Probe(state=state if isinstance(state, dict) else {}, alive="ALIVE=1" in lines, healthy="HEALTH=1" in lines)


def parse_install(stdout: str) -> dict[str, str]:
    out = {}
    for line in stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep and k in ("HOME", "PY", "INSTALLED"):
            out[k] = v.strip()
    if "HOME" not in out or "PY" not in out:
        raise RuntimeError(f"agent-server install printed no paths: {stdout[-500:]!r}")
    return out
