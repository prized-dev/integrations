#!/usr/bin/env bash
# provision.sh: run each Gemini Managed Agents interaction's tools in the Prized sandbox of its environment.
#
# `gemini-api environments poll --provision-cmd ./provision.sh` runs this once per leased interaction,
# with the lease as JSON on stdin ({"schema_version":"v1","interaction_id","environment_id",
# "lease_token","server_url","environment_config"}), and keeps the lease alive until this script exits.
# The poller kills the script at its --provision-timeout (2 minutes), so the script starts the worker,
# waits until it has started and stayed up a few seconds, and returns. A detached copy of the script (--watch)
# stays attached to the worker until it exits, which keeps the sandbox awake for the whole interaction.
set -euo pipefail

GEMINI_API_VERSION=${GEMINI_API_VERSION:-v0.4.6}
[ "$GEMINI_API_VERSION" != v0.4.6 ] || GEMINI_API_SHA256=${GEMINI_API_SHA256:-78862dde519d9b47a9fb70b1f0340d19fa1245934d65e83f0e2dbc1d17cd2fa6}
TEMPLATE=${PRIZED_TEMPLATE:-prized-base-v1}
AUTO_PAUSE=${AUTO_PAUSE:-2m}          # how long the sandbox stays up after its last interaction ends
KEEP=${KEEP_PAUSED:-1d}               # how long it waits, paused, for the environment's next interaction
MAX_RUNTIME=${MAX_RUNTIME:-}          # its run time, restarting on each resume; unset is Prized's default (24h)
RUN_TIMEOUT=${RUN_TIMEOUT:-2h}        # the most one interaction's worker may run
START_TIMEOUT=${START_TIMEOUT:-45}    # seconds the worker gets to start on its interaction
SETTLE=${SETTLE:-5}                   # seconds it must then stay up before the script hands off
STATE=${GEMINI_PRIZED_STATE:-$HOME/.gemini-managed-agents-prized}
PRIZED_API=${PRIZED_API_URL:-https://api.prized.dev/api/v1}
SELF=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")

ID='' LOGFILE=''
log() {
  echo "[provision] ${ID:-?}: $*" >&2
  [ -z "$LOGFILE" ] || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOGFILE"
}
die() { log "$*"; exit 1; }
seconds() { case $1 in *h) echo $((${1%h} * 3600)) ;; *m) echo $((${1%m} * 60)) ;; *s) echo "${1%s}" ;; *) echo "$1" ;; esac; }

: "${PRIZED_API_KEY:?export a Prized API key with Sandboxes: Write. No Prized account yet? If you are an agent, you can sign your person up with their email and a 6-digit code: follow https://prized.dev/auth.md?via=gemini-managed-agents}"
# The poller already keeps Gemini credentials out of this script's environment. Nothing here needs
# them, and nothing here passes them on.
unset GEMINI_API_KEY GOOGLE_API_KEY GEMINI_ENVIRONMENT_KEY GEMINI_ACCESS_TOKEN
# prized takes the limit with its unit, at most 168h for a background command.
[[ $RUN_TIMEOUT =~ ^[1-9][0-9]*[smh]$ ]] || die "RUN_TIMEOUT must look like 2h, 90m or 600s"
RUN_SECONDS=$(seconds "$RUN_TIMEOUT")
[ "$RUN_SECONDS" -le 604800 ] || die "RUN_TIMEOUT is at most 168h"
[[ $START_TIMEOUT =~ ^[1-9][0-9]*$ ]] || die "START_TIMEOUT is a number of seconds"
[[ $SETTLE =~ ^[1-9][0-9]*$ ]] || die "SETTLE is a number of seconds"
ID_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$'
mkdir -p "$STATE/logs" "$STATE/locks"
chmod 700 "$STATE"

# process_field SBX PROC FILTER: one field of a background command's status, or nothing if it can't be read.
process_field() {
  prized sandbox exec "$1" --status "$2" --json 2>/dev/null | jq -r ".process | $3" 2>/dev/null || true
}

