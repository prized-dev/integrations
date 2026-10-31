#!/usr/bin/env bash
# spawn.sh: run each Cursor Cloud Agent's tools in a Prized sandbox of its own, paused between jobs.
#
# `agent worker controller --spawn ./spawn.sh --session-token --pool <name>` runs this once per
# claimed request, and once per wake (CURSOR_WAKE=1): a follow-up for an agent whose worker released,
# on a pool with a reconnect window. A claim creates a sandbox and starts `agent worker` there under
# the worker id Cursor assigned. A wake resumes that worker's paused sandbox, memory, processes and
# files as they were, and starts the worker again under the same id; with nothing kept, it starts a
# fresh sandbox. Either way it returns at once. A background watcher pauses the sandbox once the
# worker releases after its idle window (exit 0), and deletes it if the worker fails, or always with
# CURSOR_HIBERNATE=0. Needs `prized` (signed in, or PRIZED_API_KEY with Sandboxes: Write), jq, bash.
set -euo pipefail

TEMPLATE=${PRIZED_TEMPLATE:-prized-base-v1}
MAX_RUNTIME=${MAX_RUNTIME:-}               # the most a sandbox runs from its create or a resume; unset is Prized's default
IDLE=${CURSOR_IDLE_RELEASE:-600}           # seconds a worker waits for a follow-up, then releases
FLAGS=${CURSOR_WORKER_FLAGS---clone-git-repos}   # the `agent worker` flags beside --pool and --worker-id
HIBERNATE=${CURSOR_HIBERNATE:-1}           # 1: a released worker's sandbox is paused for a follow-up; 0: deleted
KEEP=${CURSOR_HIBERNATE_TTL:-3d}           # how long a paused worker is kept before Prized deletes it
LOGS=${PRIZED_CURSOR_LOGS:-${XDG_STATE_HOME:-$HOME/.local/state}/prized-cursor}  # a log per request

: "${CURSOR_AGENT_WORKER_ID:?run me with agent worker controller --spawn}" "${CURSOR_POOL:?}"
ID=$CURSOR_AGENT_WORKER_ID
WAKE=${CURSOR_WAKE:-0}
log() { echo "[spawn] $ID: $*" >&2; }
[[ $ID =~ ^[A-Za-z0-9._-]+$ && $CURSOR_POOL =~ ^[A-Za-z0-9._-]+$ ]] || { log "unexpected worker id or pool"; exit 1; }
[[ $IDLE =~ ^[1-9][0-9]*$ ]] || { log "CURSOR_IDLE_RELEASE must be a number of seconds above 0"; exit 1; }
EVERY=${PRIZED_CURSOR_POLL:-30}            # seconds between polls; well under the 5-minute auto-pause
[[ $EVERY =~ ^[0-9]+(\.[0-9]+)?$ && ! $EVERY =~ ^0+(\.0+)?$ ]] || { log "PRIZED_CURSOR_POLL must be a number of seconds above 0"; exit 1; }
[[ $FLAGS =~ ^[A-Za-z0-9\ ._=/:,-]*$ ]] || { log "CURSOR_WORKER_FLAGS takes plain flags only, no quotes"; exit 1; }
if [ "$CURSOR_POOL" = default ] && [[ " $FLAGS " == *" --clone-git-repos "* ]]; then
  log "--clone-git-repos needs a named pool, not default"; exit 1
fi
[[ $HIBERNATE =~ ^[01]$ ]] || { log "CURSOR_HIBERNATE must be 1 (pause released workers) or 0 (delete them)"; exit 1; }
[[ $KEEP =~ ^[1-9][0-9]*[mhd]?$ ]] || { log "CURSOR_HIBERNATE_TTL must be a duration such as 90m, 12h or 3d"; exit 1; }
[ "$HIBERNATE" = 0 ] || command -v jq >/dev/null || { log "pausing workers needs jq on this machine (or set CURSOR_HIBERNATE=0)"; exit 1; }
# A session token reaches only this claim and ends with it; a wake brings a fresh one for the same
# claim. The service-account key stays here.
: "${CURSOR_AUTH_TOKEN:?start the controller with --session-token, so the service-account key never enters a sandbox}"
mkdir -p "$LOGS"
GEN=$(date +%s).$$                         # this start of the worker: a watcher acts only on its own

