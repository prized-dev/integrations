#!/usr/bin/env bash
# on-work.sh: run each Claude Managed Agents session's tools in a Prized sandbox of its own.
#
# `ant beta:worker poll --on-work ./on-work.sh` runs this once per work item (one run of a session),
# with the session's ids in ANTHROPIC_* variables and the work item as JSON on stdin. It stays attached
# until the run ends, because the poller stops a work item as soon as this script returns. So one
# poller serves one session at a time: start one per session you want at once.
set -euo pipefail

ANT_VERSION=${ANT_VERSION:-2.3.0}
[ "$ANT_VERSION" != 2.3.0 ] || ANT_SHA256=${ANT_SHA256:-cd3fa57af3efbdbf68d569d0b921a69171b53be14cc5e013b35018ccb0f1d8ab}
TEMPLATE=${PRIZED_TEMPLATE:-prized-base-v1}
KEEP=${KEEP_PAUSED:-1d}           # how long a session's sandbox waits, paused, for the session's next message
MAX_RUNTIME=${MAX_RUNTIME:-}      # its run time, restarting on each resume; unset is Prized's default (24h)
RUN_TIMEOUT=${RUN_TIMEOUT:-2h}    # the most one run may take, e.g. a session left waiting on a tool approval
PRIZED_API=${PRIZED_API_URL:-https://api.prized.dev/api/v1}

: "${ANTHROPIC_SESSION_ID:?run me with ant beta:worker poll --on-work}" "${ANTHROPIC_WORK_ID:?}" "${ANTHROPIC_ENVIRONMENT_ID:?}"
: "${PRIZED_API_KEY:?export a Prized API key with Sandboxes: Write}"
: "${ANT_SHA256:?set ANT_SHA256 to the sha256 of ant_${ANT_VERSION}_linux_amd64.tar.gz}"
# This script never needs the environment key, which can claim any session in the environment.
unset ANTHROPIC_ENVIRONMENT_KEY
log() { echo "[on-work] $ANTHROPIC_SESSION_ID: $*" >&2; }
[[ $ANTHROPIC_WORK_ID =~ ^[A-Za-z0-9_-]+$ ]] || { log "unexpected work id $ANTHROPIC_WORK_ID"; exit 1; }
[[ $ANT_SHA256 =~ ^[0-9a-f]{64}$ ]] || { log "ANT_SHA256 is not a sha256"; exit 1; }
seconds() { case $1 in *h) echo $((${1%h} * 3600)) ;; *m) echo $((${1%m} * 60)) ;; *s) echo "${1%s}" ;; *) echo "$1" ;; esac; }
RUN_SECONDS=$(seconds "$RUN_TIMEOUT")
if ! [[ $RUN_SECONDS =~ ^[0-9]+$ ]] || [ "$RUN_SECONDS" -eq 0 ]; then log "RUN_TIMEOUT must look like 2h, 90m or 600s"; exit 1; fi

# Only the work item's secret enters the sandbox, and it reaches this one session.
SECRET=$(jq -r '.secret // empty')
if [ -z "$SECRET" ]; then log "work item $ANTHROPIC_WORK_ID carries no secret; refusing it"; exit 1; fi

# Commands run in the sandbox, from its home. Each work item keeps its files in ~/.anthropic/<work id>,
# so a poller that takes the session's next message never touches this run's worker. The worker runs
# detached, in a process group of its own (a command lasts an hour at most, a run can last longer).
W=.anthropic/$ANTHROPIC_WORK_ID
ANT_URL=https://github.com/anthropics/anthropic-cli/releases/download/v$ANT_VERSION/ant_${ANT_VERSION}_linux_amd64.tar.gz
read -r -d '' INSTALL_ANT <<EOF || true
set -o pipefail; cd ~ && .local/bin/ant --version 2>/dev/null | grep -qF ' $ANT_VERSION' && exit 0
mkdir -p .local/bin && d=\$(mktemp -d .local/bin/.ant.XXXXXX) || exit 1
curl -fsSL -o "\$d/ant.tgz" $ANT_URL && echo "$ANT_SHA256  \$d/ant.tgz" | sha256sum -c --quiet - &&
  tar -xzf "\$d/ant.tgz" -C "\$d" ant && chmod 755 "\$d/ant" && mv -f "\$d/ant" .local/bin/ant
