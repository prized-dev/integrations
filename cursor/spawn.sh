#!/usr/bin/env bash
# spawn.sh: run each Cursor Cloud Agent's tools in a Prized sandbox of its own.
#
# `agent worker controller --spawn ./spawn.sh --session-token --pool <name>` runs this once per
# claimed request. It creates a sandbox, starts `agent worker` there under the worker id Cursor
# assigned, and returns. A background watcher deletes the sandbox once the worker has exited, after
# its idle window. Needs `prized` (signed in, or PRIZED_API_KEY with Sandboxes: Write) and bash.
set -euo pipefail

TEMPLATE=${PRIZED_TEMPLATE:-prized-base-v1}
MAX_RUNTIME=${MAX_RUNTIME:-}               # the most a sandbox runs, then it is deleted; unset is Prized's default
IDLE=${CURSOR_IDLE_RELEASE:-600}           # seconds a worker waits for a follow-up, then exits
FLAGS=${CURSOR_WORKER_FLAGS---clone-git-repos}   # the `agent worker` flags beside --pool and --worker-id
LOGS=${PRIZED_CURSOR_LOGS:-${XDG_STATE_HOME:-$HOME/.local/state}/prized-cursor}  # a log per request

: "${CURSOR_AGENT_WORKER_ID:?run me with agent worker controller --spawn}" "${CURSOR_POOL:?}"
ID=$CURSOR_AGENT_WORKER_ID
log() { echo "[spawn] $ID: $*" >&2; }
[[ $ID =~ ^[A-Za-z0-9._-]+$ && $CURSOR_POOL =~ ^[A-Za-z0-9._-]+$ ]] || { log "unexpected worker id or pool"; exit 1; }
[[ $IDLE =~ ^[1-9][0-9]*$ ]] || { log "CURSOR_IDLE_RELEASE must be a number of seconds above 0"; exit 1; }
[[ $FLAGS =~ ^[A-Za-z0-9\ ._=/:,-]*$ ]] || { log "CURSOR_WORKER_FLAGS takes plain flags only, no quotes"; exit 1; }
if [ "$CURSOR_POOL" = default ] && [[ " $FLAGS " == *" --clone-git-repos "* ]]; then
  log "--clone-git-repos needs a named pool, not default"; exit 1
fi
# Released workers aren't kept, so a wake has nothing to resume: exiting lets Cursor queue the
# request again as a new claim. Keep the pool's reconnect window (workerReadyTimeoutSeconds) at 0.
if [ "${CURSOR_WAKE:-0}" = 1 ]; then log "wake: no sandbox kept for it"; exit 0; fi
# A session token reaches only this claim and ends with it. The service-account key stays here.
: "${CURSOR_AUTH_TOKEN:?start the controller with --session-token, so the service-account key never enters a sandbox}"
mkdir -p "$LOGS"

read -r -d '' INSTALL <<'EOF' || true
mkdir -p ~/work ~/.cursor-worker && chmod 700 ~/.cursor-worker
[ -x ~/.local/bin/agent ] || curl -fsS https://cursor.com/install | bash >/dev/null 2>&1
~/.local/bin/agent --version
EOF
# The worker runs detached in a process group of its own; its exit code lands in ~/.cursor-worker/done.
read -r -d '' START <<EOF || true
cd ~/work && rm -f ~/.cursor-worker/done && setsid -f sh -c '
  echo \$\$ > ~/.cursor-worker/pid
  . ~/.cursor-worker/env
  ~/.local/bin/agent worker --pool $CURSOR_POOL --worker-id $ID --auth-token-file ~/.cursor-worker/token \\
    --idle-release-timeout $IDLE $FLAGS start >> ~/.cursor-worker/worker.log 2>&1
  echo \$? > ~/.cursor-worker/done' < /dev/null > /dev/null 2>&1
EOF
# Exits 0 once the worker has exited, 3 while it runs.
POLL='[ -e ~/.cursor-worker/done ] || ! kill -0 "$(cat ~/.cursor-worker/pid 2>/dev/null)" 2>/dev/null || exit 3'

(
  # The watcher's polls keep the sandbox running. If they stop (this machine goes down), it pauses
  # 5 minutes after the last one and is deleted 10 minutes later.
  create=(--template "$TEMPLATE" --auto-pause 5m --auto-delete 10m --label "cursor-worker=$ID" --label "cursor-pool=$CURSOR_POOL")
  [[ ${CURSOR_REQUEST_ID:-} =~ ^[A-Za-z0-9._-]+$ ]] && create+=(--label "cursor-agent=$CURSOR_REQUEST_ID")
  [ -z "$MAX_RUNTIME" ] || create+=(--max-runtime "$MAX_RUNTIME")
  SBX=$(prized sandbox create "${create[@]}")
  log "created $SBX"
  remove() { if prized sandbox rm "$SBX" >/dev/null 2>&1; then log "deleted $SBX"; else log "could not delete $SBX; it pauses and is deleted by itself"; fi; }
  trap remove EXIT  # until the worker is up, a failed step deletes the sandbox

  prized sandbox exec "$SBX" --timeout 5m -- "$INSTALL"
  printf '%s' "$CURSOR_AUTH_TOKEN" | prized sandbox cp - "$SBX:~/.cursor-worker/token"
  unset CURSOR_AUTH_TOKEN
  NAME=$(printf '%s' "${CURSOR_WORKER_NAME:-prized-$ID}" | tr -cd 'A-Za-z0-9._-')
  printf 'export CURSOR_AGENT_WORKER_ID=%s CURSOR_WORKER_NAME=%s\n' "$ID" "${NAME:-prized-$ID}" |
    prized sandbox cp - "$SBX:~/.cursor-worker/env"
  prized sandbox exec "$SBX" -- 'chmod 600 ~/.cursor-worker/token ~/.cursor-worker/env'
  prized sandbox exec "$SBX" -- "$START"
  log "worker started"

  # From here the sandbox is deleted only once its worker has exited. A stopped watcher or failing
  # polls leave it to pause and delete itself, so an outage never deletes a live agent's sandbox.
  trap - EXIT
  trap 'log "watcher stopped; $SBX pauses and is deleted by itself"; exit 0' TERM HUP INT
  while sleep "${PRIZED_CURSOR_POLL:-30}"; do
    code=0
    prized sandbox exec "$SBX" --timeout 30s -- "$POLL" >/dev/null 2>&1 || code=$?
    [ "$code" = 3 ] && continue
    [ "$code" = 0 ] && break
    if prized sandbox get "$SBX" 2>&1 | grep -q "no sandbox"; then log "$SBX is gone"; exit 0; fi
  done
  log "worker exited ($(prized sandbox exec "$SBX" -- 'cat ~/.cursor-worker/done 2>/dev/null || echo killed'))"
  prized sandbox cp "$SBX:~/.cursor-worker/worker.log" "$LOGS/$ID.worker.log" >/dev/null 2>&1 || true
  remove
) >> "$LOGS/$ID.log" 2>&1 < /dev/null &

log "starting its sandbox; see $LOGS/$ID.log"