read -r -d '' INSTALL <<'EOF' || true
mkdir -p ~/work ~/.cursor-worker && chmod 700 ~/.cursor-worker
[ -x ~/.local/bin/agent ] || curl -fsS https://cursor.com/install | bash >/dev/null 2>&1
~/.local/bin/agent --version
EOF
# The worker runs detached in a process group of its own; its exit code lands in ~/.cursor-worker/done.
read -r -d '' START <<EOF || true
cd ~/work && rm -f ~/.cursor-worker/done ~/.cursor-worker/pid && echo $GEN > ~/.cursor-worker/gen && setsid -f sh -c '
  echo \$\$ > ~/.cursor-worker/pid
  . ~/.cursor-worker/env
  ~/.local/bin/agent worker --pool $CURSOR_POOL --worker-id $ID --auth-token-file ~/.cursor-worker/token \\
    --idle-release-timeout $IDLE $FLAGS start >> ~/.cursor-worker/worker.log 2>&1
  echo \$? > ~/.cursor-worker/done' < /dev/null > /dev/null 2>&1
for i in \$(seq 50); do [ -s ~/.cursor-worker/pid ] && exit 0; sleep 0.1; done; exit 1
EOF
# Prints the running worker's generation, then "exited" once it has exited or "running" while it
# runs. (Not an exit code: the prized CLI's own codes include 3 for an unreachable sandbox.)
# shellcheck disable=SC2016 # runs in the sandbox
POLL='g=$(cat ~/.cursor-worker/gen 2>/dev/null); if [ -e ~/.cursor-worker/done ] || ! kill -0 "$(cat ~/.cursor-worker/pid 2>/dev/null)" 2>/dev/null; then echo "$g exited"; else echo "$g running"; fi'

# A lock per worker id on this machine, so a wake and a watcher never act on one sandbox at once. A
# holder never keeps it 10 minutes; a lock that old was left by a process that died.
lock() {  # lock ID [try]: take ID's lock; with "try", only if it is free now
  local dir=$LOGS/$1.lock _
  for _ in $(seq 1200); do
    mkdir "$dir" 2>/dev/null && return 0
    [ "${2:-}" = try ] && return 1
    [ -z "$(find "$dir" -maxdepth 0 -mmin +10 2>/dev/null)" ] || rmdir "$dir" 2>/dev/null || true
    sleep 0.5
  done
  return 1
}
unlock() { rmdir "$LOGS/$1.lock" 2>/dev/null || true; }
HELD=0  # whether this process holds its own worker id's lock
take() { lock "$ID" && HELD=1; }
drop() { [ "$HELD" = 0 ] || unlock "$ID"; HELD=0; }
poll() { local out; out=$(prized sandbox exec "$SBX" --timeout 30s -- "$POLL" 2>/dev/null) || return 0; echo "${out##*$'\n'}"; }

