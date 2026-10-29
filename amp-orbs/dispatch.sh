#!/usr/bin/env bash
# dispatch.sh: run each Amp orb in a Prized sandbox of its own.
#
# `amp orb provider /path/to/dispatch.sh` runs this once per job, with the job in ORB_* variables
# (https://ampcode.com/docs/orbs/self-hosted#dispatch-contract). Each orb is one sandbox, found by its
# label amp-orb=$ORB_ID, so nothing is kept on this machine. A pause keeps the sandbox's memory and
# disk, so the orb's processes carry on when it wakes. Needs bash, curl, jq and PRIZED_API_KEY
# (Sandboxes: Write).
set -euo pipefail

API=${PRIZED_API_URL:-https://api.prized.dev/api/v1}
TEMPLATE=${PRIZED_TEMPLATE:-prized-base-v1}
LABELS=${PRIZED_ORB_LABELS:-}       # more labels for each orb's sandbox: team=web,env=dev
FORK_FROM=${PRIZED_ORB_FORK_FROM:-} # a paused sandbox set up for orbs: new orbs start as live copies of it
DEADLINE=$((SECONDS + ${PRIZED_ORB_DEADLINE:-270}))  # Amp gives a job 5 minutes

OP=${ORB_OPERATION:-}
ID=${ORB_ID:-}
log() { echo "[prized] $OP $ID: $*" >&2; }
fail() { log "$*"; exit 1; }
[[ $ID =~ ^[A-Za-z0-9._-]{1,200}$ ]] || fail "run me from amp orb provider (ORB_ID is missing or has unexpected characters)"
[ -n "${PRIZED_API_KEY:-}" ] || fail "PRIZED_API_KEY is not set: make a key with Sandboxes: Write at https://prized.dev/dashboard/workspace/api-keys?via=amp-orbs"
# Each orb's shape (unset: the template's default), the minutes it may run from each wake (unset:
# Prized's default), and the minutes it may stay paused before it is deleted (unset: kept until Amp
# deletes it).
for n in PRIZED_ORB_VCPU PRIZED_ORB_MEMORY_GB PRIZED_ORB_DISK_GB PRIZED_ORB_MAX_RUNTIME_MIN PRIZED_ORB_AUTO_DELETE_MIN; do
  [[ -z ${!n:-} || ${!n} =~ ^[1-9][0-9]*$ ]] || fail "$n must be a whole number above 0"
done
[[ -z $FORK_FROM || $FORK_FROM =~ ^sbx_[A-Za-z0-9]+$ ]] || fail "PRIZED_ORB_FORK_FROM takes a sandbox id (sbx_…)"
[[ -z $LABELS || $LABELS =~ ^[A-Za-z0-9._-]+=[A-Za-z0-9._-]*(,[A-Za-z0-9._-]+=[A-Za-z0-9._-]*)*$ ]] ||
  fail "PRIZED_ORB_LABELS takes key=value pairs separated by commas"
RESOURCES=$(jq -nc --arg v "${PRIZED_ORB_VCPU:-}" --arg m "${PRIZED_ORB_MEMORY_GB:-}" --arg d "${PRIZED_ORB_DISK_GB:-}" \
  '{vcpu: $v, memoryMb: $m, diskGb: $d} | with_entries(select(.value != "") | .value |= tonumber)
   | if has("memoryMb") then .memoryMb *= 1024 else . end')
LABELS_JSON=$(jq -nc --arg id "$ID" --arg l "$LABELS" '$l | split(",") | map(select(. != "") | split("=")
  | {key: .[0], value: (.[1:] | join("="))}) | from_entries + {"amp-orb": $id}')
SELECT="label=amp-orb%3D$ID"
JOB=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')  # this job's Idempotency-Keys
TOOLBOX=
HDRS=$(mktemp)
ERRS=$(mktemp)
trap 'rm -f "$HDRS" "$ERRS"' EXIT

now_ms() { local t=${EPOCHREALTIME:-}; t=${t//[!0-9]/}; if [ -n "$t" ]; then echo $((10#$t / 1000)); else echo $(($(date +%s) * 1000)); fi; }
since() { local d=$(($(now_ms) - $1)); printf '%d.%01ds' $((d / 1000)) $((d % 1000 / 100)); }

# [IDEM=key] [MAXT=seconds] api METHOD URL [-]: one Prized API call, with the request body on stdin
# when the last argument is "-" (so a token in it never reaches a command line). Sets CODE (the HTTP
# status; 000 when no answer came, timeout when the job is out of time, bad for a 2xx that isn't
# JSON) and BODY. Waits only as long as the job has left. Retried after Retry-After: the answers
# that say nothing happened (rate_limited, sandbox_capacity, sandbox_not_ready); and a lost answer
# or a gateway error, but only for a GET or a create or fork that carries an Idempotency-Key, since
# anything else may already have happened.
api() {
  local method=$1 url=$2 body='' data=() idem=() out wait left max=${MAXT:-60} again
  if [ "${3:-}" = - ]; then body=$(cat); data=(--data-binary @-); fi
  [ -z "${IDEM:-}" ] || idem=(-H "Idempotency-Key: $IDEM")
  while :; do
    left=$((DEADLINE - SECONDS))
    if [ "$left" -le 0 ]; then CODE=timeout BODY=; return 0; fi
    [ "$max" -le "$left" ] || max=$left
    out=$(printf '%s' "$body" | curl -sS -X "$method" "$url" --max-time "$max" -D "$HDRS" -w '\n%{http_code}' \
      -H @<(printf 'Authorization: Bearer %s\n' "$PRIZED_API_KEY") -H 'Content-Type: application/json' \
      ${idem[@]+"${idem[@]}"} ${data[@]+"${data[@]}"} 2>"$ERRS") || out=$'\n000'
    CODE=${out##*$'\n'} BODY=${out%$'\n'*}
    again=no
    case $CODE in
      429 | 503)
        case $(jq -r '.error? // empty' <<<"$BODY" 2>/dev/null || true) in
          rate_limited | sandbox_capacity | sandbox_not_ready) again=yes ;;
        esac ;;
      000 | 502 | 504) if [ "$method" = GET ] || [ ${#idem[@]} -gt 0 ]; then again=yes; fi ;;
    esac
    if [ "$again" = no ]; then
      if [[ $CODE == 2* && -n $BODY ]] && ! jq -e . >/dev/null 2>&1 <<<"$BODY"; then CODE=bad; fi
      return 0
    fi
    wait=$(tr -d '\r' < "$HDRS" | awk -F': *' 'tolower($1) == "retry-after" { print $2 + 0 }' | tail -n 1)
    [[ $wait =~ ^[0-9]+$ ]] && [ "$wait" -gt 0 ] || wait=1
    [ $((SECONDS + wait)) -lt "$DEADLINE" ] || return 0
    sleep "$wait"
  done
}
# need WHAT CODE...: stop unless the last call answered one of CODE, naming what went wrong.
need() {
  local what=$1 c; shift
  for c in "$@"; do [ "$CODE" = "$c" ] && return 0; done
  case $CODE in
    000) fail "$what: no answer ($(tr -d '\r\n' < "$ERRS" | head -c 300))" ;;
    timeout) fail "$what: out of time for this job" ;;
    bad) fail "$what: the answer is not JSON: $(head -c 200 <<<"$BODY" | tr -d '\n')" ;;
  esac
  fail "$what: $CODE $(jq -r '[.error, .reason, .message] | map(select(. != null)) | join(": ")' <<<"$BODY" 2>/dev/null || true)"
}
# rm_sbx ID: delete one sandbox.
rm_sbx() {
  api DELETE "$API/sandboxes/$1"
  need "delete $1" 200 202 404
}

# find_orb: MATCHES, this orb's sandboxes (oldest first), and SBX and STATE of the one to use: a
# running one first, then starting, then paused, then one in error. Both empty when there is none.
find_orb() {
  api GET "$API/sandboxes?$SELECT"
  need "list sandboxes" 200
  MATCHES=$(jq -c '[.sandboxes[] | select(.state != "deleting" and .state != "deleted")
    | {id, state, createdAt, errorReason}] | sort_by(.createdAt, .id)' <<<"$BODY")
  read -r SBX STATE < <(jq -r 'sort_by(if .state == "running" then 0 elif .state == "starting" then 1
    elif .state == "error" then 3 else 2 end) | first // {} | "\(.id // "") \(.state // "")"' <<<"$MATCHES")
  TOOLBOX=
}
# settle TARGET: wait until SBX is TARGET; stop if it fails or is deleted on the way.
settle() {
  while :; do
    api GET "$API/sandboxes/$SBX"
    [ "$CODE" != 404 ] || fail "$SBX was deleted while it was ${STATE:-starting}"
    need "read $SBX" 200
    STATE=$(jq -r .sandbox.state <<<"$BODY")
    [ "$STATE" != "$1" ] || return 0
    case $STATE in
      error) fail "$SBX went to error: $(jq -r '.sandbox.errorReason // "no reason given"' <<<"$BODY")" ;;
      deleting | deleted) fail "$SBX is being deleted" ;;
    esac
    [ "$SECONDS" -lt "$DEADLINE" ] || fail "$SBX is still $STATE, not $1"
    sleep 1
  done
}
# run CMD TIMEOUT [token]: run CMD in the sandbox (bash -c, as its user), with ORB_TOKEN and AMP_URL in
# its environment when the third argument says token; prints its output and returns its exit code.
run() {
  local rc
  if [ -z "$TOOLBOX" ]; then
    api GET "$API/sandboxes/$SBX"
    need "read $SBX" 200
    TOOLBOX=$(jq -r .sandbox.toolboxBase <<<"$BODY")
  fi
  MAXT=$(($2 + 15)) api POST "$TOOLBOX/process/execute" - < <(jq -n --arg command "$1" --argjson timeout "$2" --arg token "${3:-}" '{$command, $timeout}
    + if $token == "token" then {env: ({ORB_TOKEN: env.ORB_TOKEN, AMP_URL: (env.AMP_URL // "")} | with_entries(select(.value != "")))} else {} end')
  need "run a command in $SBX" 200
  jq -j '.stdout, .stderr' <<<"$BODY" >&2
  rc=$(jq -r '.exitCode // 1' <<<"$BODY")
  [[ $rc =~ ^[0-9]+$ ]] || rc=1
  return "$rc"
}

# In the sandbox: install the Amp CLI once (it updates itself after that), then start
# `amp orb bootstrap` detached, so this job returns while the orb connects to its thread.
# shellcheck disable=SC2016 # these run in the sandbox
INSTALL='set -o pipefail
export PATH="$HOME/.amp/bin:$HOME/.local/bin:$PATH"
if ! command -v amp >/dev/null 2>&1; then
  out=$( (curl -fsSL https://ampcode.com/install.sh | bash) 2>&1) || { printf "%s\n" "$out" | tail -n 20; exit 1; }
  command -v amp >/dev/null 2>&1 || { echo "amp is not on PATH after its installer ran"; exit 1; }
fi
echo "amp $(amp --version 2>/dev/null | head -n 1)"'
# shellcheck disable=SC2016
START='export PATH="$HOME/.amp/bin:$HOME/.local/bin:$PATH"
mkdir -p ~/.amp-orb && cd ~ && setsid -f bash -lc "exec amp orb bootstrap" </dev/null >>~/.amp-orb/bootstrap.log 2>&1
echo "amp orb bootstrap started; its log is ~/.amp-orb/bootstrap.log"'

# create_orb: a new sandbox for the orb in SBX and STATE (and what it came from in FROM): a live copy
# of PRIZED_ORB_FORK_FROM when that is set and its machine has room, otherwise one from the template.
create_orb() {
  local template=$TEMPLATE resources=$RESOURCES parent body
  FROM=$TEMPLATE
  if [ -n "$FORK_FROM" ]; then
    api GET "$API/sandboxes/$FORK_FROM"
    need "read PRIZED_ORB_FORK_FROM $FORK_FROM" 200
    parent=$BODY
    # A copy would carry that orb's running bootstrap, with its thread's token.
    jq -e '.sandbox.labels["amp-orb"] == null' <<<"$parent" >/dev/null ||
      fail "PRIZED_ORB_FORK_FROM $FORK_FROM is an orb's own sandbox: set up a template sandbox of its own"
    # Copies keep the parent's auto-pause, which counts only API calls, not the agent's work.
    [ "$(jq -r '.sandbox.autoPauseMin // 0' <<<"$parent")" = 0 ] ||
      fail "$FORK_FROM has auto-pause on, so its copies would pause while the agent works: make it again without --auto-pause"
    IDEM="amp-orb-$ID-$JOB-fork" api POST "$API/sandboxes/$FORK_FROM/fork" - < <(jq -n --argjson labels "$LABELS_JSON" \
      --arg rt "${PRIZED_ORB_MAX_RUNTIME_MIN:-}" '{count: 1, $labels} + (if $rt != "" then {maxRuntimeMin: ($rt | tonumber)} else {} end)')
    if [ "$CODE" = 409 ] && [[ $(jq -r '.error' <<<"$BODY") =~ ^(fork_capacity|fork_unsupported_host|parent_not_running)$ ]]; then
      # The parent's machine can't take a copy now: start from the parent's template and shape instead.
      template=$(jq -r .sandbox.template <<<"$parent")
      [ "$RESOURCES" != '{}' ] || resources=$(jq -c '.sandbox.resources // {}' <<<"$parent")
      log "could not fork $FORK_FROM ($(jq -r '[.error, .reason] | map(select(. != null)) | join(": ")' <<<"$BODY")); creating the orb from $template instead"
      FROM=$template
    else
      need "fork $FORK_FROM" 200 202
      SBX=$(jq -r '.sandboxes[0].id' <<<"$BODY") STATE=$(jq -r '.sandboxes[0].state' <<<"$BODY")
      TOOLBOX=$(jq -r '.sandboxes[0].toolboxBase // empty' <<<"$BODY")
      FROM="a copy of $FORK_FROM"
      return 0
    fi
  fi
  body=$(jq -n --arg t "$template" --argjson labels "$LABELS_JSON" --argjson r "$resources" \
    --arg rt "${PRIZED_ORB_MAX_RUNTIME_MIN:-}" --arg ad "${PRIZED_ORB_AUTO_DELETE_MIN:-}" '{template: $t, $labels}
    + (if $r == {} then {} else {resources: $r} end)
    + (if $rt != "" then {maxRuntimeMin: ($rt | tonumber)} else {} end)
    + (if $ad != "" then {autoDeleteMin: ($ad | tonumber)} else {} end)')
  IDEM="amp-orb-$ID-$JOB-create" api POST "$API/sandboxes?wait=running" - < <(printf '%s' "$body")
  need "create a sandbox" 200 201
  SBX=$(jq -r .sandbox.id <<<"$BODY") STATE=$(jq -r .sandbox.state <<<"$BODY")
  TOOLBOX=$(jq -r '.sandbox.toolboxBase // empty' <<<"$BODY")
}

spawn() {
  local t0 mine toolbox oldest how reason until
  t0=$(now_ms)
  [ -n "${ORB_TOKEN:-}" ] || fail "no ORB_TOKEN in the job"
  find_orb
  if [ "$STATE" = error ]; then
    reason=$(jq -r --arg id "$SBX" '.[] | select(.id == $id) | .errorReason // ""' <<<"$MATCHES")
    if [[ $reason == runtime_degraded* ]]; then
      # It may still be running, and comes back by itself if it answers again.
      log "$SBX is degraded; waiting up to 30 s for it to answer again"
      until=$((SECONDS + 30))
      while [ "$STATE" = error ] && [ "$SECONDS" -lt "$until" ] && [ "$SECONDS" -lt "$DEADLINE" ]; do
        sleep 2
        api GET "$API/sandboxes/$SBX"
        need "read $SBX" 200 404
        if [ "$CODE" = 404 ]; then STATE=deleted; else STATE=$(jq -r .sandbox.state <<<"$BODY"); fi
      done
    fi
    if [ "$STATE" = error ] || [ "$STATE" = deleted ]; then
      log "$SBX is in error (${reason:-no reason given}); deleting it and starting the orb afresh (Amp sets the repository up again)"
      [ "$STATE" = deleted ] || rm_sbx "$SBX"
      SBX='' STATE=''
    fi
  fi
  how=resumed
  if [ -z "$STATE" ]; then
    create_orb
    how=created mine=$SBX toolbox=$TOOLBOX
    # Two jobs for one orb at once can each make a sandbox. Every job keeps the oldest, so they
    # all end up on the same one.
    find_orb
    oldest=$(jq -r 'map(select(.state != "error")) | first.id // empty' <<<"$MATCHES")
    if [ -n "$oldest" ] && [ "$oldest" != "$mine" ]; then
      log "$oldest was made for this orb by another job first; deleting $mine and using $oldest"
      rm_sbx "$mine"
      SBX=$oldest how=joined
    else
      SBX=$mine TOOLBOX=$toolbox
    fi
    STATE=$(jq -r --arg id "$SBX" '(map(select(.id == $id)) | first.state) // "starting"' <<<"$MATCHES")
  fi
  case $STATE in
    running) [ "$how" != resumed ] || how="found running" ;;
    starting) settle running ;;
    resizing | pausing | paused)
      [ "$STATE" != resizing ] || settle paused
      api POST "$API/sandboxes/$SBX/resume?wait=running"
      need "resume $SBX" 200 202
      STATE=$(jq -r .sandbox.state <<<"$BODY") TOOLBOX=$(jq -r '.sandbox.toolboxBase // empty' <<<"$BODY")
      [ "$STATE" = running ] || settle running ;;
    *) fail "$SBX is $STATE" ;;
  esac
  case $how in
    created) log "created $SBX from $FROM in $(since "$t0")" ;;
    resumed) log "resumed $SBX in $(since "$t0"), with its memory and processes" ;;
    joined) log "$SBX is running" ;;
    *) log "$SBX is already running" ;;
  esac
  run "$INSTALL" 240 || fail "could not install the Amp CLI in $SBX"
  run "$START" 60 token || fail "could not start amp orb bootstrap in $SBX"
  log "ready in $(since "$t0")"
}

