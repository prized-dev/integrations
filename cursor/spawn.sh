#!/usr/bin/env bash
# spawn.sh: run each Cursor Cloud Agent's tools in a Prized sandbox of its own, paused between jobs.
#
# `agent worker controller --spawn ./spawn.sh --session-token --pool <name>` runs this once per
# claimed request, and once per wake (CURSOR_WAKE=1): a follow-up for an agent whose worker went
# offline, on a pool with a reconnect window. A claim creates a sandbox and starts `agent worker`
# there under the worker id Cursor assigned. A wake resumes that worker's paused sandbox, memory,
# processes and files as they were, and starts the worker again under the same id; with nothing to
# resume, it starts a fresh sandbox. Either way it returns at once. A background watcher pauses the
# sandbox once the worker exits (Cursor's idle release), or deletes it with CURSOR_HIBERNATE=0.
# Needs `prized` (signed in, or PRIZED_API_KEY with Sandboxes: Write), jq and bash.
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
GRACE=${PRIZED_CURSOR_FAIL_GRACE:-120}     # a new sandbox whose worker fails sooner than this is deleted, not paused
[[ $GRACE =~ ^[0-9]+$ ]] || { log "PRIZED_CURSOR_FAIL_GRACE must be a number of seconds"; exit 1; }
[[ $FLAGS =~ ^[A-Za-z0-9\ ._=/:,-]*$ ]] || { log "CURSOR_WORKER_FLAGS takes plain flags only, no quotes"; exit 1; }
if [ "$CURSOR_POOL" = default ] && [[ " $FLAGS " == *" --clone-git-repos "* ]]; then
  log "--clone-git-repos needs a named pool, not default"; exit 1
fi
[[ $HIBERNATE =~ ^[01]$ ]] || { log "CURSOR_HIBERNATE must be 1 (pause released workers) or 0 (delete them)"; exit 1; }
[[ $KEEP =~ ^([1-9][0-9]{0,5})([mhd]?)$ ]] || { log "CURSOR_HIBERNATE_TTL must be a duration such as 90m, 12h or 3d"; exit 1; }
case ${BASH_REMATCH[2]} in d) min=$((BASH_REMATCH[1] * 1440)) ;; h) min=$((BASH_REMATCH[1] * 60)) ;; *) min=${BASH_REMATCH[1]} ;; esac
[ "$min" -le 40320 ] || { log "CURSOR_HIBERNATE_TTL can be at most 28d (Prized's longest auto-delete)"; exit 1; }
[ "$HIBERNATE" = 0 ] || command -v jq >/dev/null || { log "pausing workers needs jq on this machine (or set CURSOR_HIBERNATE=0)"; exit 1; }
# A session token reaches only this claim and ends with it; a wake brings a fresh one for the same
# claim. The service-account key stays here.
: "${CURSOR_AUTH_TOKEN:?start the controller with --session-token, so the service-account key never enters a sandbox}"
mkdir -p "$LOGS"
GEN=$(date +%s).$$                         # this start of the worker: a watcher acts only on its own
WAIT=1800                                  # half-seconds to wait for this worker's lock: 15 minutes,
if [ "$WAKE" = 1 ] && [[ ${CURSOR_WAKE_TIMEOUT_MS:-} =~ ^[0-9]{1,9}$ ]]; then  # or a wake's window
  WAIT=$((CURSOR_WAKE_TIMEOUT_MS / 500)); [ "$WAIT" -le 1800 ] || WAIT=1800
fi

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
# Ends a worker that is still there when a wake comes: Cursor sees it offline (that is why it woke
# it), so it is on its way out or can't reach Cursor, and a fresh start serves the follow-up.
# shellcheck disable=SC2016 # runs in the sandbox
STOP='p=$(cat ~/.cursor-worker/pid 2>/dev/null) && kill -TERM -- "-$p" 2>/dev/null || exit 0
for i in $(seq 100); do kill -0 "$p" 2>/dev/null || exit 0; sleep 0.1; done; kill -KILL -- "-$p" 2>/dev/null; exit 0'
# Prints the running worker's generation, then "exited" once it has exited or "running" while it
# runs. (Not an exit code: the prized CLI's own codes include 3 for an unreachable sandbox.)
# shellcheck disable=SC2016 # runs in the sandbox
POLL='g=$(cat ~/.cursor-worker/gen 2>/dev/null); if [ -e ~/.cursor-worker/done ] || ! kill -0 "$(cat ~/.cursor-worker/pid 2>/dev/null)" 2>/dev/null; then echo "$g exited"; else echo "$g running"; fi'