code=\$?; rm -rf "\$d"; exit \$code
EOF
read -r -d '' START_WORKER <<EOF || true
cd ~ && chmod 600 $W/secret && rm -f $W/done $W/pid && setsid -f sh -c '
  echo \$\$ > $W/pid
  ANTHROPIC_WORK_SECRET_FILE=\$HOME/$W/secret .local/bin/ant beta:worker run --workdir "\$HOME/work" >> $W/worker.log 2>&1
  echo \$? > $W/done; rm -f $W/pid' < /dev/null > /dev/null 2>&1
for i in \$(seq 50); do [ -s $W/pid ] && exit 0; sleep 0.1; done; echo "the worker did not start" >&2; exit 1
EOF
# Exits 0 when the run is done, 86 if the worker is gone without finishing.
read -r -d '' WAIT_FOR_RUN <<EOF || true
cd ~ && while [ ! -e $W/done ]; do
  { [ -s $W/pid ] && kill -0 -- "-\$(cat $W/pid)" 2>/dev/null; } || [ -e $W/done ] || exit 86
  sleep 1
done
EOF
# Stops this work item's worker: SIGTERM, up to 15 s to finish (ant kills this script 30 s after its
# own SIGTERM), then SIGKILL. Removes the secret either way.
read -r -d '' STOP_OURS <<EOF || true
cd ~ && if [ -s $W/pid ]; then g=\$(cat $W/pid); kill -TERM -- "-\$g" 2>/dev/null
  for i in \$(seq 30); do kill -0 -- "-\$g" 2>/dev/null || break; sleep 0.5; done; kill -KILL -- "-\$g" 2>/dev/null; fi
rm -f $W/pid $W/secret
EOF

# However this ends, our worker stops. Nothing pauses the sandbox explicitly, so this poller can never
# freeze another poller's worker: it pauses itself after 2 minutes without a command, and a running
# command (our wait) keeps it awake.
SBX='' WAITER='' CLEANED=''
cleanup() {
  trap '' TERM INT
  [ -z "$CLEANED" ] || return 0
  CLEANED=1
  if [ -n "$WAITER" ]; then pkill -P "$WAITER" 2>/dev/null || true; kill "$WAITER" 2>/dev/null || true; fi
  [ -n "$SBX" ] || return 0
  prized sandbox exec "$SBX" --timeout 20s -- "$STOP_OURS" >/dev/null 2>&1 || log "could not reach $SBX to stop its worker"
  log "done with $SBX; it pauses itself after 2 idle minutes"
}
trap cleanup EXIT
# Ctrl-C reaches this script and ant at once, and ant then sends SIGTERM too. The first signal shuts
# out the rest and cleans up right there, so a second one can't land mid-cleanup and end it.
trap 'trap "" TERM INT; cleanup; exit 143' TERM INT

# Resume the session's sandbox: 0 resumed, 2 it can't be resumed (use a new one), 1 not now.
try_resume() {
  local i out status reason
  for i in 1 2 3; do
    out=$(curl -sS -X POST "$PRIZED_API/sandboxes/$SBX/resume?wait=running" -w '\n%{http_code}' \
      -H @<(printf 'Authorization: Bearer %s\n' "$PRIZED_API_KEY") 2>/dev/null) || out=$'\n000'
    status=${out##*$'\n'}
    reason=$(jq -r '.reason // .error // empty' <<<"${out%$'\n'*}" 2>/dev/null) || reason=''
    case $status in 200 | 202) return 0 ;; 404) return 2 ;; esac
    # Only a used-up run time is final; a quota, the balance, or no host fitting its shape right now
    # (invalid_resources) may pass, and a new sandbox would lose the session's files for nothing.
    case $reason in max_runtime) log "$SBX can't be resumed ($reason)"; return 2 ;; esac
    log "resuming $SBX failed (HTTP $status${reason:+, $reason}), try $i of 3"
    [ "$i" = 3 ] || sleep $((2 * i))
  done
  case $(prized sandbox get "$SBX" --json 2>/dev/null | jq -r '.sandbox.state' 2>/dev/null) in
    error | deleting | deleted | '') return 2 ;;
  esac
  return 1
}