pause() {
  local t0 id state reason n=0
  t0=$(now_ms)
  [ -z "${ORB_SNAPSHOT_ID:-}" ] || log "project snapshots are not supported yet: pausing without saving one"
  find_orb
  [ "$MATCHES" != '[]' ] || { log "no sandbox for this orb, nothing to pause"; return 0; }
  # Every sandbox of this orb, in case two jobs ever made two.
  while read -r id state reason; do
    SBX=$id STATE=$state BODY=
    case $state in
      paused) log "$SBX is already paused"; continue ;;
      error)
        if [[ $reason == runtime_degraded* ]]; then
          api POST "$API/sandboxes/$SBX/pause?wait=paused"
          [ "$CODE" != 409 ] || { log "$SBX is degraded and can't pause; deleting it"; rm_sbx "$SBX"; continue; }
          need "pause $SBX" 200 202
        else
          log "$SBX is in error (${reason:-no reason given}); deleting it (the next spawn starts the orb afresh)"
          rm_sbx "$SBX"
          continue
        fi ;;
      pausing | resizing) ;;
      running | starting)
        api POST "$API/sandboxes/$SBX/pause?wait=paused"
        need "pause $SBX" 200 202 ;;
      *) fail "$SBX is $state" ;;
    esac
    [ "$(jq -r '.sandbox.state? // empty' <<<"$BODY")" = paused ] || settle paused
    n=$((n + 1))
    log "paused $SBX in $(since "$t0"), keeping its memory and disk"
  done < <(jq -r '.[] | "\(.id) \(.state) \(.errorReason // "")"' <<<"$MATCHES")
}

delete() {
  local n=0
  for _ in 1 2 3 4 5 6 7 8; do
    api DELETE "$API/sandboxes?$SELECT"
    need "delete sandboxes" 200 202
    n=$((n + $(jq -r '.count // 0' <<<"$BODY")))
    [ "$(jq -r '.more // false' <<<"$BODY")" = true ] || break
  done
  log "deleting $n sandbox(es)"
}

case $OP in
  spawn) spawn ;;
  pause) pause ;;
  delete) delete ;;
  *) fail "unknown ORB_OPERATION '$OP'" ;;
esac