# A paused worker is a cache. When the workspace has no room for a new sandbox, the pool's
# longest-paused worker that no wake is using is deleted to make room.
make_room() {
  local old
  for old in $(prized sandbox ls --label "cursor-pool=$CURSOR_POOL" --json | jq -r '[.sandboxes[]
      | select(.state == "paused" and (.labels["cursor-worker"] // "") != "")]
      | sort_by((.lastActiveAt // .createdAt // "") | sub("\\.[0-9]+"; "")) | .[].labels["cursor-worker"]'); do
    if ! [[ $old =~ ^[A-Za-z0-9._-]+$ && $old != "$ID" ]] || ! lock "$old" try; then continue; fi
    local sbx
    sbx=$(prized sandbox ls --label "cursor-worker=$old" --json | jq -r '.sandboxes[] | select(.state == "paused") | .id' | head -1)
    if [ -n "$sbx" ] && prized sandbox rm "$sbx" >/dev/null; then
      log "deleted $sbx, paused worker $old, to make room"; unlock "$old"; return 0
    fi
    unlock "$old"
  done
  return 1
}

# fresh: create a sandbox, install the Cursor CLI and hand it this claim's token.
fresh() {
  # The watcher's polls keep the sandbox running. If they stop (this machine goes down), it pauses
  # 5 minutes after the last one, and is deleted after it has been paused for the TTL (or 10 minutes
  # with CURSOR_HIBERNATE=0); a wake meanwhile picks it up where it stopped.
  local create err try=1 keep=$KEEP code
  [ "$HIBERNATE" = 1 ] || keep=10m
  create=(--template "$TEMPLATE" --auto-pause 5m --auto-delete "$keep" --label "cursor-worker=$ID" --label "cursor-pool=$CURSOR_POOL")
  [[ ${CURSOR_REQUEST_ID:-} =~ ^[A-Za-z0-9._-]+$ ]] && create+=(--label "cursor-agent=$CURSOR_REQUEST_ID")
  [ -z "$MAX_RUNTIME" ] || create+=(--max-runtime "$MAX_RUNTIME")
  err=$(mktemp)
  until SBX=$(prized sandbox create "${create[@]}" 2> "$err"); do
    code=$?
    cat "$err" >&2
    # Exit 6 with one of these is a workspace quota that paused sandboxes count toward.
    if [ "$code" = 6 ] && [ "$try" -lt 4 ] && grep -q -E 'paused sandboxes|disk limit|running vCPUs|running memory' "$err" && make_room; then
      try=$((try + 1)); continue
    fi
    rm -f "$err"; return 1
  done
  rm -f "$err"
  log "created $SBX"
  NEW=1  # until the worker is up, a failed step deletes it
  prized sandbox exec "$SBX" --timeout 5m -- "$INSTALL"
  NAME=$(printf '%s' "${CURSOR_WORKER_NAME:-prized-$ID}" | tr -cd 'A-Za-z0-9._-')
  printf 'export CURSOR_AGENT_WORKER_ID=%s CURSOR_WORKER_NAME=%s\n' "$ID" "${NAME:-prized-$ID}" |
    prized sandbox cp - "$SBX:~/.cursor-worker/env"
}

# kept: find the sandbox this worker id left paused and resume it. Fails, leaving it alone, if
# there is none or it can't run (resume refused, error state); the caller then starts a fresh one.
kept() {
  local row state _
  row=$(prized sandbox ls --label "cursor-worker=$ID" --json | jq -r '[.sandboxes[] | select(.state != "deleting")][0] // empty | "\(.id) \(.state)"') || return 1
  [ -n "$row" ] || { log "wake: no sandbox kept for it"; return 1; }
  SBX=${row% *} state=${row#* }
  for _ in $(seq 30); do  # a pause or resume in flight settles within seconds
    case $state in pausing|starting|resizing) sleep 2; state=$(prized sandbox get "$SBX" --json | jq -r .sandbox.state) || return 1 ;; *) break ;; esac
  done
  case $state in
    running) log "wake: $SBX is running" ;;
    paused) prized sandbox resume "$SBX" >/dev/null || { log "wake: could not resume $SBX; it stays paused"; return 1; }
      log "resumed $SBX" ;;
    error) log "wake: $SBX lost its state (error); deleting it"; prized sandbox rm "$SBX" >/dev/null || true; return 1 ;;
    *) log "wake: $SBX is $state"; return 1 ;;
  esac
}

remove() { if prized sandbox rm "$SBX" >/dev/null 2>&1; then log "deleted $SBX"; else log "could not delete $SBX; it pauses and is deleted by itself"; fi; }