# A lock per worker id on this machine, so a wake, a watcher and reap.sh never act on one sandbox at
# once. A holder never keeps it 15 minutes; a lock that old was left by a process that died.
lock() {  # lock ID [try]: take ID's lock; with "try", only if it is free now
  local dir=$LOGS/$1.lock _
  for _ in $(seq "$WAIT"); do
    mkdir "$dir" 2>/dev/null && return 0
    [ "${2:-}" = try ] && return 1
    if [ -n "$(find "$dir" -maxdepth 0 -mmin +15 2>/dev/null)" ] && mv "$dir" "$dir.stale.$$" 2>/dev/null; then
      rmdir "$dir.stale.$$" 2>/dev/null || true; continue
    fi
    sleep 0.5
  done
  return 1
}
unlock() { rmdir "$LOGS/$1.lock" 2>/dev/null || true; }
HELD=0  # whether this process holds its own worker id's lock
take() { lock "$ID" && HELD=1; }
drop() { [ "$HELD" = 0 ] || unlock "$ID"; HELD=0; }
poll() { local out; out=$(prized sandbox exec "$SBX" --timeout 30s -- "$POLL" 2>/dev/null) || return 0; echo "${out##*$'\n'}"; }
# state_of SBX: its state without waking it (get is not activity), "gone" once deleted, "" if unknown.
state_of() {
  local out
  out=$(prized sandbox get "$1" --json 2>/dev/null) || true  # --json puts a refusal on stdout too
  jq -r 'if .ok == true then .sandbox.state // empty
    elif (.error.message // "" | test("^no sandbox ")) then "gone" else empty end' <<< "$out" 2>/dev/null || true
}
# jq: an API time as seconds, its fraction kept, so workers paused in the same second sort right.
TS='def ts: (. // "1970-01-01T00:00:00Z") | (sub("\\.[0-9]+"; "") | fromdateiso8601) + ((capture("(?<f>\\.[0-9]+)").f // "0") | tonumber);'

# A paused worker is a cache. When the workspace has no room for a new sandbox, the pool's
# longest-paused worker that no wake is using is deleted, and the create waits until it is gone.
make_room() {
  local old sbx _
  for old in $(prized sandbox ls --label "cursor-pool=$CURSOR_POOL" --json | jq -r "$TS"' [.sandboxes[]
      | select(.state == "paused" and (.labels["cursor-worker"] // "") != "")]
      | sort_by(.lastActiveAt // .createdAt | ts) | .[].labels["cursor-worker"]'); do
    if ! [[ $old =~ ^[A-Za-z0-9._-]+$ && $old != "$ID" ]] || ! lock "$old" try; then continue; fi
    sbx=$(prized sandbox ls --label "cursor-worker=$old" --json | jq -r '.sandboxes[] | select(.state == "paused") | .id' | head -1)
    if [ -n "$sbx" ] && prized sandbox rm "$sbx" >/dev/null; then
      log "deleting $sbx, paused worker $old, to make room"
      for _ in $(seq 60); do [ "$(state_of "$sbx")" = gone ] && break; sleep 1; done  # it counts until it is gone
      unlock "$old"; return 0
    fi
    unlock "$old"
  done
  return 1
}

# fresh: create a sandbox, install the Cursor CLI and write the worker's environment.
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
    # Exit 6 saying one of these is a quota paused sandboxes count toward (the message line only:
    # the hint of the running-sandbox limit, which they don't count toward, mentions disk too).
    if [ "$code" = 6 ] && [ "$try" -lt 4 ] && grep '^prized:' "$err" | grep -q -E 'limit of paused sandboxes|disk limit|running vCPUs|running memory' && make_room; then
      try=$((try + 1)); continue
    fi
    rm -f "$err"; return 1
  done
  rm -f "$err"
  log "created $SBX"
  NEW=1  # until the worker is up, a failed step deletes it
  touch "$LOGS/$ID.lock"  # a long install must not make the lock look stale
  prized sandbox exec "$SBX" --timeout 5m -- "$INSTALL"
  NAME=$(printf '%s' "${CURSOR_WORKER_NAME:-prized-$ID}" | tr -cd 'A-Za-z0-9._-')
  printf 'export CURSOR_AGENT_WORKER_ID=%s CURSOR_WORKER_NAME=%s\n' "$ID" "${NAME:-prized-$ID}" |
    prized sandbox cp - "$SBX:~/.cursor-worker/env"
}

# kept: resume the sandbox this worker id left. Returns 0 when it runs again, 1 when there is
# nothing to resume (none, lost, or a resume refused while it stays paused; start a fresh one), and
# 2 when it can't tell: then it leaves the sandbox alone, rather than start a second worker beside
# one that may still be live.
kept() {
  local row state why _
  for _ in 1 2 3; do
    row=$(prized sandbox ls --label "cursor-worker=$ID" --json | jq -r '.sandboxes | [.[] | select(.state != "deleting")] | (.[0] // {id: "", state: "none"}) | "\(.id) \(.state)"') && break
    sleep 2
  done
  [ -n "$row" ] || { log "wake: can't list sandboxes; leaving it"; return 2; }
  [ "$row" != " none" ] || { log "wake: no sandbox kept for it"; return 1; }
  SBX=${row% *} state=${row#* }
  for _ in $(seq 30); do  # a pause or resume in flight settles within seconds
    case $state in pausing | starting | resizing | "") sleep 2; state=$(state_of "$SBX") ;; *) break ;; esac
  done
  case $state in
    running) log "wake: $SBX is running" ;;
    paused) prized sandbox resume "$SBX" >/dev/null || { log "wake: could not resume $SBX; it stays paused"; return 1; }
      log "resumed $SBX" ;;
    error)
      why=$(prized sandbox get "$SBX" --json 2>/dev/null | jq -r '.sandbox.errorReason // ""') || why=
      case $why in *runtime_degraded*) log "wake: $SBX is degraded and may come back; leaving it"; return 2 ;; esac
      log "wake: $SBX lost its state (error); deleting it"; prized sandbox rm "$SBX" >/dev/null || true; return 1 ;;
    gone) log "wake: $SBX is gone"; return 1 ;;
    *) log "wake: $SBX is ${state:-unknown}; leaving it"; return 2 ;;
  esac
}