# The session's sandbox from its last message, paused with its files and processes, or a new one.
# Sandboxes are listed newest first, so a new one made when the old can't be resumed is found next time.
SBX=$(prized sandbox ls --json | jq -r --arg s "$ANTHROPIC_SESSION_ID" \
  'first(.sandboxes[] | select(.labels["anthropic-session"] == $s and (.state | IN("starting", "running", "pausing", "paused"))) | .id) // empty')
if [ -n "$SBX" ]; then
  code=0
  try_resume || code=$?
  case $code in
    0) log "resumed $SBX" ;;
    2) log "starting a new sandbox; $SBX is kept until it is deleted"; SBX='' ;;
    *) log "$SBX could not be resumed now; failing this work item"; SBX=''; exit 1 ;;
  esac
fi
if [ -z "$SBX" ]; then
  create=(--template "$TEMPLATE" --label "anthropic-session=$ANTHROPIC_SESSION_ID" --auto-pause 2m --auto-delete "$KEEP")
  [ -z "$MAX_RUNTIME" ] || create+=(--max-runtime "$MAX_RUNTIME")
  SBX=$(prized sandbox create "${create[@]}")
  log "created $SBX"
fi

prized sandbox exec "$SBX" --timeout 5m -- "$INSTALL_ANT"
printf '%s' "$SECRET" | prized sandbox cp - "$SBX:~/$W/secret"
env_args=(--env "ANTHROPIC_SESSION_ID=$ANTHROPIC_SESSION_ID" --env "ANTHROPIC_WORK_ID=$ANTHROPIC_WORK_ID"
  --env "ANTHROPIC_ENVIRONMENT_ID=$ANTHROPIC_ENVIRONMENT_ID")
[ -z "${ANTHROPIC_BASE_URL:-}" ] || env_args+=(--env "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL")
prized sandbox exec "$SBX" "${env_args[@]}" -- "$START_WORKER"
log "worker started for $ANTHROPIC_WORK_ID"

# Wait for the run to end. On end_turn the worker exits a minute after the session goes idle; a
# session waiting on something else (a tool approval) keeps it running, so RUN_TIMEOUT caps the wait.
DEADLINE=$(($(date +%s) + RUN_SECONDS))
wait_for_run() {
  local failures=0 code left
  while :; do
    left=$((DEADLINE - $(date +%s)))
    if [ "$left" -le 0 ]; then log "the run passed RUN_TIMEOUT ($RUN_TIMEOUT); stopping its worker"; return 87; fi
    [ "$left" -le 3300 ] || left=3300
    code=0
    prized sandbox exec "$SBX" --timeout "${left}s" -- "$WAIT_FOR_RUN" || code=$?
    case $code in
      0) return 0 ;;
      86) log "the worker stopped without finishing; its log is ~/$W/worker.log in $SBX"; return 86 ;;
      124) failures=0 ;;  # the wait's own time limit: keep waiting
      *) failures=$((failures + 1)); [ "$failures" -lt 3 ] || return "$code"; sleep 5 ;;
    esac
  done
}
wait_for_run &
WAITER=$!
wait "$WAITER"
WAITER=''
exit_code=$(prized sandbox exec "$SBX" -- "cat ~/$W/done" 2>/dev/null | tr -cd 0-9) || exit_code=''
log "run ended, worker exit ${exit_code:-unknown}"
if [ "${exit_code:-0}" != 0 ]; then
  log "the end of the worker's log (~/$W/worker.log):"
  prized sandbox exec "$SBX" -- "tail -n 20 ~/$W/worker.log" >&2 2>/dev/null || true
fi