(
  SBX='' NEW=0
  trap '[ "$NEW" = 0 ] || remove; drop' EXIT
  take || { log "could not take $LOGS/$ID.lock"; exit 1; }
  if [ "$WAKE" = 1 ] && [ "$HIBERNATE" = 1 ] && kept; then
    for _ in 1 2 3; do state=$(poll); [ -n "$state" ] && break; sleep 2; done
    # Never start a second worker beside one we can't see: leave it, and let the wake window lapse.
    [ -n "$state" ] || { log "wake: $SBX does not answer; leaving it"; exit 1; }
  else
    fresh
    state=
  fi
  printf '%s' "$CURSOR_AUTH_TOKEN" | prized sandbox cp - "$SBX:~/.cursor-worker/token"
  unset CURSOR_AUTH_TOKEN
  prized sandbox exec "$SBX" --timeout 60s -- 'chmod 600 ~/.cursor-worker/token ~/.cursor-worker/env'
  if [ "${state#* }" = running ]; then
    GEN=${state% *}  # a wake while the worker still serves: it reads the new token when it reconnects
    log "worker still running; token replaced"
  else
    prized sandbox exec "$SBX" --timeout 60s -- "$START"
    log "worker started"
  fi
  NEW=0
  trap - EXIT
  drop

  # From here the sandbox is paused or deleted only once its worker has exited, and only by the
  # watcher of that start of the worker. A stopped watcher or failing polls leave it to pause by
  # itself, so an outage never deletes a live agent's sandbox.
  trap 'log "watcher stopped; $SBX pauses by itself"; drop; exit 0' TERM HUP INT
  while :; do
    sleep "$EVERY" || true  # a failed or killed sleep must not end the loop: only "exited" does
    state=$(poll)
    case $state in
      "$GEN running") continue ;;
      "$GEN exited") ;;
      *" running" | *" exited") log "a newer start of the worker watches $SBX"; exit 0 ;;
      *) # Not a pipeline: under pipefail, get's non-zero exit would hide the match.
        info=$(prized sandbox get "$SBX" 2>&1) || case $info in *"no sandbox"*) log "$SBX is gone"; exit 0 ;; esac
        continue ;;
    esac
    take || continue
    # Again, under the lock: another watcher may have paused it (a poll would resume it), or a wake
    # started the worker again meanwhile.
    if [ "$HIBERNATE" = 1 ]; then st=$(prized sandbox get "$SBX" --json 2>/dev/null | jq -r '.sandbox.state // empty') || st=; else st=running; fi
    case $st in running) ;; paused | pausing) log "$SBX is $st already"; drop; exit 0 ;; *) drop; continue ;; esac
    state=$(poll)
    [ "$state" = "$GEN exited" ] || { drop; continue; }
    code=$(prized sandbox exec "$SBX" --timeout 30s -- 'cat ~/.cursor-worker/done 2>/dev/null || echo killed') || code=unknown
    log "worker exited ($code)"
    prized sandbox cp "$SBX:~/.cursor-worker/worker.log" "$LOGS/$ID.worker.log" >/dev/null 2>&1 || true
    if [ "$HIBERNATE" = 1 ] && { [ "$code" = 0 ] || [ "$code" = unknown ]; }; then
      # Released after its idle window (exit 0). Its claim may come back as a wake: keep
      # everything but the token, which a wake replaces. A worker that failed is deleted.
      prized sandbox exec "$SBX" --timeout 30s -- 'rm -f ~/.cursor-worker/token' >/dev/null || true
      if prized sandbox pause "$SBX" --wait >/dev/null; then
        log "paused $SBX; a follow-up resumes it, or it is deleted after $KEEP"
      else
        log "could not pause $SBX; it pauses by itself"
      fi
    else
      remove
    fi
    drop
    exit 0
  done
) >> "$LOGS/$ID.log" 2>&1 < /dev/null &

if [ "$WAKE" = 1 ]; then log "waking its sandbox; see $LOGS/$ID.log"; else log "starting its sandbox; see $LOGS/$ID.log"; fi
