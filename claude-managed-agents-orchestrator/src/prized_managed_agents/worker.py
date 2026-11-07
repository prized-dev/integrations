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

The agent's tools run in the same sandbox as the same user, so anything in these directories may
have been written by the agent. The status script prints only well-formed fields, and the
orchestrator believes a run only if its id carries a valid signature: the id ends in an HMAC, keyed
from the environment key (which the sandbox never sees), over the session, the work item, the start
time and a nonce. A line the agent made up, or a run whose ``session`` or ``work`` it changed, is
ignored.

The scripts take their inputs from environment variables (``PMA_*``), never by splicing values into
the script text, and the ids they get are checked against ``SAFE_ID`` first. Prized runs commands
with bash where there is one and sh otherwise, so the run scripts stay within POSIX sh (dash's
``kill`` takes no ``--``, so a process group is ``kill -SIG -<pgid>``, the pgid checked as a number
above 1 first). LAUNCH needs util-linux's ``setsid -f`` and INSTALL_ANT curl and GNU sha256sum, as
Prized's templates have.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

HOME = "/home/prized"
ROOT = ".prized-managed-agents"
RUNS = f"{ROOT}/runs"
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

# Exit codes of INSTALL_ANT.
ANT_ARCH, ANT_CHECKSUM, ANT_DOWNLOAD = 3, 4, 5


PREFIX = 48  # how much of the work id a run id keeps, for people reading the runs directory
_RUN_ID = re.compile(
    r"(?P<prefix>[A-Za-z0-9_-]{1,48})-(?P<started>[0-9]{10})-(?P<nonce>[0-9a-f]{8})-(?P<tag>[0-9a-f]{20})"
)


def run_key(environment_key: str) -> bytes:
    """The key run ids are signed with: derived from the environment key, so it never leaves the
    orchestrator, and every orchestrator of the environment shares it."""
    return hmac.new(environment_key.encode(), b"prized-managed-agents run id v1", hashlib.sha256).digest()


def _tag(key: bytes, session_id: str, work_id: str, started: int, nonce: str) -> str:
    msg = f"{session_id}\n{work_id}\n{started}\n{nonce}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:20]


def run_id(key: bytes, session_id: str, work_id: str, *, now: float | None = None) -> str:
    """A run's id and directory name: the start of the work id, the start time, a nonce (the same work
    id can come again: Anthropic's dispatcher notes the queue reuses it for a session's later
    messages) and the signature. At most 89 characters, whatever the work id's length."""
    started = int(time.time() if now is None else now)
    nonce = secrets.token_hex(4)
    return f"{work_id[:PREFIX]}-{started}-{nonce}-{_tag(key, session_id, work_id, started, nonce)}"


def started_at(rid: str) -> int | None:
    """When the run started (Unix seconds), from its id; None for an id that isn't a run id."""
    m = _RUN_ID.fullmatch(rid)
    return int(m.group("started")) if m else None


def genuine(key: bytes | list[bytes], st: RunStatus) -> bool:
    """Whether a status line describes a run an orchestrator of this environment started (signed
    with ``key``, or any of a list: the current key and the one before a rotation)."""
    m = _RUN_ID.fullmatch(st.run_id)
    if m is None or m.group("prefix") != st.work_id[:PREFIX]:
        return False
    started, nonce = int(m.group("started")), m.group("nonce")
    keys = key if isinstance(key, list) else [key]
    return any(hmac.compare_digest(m.group("tag"), _tag(k, st.session_id, st.work_id, started, nonce)) for k in keys)


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

# Makes the run's directory (0700) and says whose run it is, before the secret is written into it.
# From here until the worker writes its pid, the status script reports the run as starting.
PREPARE_RUN = r"""set -eu
cd "$HOME"
mkdir -p "$PMA_RUNS"
chmod 700 "${PMA_RUNS%/runs}" "$PMA_RUNS"
mkdir -m 700 "$PMA_RUNS/$PMA_RUN"
printf '%s' "$PMA_SESSION" > "$PMA_RUNS/$PMA_RUN/session"
printf '%s' "$PMA_WORK" > "$PMA_RUNS/$PMA_RUN/work"
"""

# Starts ant for one run. Prints "started <pgid>", or "exited <code>" if it is over already.
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