remove() { if prized sandbox rm "$SBX" >/dev/null 2>&1; then log "deleted $SBX"; else log "could not delete $SBX; it pauses and is deleted by itself"; fi; }

(
  SBX='' NEW=0 WOKE=0
  trap '[ "$NEW" = 0 ] || remove; drop' EXIT
  take || { log "could not take $LOGS/$ID.lock"; exit 1; }
  rc=1
  if [ "$WAKE" = 1 ] && [ "$HIBERNATE" = 1 ]; then if kept; then rc=0; else rc=$?; fi; fi
  case $rc in
    0) WOKE=1
      state=''
      for _ in 1 2 3; do state=$(poll); [ -n "$state" ] && break; sleep 2; done
      # Never start a second worker beside one we can't see: leave it, and let the wake window lapse.
      [ -n "$state" ] || { log "wake: $SBX does not answer; leaving it"; exit 1; }
      if [ "${state#* }" = running ]; then
        prized sandbox exec "$SBX" --timeout 30s -- "$STOP" >/dev/null
        log "stopped the worker still there"
      fi ;;
    1) fresh ;;
    *) exit 1 ;;
  esac
  printf '%s' "$CURSOR_AUTH_TOKEN" | prized sandbox cp - "$SBX:~/.cursor-worker/token"
  unset CURSOR_AUTH_TOKEN
  prized sandbox exec "$SBX" --timeout 60s -- 'chmod 600 ~/.cursor-worker/token ~/.cursor-worker/env'
  prized sandbox exec "$SBX" --timeout 60s -- "$START"
  NEW=0 STARTED=$SECONDS
  trap - EXIT
  log "worker started"
  drop

  # From here the sandbox is paused or deleted only once its worker has exited, and only by the
  # watcher of that start of the worker. A stopped watcher or failing polls leave it to pause by
  # itself, so an outage never deletes a live agent's sandbox.
  trap 'log "watcher stopped; $SBX pauses by itself"; drop; exit 0' TERM HUP INT
  while :; do
    sleep "$EVERY" || true  # a failed or killed sleep must not end the loop: only "exited" does
    if [ "$HIBERNATE" = 1 ]; then  # look before polling: a poll would resume a paused sandbox
      case $(state_of "$SBX") in
        paused | pausing) log "$SBX is paused; a wake resumes it"; exit 0 ;;
        gone) log "$SBX is gone"; exit 0 ;;
      esac
    fi
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
    # Again, under the lock: a wake may have started the worker again meanwhile.
    if [ "$HIBERNATE" = 1 ]; then st=$(state_of "$SBX"); else st=running; fi
    case $st in running) ;; paused | pausing | gone) log "$SBX is $st already"; drop; exit 0 ;; *) drop; continue ;; esac
    state=$(poll)
    [ "$state" = "$GEN exited" ] || { drop; continue; }
    code=$(prized sandbox exec "$SBX" --timeout 30s -- 'cat ~/.cursor-worker/done 2>/dev/null || echo killed') || code=unknown
    log "worker exited ($code)"
    prized sandbox cp "$SBX:~/.cursor-worker/worker.log" "$LOGS/$ID.worker.log" >/dev/null 2>&1 || true
    # Keep what the agent did: pause unless this is a new sandbox whose worker failed right away
    # (a refused token, a bad flag), which holds nothing worth keeping. A wake replaces the token.
    if [ "$HIBERNATE" = 1 ] && { [ "$code" = 0 ] || [ "$WOKE" = 1 ] || [ $((SECONDS - STARTED)) -ge "$GRACE" ]; }; then
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
