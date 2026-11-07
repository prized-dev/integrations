"""What runs inside a session's sandbox: Anthropic's worker, ``ant beta:worker run``, one per run.

Each run (one work item) gets a directory, ``~/.prized-managed-agents/runs/<run id>/``:

- ``secret``: the work item's secret, mode 0600, read by ant through ``ANTHROPIC_WORK_SECRET_FILE``
  and removed when the worker exits. It carries a token for this one session; the environment key,
  which can claim any session in the environment, never enters a sandbox.
- ``session``, ``work``: whose run it is. ``acked``: present once the queue took our ack.
- ``pid``: the worker's process group, while it runs. ``done``: its exit code, once it has exited.
- ``worker.log``: ant's output. ``collected``: the orchestrator has seen this run end.

The worker runs detached (``setsid -f``) so it outlives the command that started it; the
orchestrator's status checks, every ``probe_every_s``, are what keep the sandbox from pausing
itself mid-run.

The scripts take their inputs from environment variables (``PMA_*``), never by splicing values into
the script text, and the ids they get are checked against ``SAFE_ID`` first.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any

HOME = "/home/prized"
ROOT = ".prized-managed-agents"
RUNS = f"{ROOT}/runs"
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

# Exit codes of INSTALL_ANT.
ANT_ARCH, ANT_CHECKSUM, ANT_DOWNLOAD = 3, 4, 5


def run_id(work_id: str) -> str:
    """A run's directory name: the work item's id plus a nonce, since the same work id can come
    again (Anthropic's dispatcher notes the queue reuses it for a session's later messages)."""
    return f"{work_id}-{secrets.token_hex(4)}"


def secret_path(rid: str) -> str:
    return f"{HOME}/{RUNS}/{rid}/secret"


# Installs ant into ~/.local/bin unless that exact release (by its tarball's sha256) is there.
INSTALL_ANT = r"""set -u
cd "$HOME" || exit 1
case $(uname -m) in
  x86_64|amd64) arch=amd64; sha=$PMA_ANT_SHA256_AMD64 ;;
  aarch64|arm64) arch=arm64; sha=$PMA_ANT_SHA256_ARM64 ;;
  *) echo "no ant build for $(uname -m)" >&2; exit 3 ;;
esac
[ -n "$sha" ] || { echo "no sha256 for ant on $arch" >&2; exit 3; }
mark=.local/bin/.ant.sha256
if [ -x .local/bin/ant ] && [ "$(cat "$mark" 2>/dev/null)" = "$sha" ]; then exit 0; fi
mkdir -p .local/bin && d=$(mktemp -d .local/bin/.ant.XXXXXX) || exit 1
url=$(printf '%s' "$PMA_ANT_URL" | sed "s/{arch}/$arch/g")
if ! curl -fsSL --retry 3 -o "$d/ant.tgz" "$url"; then rm -rf "$d"; echo "downloading ant failed" >&2; exit 5; fi
if ! echo "$sha  $d/ant.tgz" | sha256sum -c --quiet - >/dev/null 2>&1; then
  rm -rf "$d"; echo "ant's download does not match its sha256" >&2; exit 4