# One line per run: "<run> <session> <work> <state> <number> <acked>", where state and number are
#   alive <pgid>     the worker's process group is alive
#   stray <pgid>     the run was collected, yet the process group in its pid file is alive
#   exited <code>    it exited with that code
#   lost -           it had a process group and no exit code: it was killed outright
#   starting -       no worker yet: the handoff is under way, or died before the launch
# Other collected runs are left out. Fields that aren't plain ids (the agent can write these files)
# leave the run out, so a newline in a file can't add a line, and only the newest PMA_SCAN entries
# (by modification time) are looked at, so a directory full of them can't make the check slow.
STATUS = r"""cd "$HOME" 2>/dev/null || exit 0
[ -d "$PMA_RUNS" ] || exit 0
safe() { case $1 in ''|*[!A-Za-z0-9_-]*) return 1 ;; esac; [ ${#1} -le 128 ]; }
ls -1t "$PMA_RUNS" 2>/dev/null | head -n "${PMA_SCAN:-200}" | while read -r id; do
  safe "$id" || continue
  r=$PMA_RUNS/$id; [ -d "$r" ] || continue
  [ -f "$r/session" ] && [ -f "$r/work" ] || continue
  s=$(head -c 129 "$r/session"); w=$(head -c 129 "$r/work")
  safe "$s" && safe "$w" || continue
  a=0; [ -e "$r/acked" ] && a=1
  g=; [ -f "$r/pid" ] && g=$(head -c 12 "$r/pid")
  case $g in ''|*[!0-9]*) g= ;; esac
  if [ -n "$g" ] && [ "$g" -gt 1 ] && kill -0 "-$g" 2>/dev/null; then
    if [ -e "$r/collected" ]; then echo "$id $s $w stray $g $a"; else echo "$id $s $w alive $g $a"; fi
  elif [ -e "$r/collected" ]; then
    continue
  elif [ -f "$r/done" ]; then
    c=$(head -c 12 "$r/done"); case $c in ''|*[!0-9]*) c=- ;; esac
    echo "$id $s $w exited $c $a"
  elif [ -e "$r/pid" ]; then
    echo "$id $s $w lost - $a"
  else
    echo "$id $s $w starting - $a"
  fi
done
exit 0
"""

MARK_ACKED = r"""cd "$HOME" && [ -d "$PMA_RUNS/$PMA_RUN" ] && touch "$PMA_RUNS/$PMA_RUN/acked"
"""

# Stops one run's worker: SIGTERM to its group (ant ends the session's in-flight tool call and
# stops the work item), PMA_GRACE seconds, then SIGKILL. PMA_SIGNAL=KILL skips the SIGTERM, for a
# worker whose own stop could reach a newer work item with the same id. The secret goes either way.
STOP = r"""cd "$HOME" || exit 0
r=$PMA_RUNS/$PMA_RUN
g=; [ -f "$r/pid" ] && g=$(head -c 12 "$r/pid")
case $g in ''|*[!0-9]*) g= ;; esac
if [ -n "$g" ] && [ "$g" -gt 1 ]; then
  if [ "${PMA_SIGNAL:-TERM}" = KILL ]; then kill -KILL "-$g" 2>/dev/null; else kill -TERM "-$g" 2>/dev/null; fi
  i=0
  while kill -0 "-$g" 2>/dev/null; do
    i=$((i + 1)); [ $i -le $((PMA_GRACE * 4)) ] || { kill -KILL "-$g" 2>/dev/null; break; }
    sleep 0.25
  done
fi
rm -f "$r/secret"
echo stopped
"""

# Marks a run collected, removes its secret, prints the end of its log, and keeps the newest 20
# runs' directories (for their logs). A pid file stays only while its group is alive, so a pid
# the system hands out again later can't make a collected run look alive.
COLLECT = r"""cd "$HOME" || exit 0
r=$PMA_RUNS/$PMA_RUN
[ -d "$r" ] || exit 0
rm -f "$r/secret"; touch "$r/collected"
g=; [ -f "$r/pid" ] && g=$(head -c 12 "$r/pid")
case $g in ''|*[!0-9]*) g= ;; esac
{ [ -n "$g" ] && [ "$g" -gt 1 ] && kill -0 "-$g" 2>/dev/null; } || rm -f "$r/pid"
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


STATES = ("alive", "stray", "exited", "lost", "starting")
_NUMBER = re.compile(r"-|[0-9]{1,12}")


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    session_id: str
    work_id: str
    state: str  # alive, stray, exited, lost or starting
    number: int | None  # the pgid while alive (or stray), the exit code once exited
    acked: bool

    @property
    def started(self) -> int | None:
        return started_at(self.run_id)


def parse_status(text: str) -> list[RunStatus]:
    """The status script's lines, each field checked again here; anything else is skipped."""
    out = []
    for line in text.splitlines():
        parts = line.split(" ")
        if len(parts) != 6:
            continue
        rid, sid, wid, state, num, acked = parts
        if state not in STATES or acked not in ("0", "1") or not _NUMBER.fullmatch(num):
            continue
        if not all(SAFE_ID.fullmatch(x) for x in (rid, sid, wid)):
            continue
        number = None if num == "-" else int(num)
        if state in ("alive", "stray") and (number is None or number <= 1):
            continue
        out.append(RunStatus(rid, sid, wid, state, number, acked == "1"))
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