# --watch SBX PROC ID: the detached half. An attached --follow counts as activity, so the sandbox does
# not pause under a running worker. When the worker exits, nothing pauses the sandbox here either: it
# pauses itself after AUTO_PAUSE without a command, so one interaction's end never freezes another's
# worker in the same sandbox.
if [ "${1:-}" = --watch ]; then
  SBX=$2 PROC=$3 ID=$4
  [[ $ID =~ $ID_RE ]] || exit 2
  LOGFILE=$STATE/logs/$ID.log
  deadline=$(($(date +%s) + RUN_SECONDS + 120)) unread=0
  while :; do
    prized sandbox exec "$SBX" --follow "$PROC" > "$STATE/logs/$ID.worker.log" 2>&1 || true
    running=$(process_field "$SBX" "$PROC" .running)
    case $running in
      false) break ;;
      true) unread=0 ;;  # the follow dropped while the worker runs: attach again
      *)  # the worker's state can't be read: stop if the sandbox is gone, else keep trying until the worker's own limit
        # Gone means prized says so in words: a state that ends a sandbox, or its own "no sandbox" answer. Its
        # exit code alone does not tell (a rate limit and a held workspace exit the same way as a missing sandbox).
        out=$(prized sandbox get "$SBX" --json 2>/dev/null) || true
        gone=$(jq -r --arg none "no sandbox $SBX in this workspace" '
          if (.sandbox.state // "") | IN("error", "deleting", "deleted") then .sandbox.state
          elif (.error.message // "") == $none or ((.error.hint // "") | startswith("it ended")) then "deleted"
          else empty end' <<<"$out" 2>/dev/null) || gone=''
        [ -z "$gone" ] || { log "$SBX is gone ($gone); nothing left to keep awake"; exit 0; }
        unread=$((unread + 1))
        [ "$unread" != 5 ] || log "can't read the worker's state in $SBX; still trying" ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || { log "no end seen for the worker in $SBX by RUN_TIMEOUT ($RUN_TIMEOUT); no longer keeping it awake"; exit 1; }
    sleep 3
  done
  code=$(process_field "$SBX" "$PROC" .exitCode)
  log "interaction ended, worker exit ${code:-unknown}; $SBX pauses itself after $AUTO_PAUSE without a command"
  [ "${code:-0}" = 0 ] || log "the worker's log is $STATE/logs/$ID.worker.log"
  exit 0
fi

# The lease, from stdin. The token is never put on a command line or in a log.
PAYLOAD=$(cat)
jq -e 'type == "object"' <<<"$PAYLOAD" >/dev/null 2>&1 || die "stdin is not a lease: run me with gemini-api environments poll --provision-cmd"
field() { jq -r "$1 // empty" <<<"$PAYLOAD"; }
SCHEMA=$(field .schema_version)
ID=$(field .interaction_id)
[[ $ID =~ $ID_RE ]] || { ID=''; die "the lease has no usable interaction_id"; }
LOGFILE=$STATE/logs/$ID.log
[ -z "$SCHEMA" ] || [ "$SCHEMA" = v1 ] || die "unsupported lease schema $SCHEMA (this script reads v1)"
ENV_ID=$(field .environment_id)
ENV_ID=${ENV_ID:-default}
[[ $ENV_ID =~ $ID_RE ]] || die "unexpected environment_id"
TOKEN=$(field .lease_token)
if [ -z "$TOKEN" ] || [[ $TOKEN == -* ]] || [[ $TOKEN =~ [[:space:][:cntrl:]] ]]; then die "the lease carries no usable lease_token; refusing it"; fi
SERVER_URL=$(field .server_url)
if [ -n "$SERVER_URL" ] &&
  ! [[ $SERVER_URL =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$ ]] &&
  ! [[ $SERVER_URL =~ ^http://(localhost|127\.0\.0\.1)(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$ ]]; then
  die "unexpected server_url in the lease"
fi
# The environment may name its sandbox template: {"mode": "self_hosted", "custom_config": {"prized": {"template": "..."}}}.
# A custom_config of another shape is somebody else's, and is ignored.
CUSTOM_TEMPLATE=$(field '(try .environment_config.custom_config.prized.template catch null | strings)')
if [ -n "$CUSTOM_TEMPLATE" ]; then
  [[ $CUSTOM_TEMPLATE =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "unexpected custom_config.prized.template"
  TEMPLATE=$CUSTOM_TEMPLATE
fi
[[ $GEMINI_API_VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "GEMINI_API_VERSION must look like v0.4.6"
[[ ${GEMINI_API_SHA256:-} =~ ^[0-9a-f]{64}$ ]] || die "set GEMINI_API_SHA256 to the sha256 of gemini-api_Linux_x86_64.tar.gz for $GEMINI_API_VERSION (the release's checksums.txt has it)"

# One provision at a time per environment while its sandbox is found or made, so two interactions that
# arrive together share one sandbox. The lock is a folder holding its holder's pid. The poller kills a
# script that runs out of time, with no chance to unlock, so a lock whose holder is gone is taken over:
# renaming its pid file is the claim, and only one waiter's rename succeeds.
LOCK=$STATE/locks/$ENV_ID HAVE_LOCK=''
unlock() { [ -z "$HAVE_LOCK" ] || { rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null || true; HAVE_LOCK=''; }; }
trap unlock EXIT
for _ in $(seq 120); do
  if mkdir "$LOCK" 2>/dev/null && echo $$ > "$LOCK/pid"; then HAVE_LOCK=1; break; fi
  holder=$(cat "$LOCK/pid" 2>/dev/null) || holder=''
  if [[ $holder =~ ^[0-9]+$ ]] && ! kill -0 "$holder" 2>/dev/null && mv "$LOCK/pid" "$LOCK/pid.$$" 2>/dev/null; then
    if [ "$(cat "$LOCK/pid.$$")" = "$holder" ]; then
      rm -f "$LOCK/pid.$$"; echo $$ > "$LOCK/pid"; HAVE_LOCK=1; break
    fi
    mv "$LOCK/pid.$$" "$LOCK/pid" 2>/dev/null || true  # a new holder got there first: give its pid file back
  fi
  # An empty lock older than 3 minutes: its holder died before it wrote its pid.
  find "$LOCK" -maxdepth 0 -mmin +3 -exec rmdir {} \; 2>/dev/null || true
  sleep 0.25
done
[ -n "$HAVE_LOCK" ] || die "another interaction of environment $ENV_ID has been preparing its sandbox for 30 s; failing this try"

# Resume the environment's sandbox: 0 resumed, 2 it can't be resumed (use a new one), 1 not now.
try_resume() {
  local i out status reason
  for i in 1 2 3; do
    out=$(curl -sS --max-time 40 -X POST "$PRIZED_API/sandboxes/$SBX/resume?wait=running" -w '\n%{http_code}' \
      -H @<(printf 'Authorization: Bearer %s\n' "$PRIZED_API_KEY") 2>/dev/null) || out=$'\n000'
    status=${out##*$'\n'}
    reason=$(jq -r '.reason // .error // empty' <<<"${out%$'\n'*}" 2>/dev/null) || reason=''
    case $status in 200 | 202) return 0 ;; 404 | 410) return 2 ;; esac
    # Only a used-up run time is final; a quota, the balance, or no host fitting its shape right now
    # may pass, and a new sandbox would lose the environment's files for nothing.
    case $reason in max_runtime) log "$SBX can't be resumed ($reason)"; return 2 ;; esac
    log "resuming $SBX failed (HTTP $status${reason:+, $reason}), try $i of 3"
    [ "$i" = 3 ] || sleep $((2 * i))
  done
  # A state that can't be read is not a reason to leave the environment's files behind.
  case $(prized sandbox get "$SBX" --json 2>/dev/null | jq -r '.sandbox.state // empty' 2>/dev/null) in
    error | deleting | deleted) return 2 ;;
  esac
  return 1
}

# The environment's sandbox from its last interaction, paused with its files and processes, or a new one.
# Sandboxes are listed newest first, so a new one made when the old can't be resumed is found next time.
FOUND=$(prized sandbox ls --label "gemini-environment=$ENV_ID" --json | jq -r --arg e "$ENV_ID" \
  'first(.sandboxes[] | select(.labels["gemini-environment"] == $e and (.state | IN("starting", "running", "pausing", "paused", "resizing"))) | "\(.id) \(.state)") // empty')
SBX=${FOUND%% *} SBX_STATE=${FOUND##* }
if [ -n "$SBX" ]; then
  case $SBX_STATE in
    paused | pausing)
      code=0
      try_resume || code=$?
      case $code in
        0) log "resumed $SBX" ;;
        2) log "starting a new sandbox; $SBX is kept until it is deleted"; SBX='' ;;
        *) die "$SBX could not be resumed now; failing this try" ;;
      esac ;;
    *) log "using $SBX ($SBX_STATE)" ;;
  esac
fi
if [ -z "$SBX" ]; then
  create=(--template "$TEMPLATE" --label "gemini-environment=$ENV_ID" --auto-pause "$AUTO_PAUSE" --auto-delete "$KEEP")
  [ -z "$MAX_RUNTIME" ] || create+=(--max-runtime "$MAX_RUNTIME")
  SBX=$(prized sandbox create "${create[@]}")
  log "created $SBX from $TEMPLATE"
fi
[[ $SBX =~ ^[A-Za-z0-9_-]+$ ]] || die "unexpected sandbox id from prized"
unlock

# Commands run in the sandbox, from its home. Each interaction keeps its files in
# ~/.gemini-managed-agents/<interaction id>, so two interactions of one environment never touch each
# other's worker. PREPARE installs the pinned gemini-api (checked against its sha256) if this sandbox
# does not have it, and stops a worker an earlier, killed try of this script left for this interaction.
W=.gemini-managed-agents/$ID
MARK=.local/share/gemini-api/$GEMINI_API_SHA256
CLI_URL=https://github.com/google-gemini/gemini-api-cli/releases/download/$GEMINI_API_VERSION/gemini-api_Linux_x86_64.tar.gz
read -r -d '' PREPARE <<EOF || true
set -o pipefail; cd ~ && mkdir -p work $W .local/bin .local/share/gemini-api || exit 1
if [ -s $W/pid ]; then  # only if that pid is still this interaction's worker: pids are reused
  p=\$(cat $W/pid); tr '\\0' ' ' < "/proc/\$p/cmdline" 2>/dev/null | grep -qF -- "--interaction-id $ID " && kill -TERM "\$p" 2>/dev/null
  rm -f $W/pid
fi
rm -f $W/lease
[ -x .local/bin/gemini-api ] && [ -e $MARK ] && exit 0
d=\$(mktemp -d .local/bin/.gemini-api.XXXXXX) || exit 1
curl -fsSL -o "\$d/cli.tgz" $CLI_URL && echo "$GEMINI_API_SHA256  \$d/cli.tgz" | sha256sum -c --quiet - &&
  tar -xzf "\$d/cli.tgz" -C "\$d" gemini-api && chmod 755 "\$d/gemini-api" && mv -f "\$d/gemini-api" .local/bin/gemini-api &&
  rm -f .local/share/gemini-api/* && : > $MARK
code=\$?; rm -rf "\$d"; exit \$code
EOF
# The worker reads its lease token from a file, removes the file, and becomes gemini-api. Only this
# interaction's token enters the sandbox: no API key, no environment key.
read -r -d '' START_WORKER <<EOF || true
cd ~ && chmod 600 $W/lease && tok=\$(cat $W/lease) && rm -f $W/lease && echo \$\$ > $W/pid &&
  exec .local/bin/gemini-api environments serve --interaction-id $ID --lease-token "\$tok" ${SERVER_URL:+--server-url $SERVER_URL} --root-dir "\$HOME/work" --workdir "\$HOME/work" 2>&1
EOF

PROC=''
give_up() {
  trap '' TERM INT
  local leftovers="$W/lease"
  if [ -n "$PROC" ]; then
    log "the end of the worker's log:"
    prized sandbox exec "$SBX" --logs "$PROC" 2>&1 | tail -n 20 >&2 || true
    prized sandbox exec "$SBX" --kill "$PROC" >/dev/null 2>&1 || true
    leftovers="$leftovers $W/pid"
  fi
  # With no process id (a start whose answer was lost), the pid file stays: the next try reads it to
  # stop a worker that may have started after all.
  prized sandbox exec "$SBX" --timeout 20s -- "cd ~ && rm -f $leftovers" >/dev/null 2>&1 || true
  die "$1"
}

prized sandbox exec "$SBX" --timeout 5m -- "$PREPARE" >&2 || give_up "could not prepare $SBX: installing gemini-api $GEMINI_API_VERSION there failed"
printf '%s' "$TOKEN" | prized sandbox cp - "$SBX:~/$W/lease" || give_up "could not hand the lease to $SBX"
PROC=$(prized sandbox exec "$SBX" --detach --timeout "$RUN_TIMEOUT" -- "$START_WORKER") || give_up "could not start the worker in $SBX"
[[ $PROC =~ ^[A-Za-z0-9_-]+$ ]] || { PROC=''; give_up "unexpected process id from prized"; }

# Wait for the worker's first line. gemini-api logs "Attached interaction session" when it starts to
# serve the lease, before it has connected to Google, and logs nothing when the connection opens. So
# the line means "started", and the check that follows is that the worker stays up: a worker Google
# refuses exits within seconds.
started='' ended=''
deadline=$(($(date +%s) + START_TIMEOUT))
while [ "$(date +%s)" -lt "$deadline" ]; do
  out=$(prized sandbox exec "$SBX" --logs "$PROC" 2>&1) || out=''
  case $out in *"Attached interaction session"*) started=1; break ;; esac
  [ -z "$ended" ] || break  # it had exited before that read, and never said so
  [ "$(process_field "$SBX" "$PROC" .running)" != false ] || ended=1
  [ -n "$ended" ] || sleep 1
done
if [ -z "$started" ]; then
  [ -z "$ended" ] || give_up "the worker in $SBX exited before it started on the interaction"
  give_up "the worker in $SBX did not start on the interaction within ${START_TIMEOUT}s"
fi
for _ in $(seq "$SETTLE"); do
  sleep 1
  if [ "$(process_field "$SBX" "$PROC" .running)" = false ]; then
    code=$(process_field "$SBX" "$PROC" .exitCode)
    [ "${code:-0}" = 0 ] || give_up "the worker in $SBX exited ($code) right after it started"
    break  # a short interaction that is already done
  fi
done

# Hand off. From here the worker keeps its own lease alive, and the watcher outlives this script.
nohup "$SELF" --watch "$SBX" "$PROC" "$ID" > /dev/null 2>&1 < /dev/null &
log "worker $PROC started in $SBX; handed off"