fi
tar -xzf "$d/ant.tgz" -C "$d" ant && chmod 755 "$d/ant" && mv -f "$d/ant" .local/bin/ant && printf '%s' "$sha" > "$mark"
code=$?; rm -rf "$d"; exit $code
"""

# Makes the run's directory (0700) before the secret is written into it.
PREPARE_RUN = r"""set -eu
cd "$HOME"
mkdir -p "$PMA_RUNS"
chmod 700 "${PMA_RUNS%/runs}" "$PMA_RUNS"
mkdir -m 700 "$PMA_RUNS/$PMA_RUN"
"""

# Starts ant for one run. Prints "started <pgid>".
LAUNCH = r"""set -eu
if [ -n "${ANTHROPIC_ENVIRONMENT_KEY:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "refusing to start: an environment or API key is set, and only the session's secret may reach a sandbox" >&2
  exit 2
fi
cd "$HOME"
r=$PMA_RUNS/$PMA_RUN
[ -s "$r/secret" ] || { echo "no secret for run $PMA_RUN" >&2; exit 2; }
chmod 600 "$r/secret"
mkdir -p "$PMA_WORKDIR"
printf '%s' "$ANTHROPIC_SESSION_ID" > "$r/session"
printf '%s' "$ANTHROPIC_WORK_ID" > "$r/work"
date +%s > "$r/started"
export ANTHROPIC_WORK_SECRET_FILE="$HOME/$r/secret"
setsid -f sh -c '
  r=$1; echo $$ > "$r/pid"
  trap : TERM INT  # a stop signals the whole group; this shell outlives it to record the exit code
  "$HOME/.local/bin/ant" beta:worker run --workdir "$PMA_WORKDIR" --max-idle "${PMA_MAX_IDLE}s" >> "$r/worker.log" 2>&1
  code=$?
  rm -f "$r/secret"
  echo $code > "$r/done.tmp" && mv -f "$r/done.tmp" "$r/done"
  rm -f "$r/pid"
' sh "$HOME/$r" < /dev/null > /dev/null 2>&1
i=0
while [ ! -s "$r/pid" ] && [ ! -e "$r/done" ]; do
  i=$((i + 1)); [ $i -le 100 ] || { echo "the worker did not start" >&2; exit 1; }
  sleep 0.05
done
if [ -s "$r/pid" ]; then echo "started $(cat "$r/pid")"; else echo "exited $(cat "$r/done")"; fi
"""

# One line per run not yet collected: "<run> <session> <work> alive <pgid> <acked>",
# "<run> <session> <work> exited <code> <acked>" or "<run> <session> <work> lost - <acked>"
# (no worker and no exit code: it was killed outright).
STATUS = r"""cd "$HOME" 2>/dev/null || exit 0
[ -d "$PMA_RUNS" ] || exit 0
for r in "$PMA_RUNS"/*/; do
  r=${r%/}; [ -d "$r" ] || continue; [ -e "$r/collected" ] && continue
  [ -e "$r/session" ] || continue
  id=${r##*/}; s=$(cat "$r/session"); w=$(cat "$r/work" 2>/dev/null || echo -)
  a=0; [ -e "$r/acked" ] && a=1
  if [ -s "$r/pid" ] && kill -0 -- "-$(cat "$r/pid")" 2>/dev/null; then
    echo "$id $s $w alive $(cat "$r/pid") $a"
  elif [ -e "$r/done" ]; then
    echo "$id $s $w exited $(cat "$r/done") $a"
  else
    echo "$id $s $w lost - $a"
  fi
done
"""

MARK_ACKED = r"""cd "$HOME" && [ -d "$PMA_RUNS/$PMA_RUN" ] && touch "$PMA_RUNS/$PMA_RUN/acked"
"""

# Stops one run's worker: SIGTERM to its group (ant ends the session's in-flight tool call and
# stops the work item), PMA_GRACE seconds, then SIGKILL. The secret goes either way.
STOP = r"""cd "$HOME" || exit 0
r=$PMA_RUNS/$PMA_RUN
if [ -s "$r/pid" ]; then
  g=$(cat "$r/pid")
  kill -TERM -- "-$g" 2>/dev/null
  i=0
  while kill -0 -- "-$g" 2>/dev/null; do
    i=$((i + 1)); [ $i -le $((PMA_GRACE * 4)) ] || { kill -KILL -- "-$g" 2>/dev/null; break; }
    sleep 0.25
  done
fi
rm -f "$r/secret"
echo stopped
"""

# Marks a run collected, removes its secret, prints the end of its log, and keeps the newest 20
# runs' directories (for their logs).
COLLECT = r"""cd "$HOME" || exit 0
r=$PMA_RUNS/$PMA_RUN
[ -d "$r" ] || exit 0
rm -f "$r/secret"; touch "$r/collected"
tail -n "${PMA_TAIL:-0}" "$r/worker.log" 2>/dev/null
ls -1dt "$PMA_RUNS"/*/collected 2>/dev/null | tail -n +21 | while read -r c; do rm -rf "${c%/collected}"; done
exit 0
"""


def env(rid: str | None = None, **extra: str) -> dict[str, str]:
    out = {"PMA_RUNS": RUNS}
    if rid is not None:
        if not SAFE_ID.fullmatch(rid):
            raise ValueError(f"unsafe run id {rid!r}")
        out["PMA_RUN"] = rid
    out.update(extra)
    return out


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    session_id: str
    work_id: str
    state: str  # alive, exited or lost
    number: int | None  # the pgid while alive, the exit code once exited
    acked: bool


def parse_status(text: str) -> list[RunStatus]:
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 6 or parts[3] not in ("alive", "exited", "lost"):
            continue
        num = int(parts[4]) if parts[4].lstrip("-").isdigit() else None
        out.append(RunStatus(parts[0], parts[1], parts[2], parts[3], num, parts[5] == "1"))
    return out


class InstallError(Exception):
    pass


async def install_ant(sb: Any, settings: Any) -> None:
    """Install ``settings.ant_version`` in the sandbox unless that exact build is there already."""
    env = {
        "PMA_ANT_URL": settings.ant_url.replace("{version}", settings.ant_version),
        "PMA_ANT_SHA256_AMD64": settings.ant_sha256.get("amd64", ""),
        "PMA_ANT_SHA256_ARM64": settings.ant_sha256.get("arm64", ""),
    }
    r = await sb.exec(INSTALL_ANT, env=env, timeout=300)
    if r.exit_code != 0:
        raise InstallError(f"installing ant {settings.ant_version} failed ({r.exit_code}): {r.stderr.strip()[-300:]}")
