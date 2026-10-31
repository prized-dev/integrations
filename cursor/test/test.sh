#!/usr/bin/env bash
# check() evals its single-quoted condition later, so variables set here are used there.
# shellcheck disable=SC2016,SC2034
# Offline tests for spawn.sh and reap.sh. `prized` is a fake whose sandboxes are directories
# (fake-prized), and Cursor's installer and `agent` CLI are stubs (sandbox-bin/curl, stub-agent). No
# network, no keys.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SPAWN=$HERE/../spawn.sh
REAP=$HERE/../reap.sh
T=$(mktemp -d)
# kill_sandboxes: end every process a fake sandbox under $T started (a paused one is stopped, so
# CONT after KILL lets it die).
kill_sandboxes() {
  local e p
  for e in /proc/[0-9]*/environ; do
    if { tr '\0' '\n' < "$e"; } 2>/dev/null | grep -q "^FAKE_SANDBOX=$T/"; then
      p=${e#/proc/}; p=${p%/environ}; kill -KILL "$p" 2>/dev/null || true; kill -CONT "$p" 2>/dev/null || true
    fi
  done
}
trap 'pkill -f "$T/" 2>/dev/null || true; pkill -f "$SPAWN" 2>/dev/null || true; kill_sandboxes; rm -rf "$T"' EXIT
mkdir -p "$T/bin"
ln -s "$HERE/fake-prized" "$T/bin/prized"
export PATH="$T/bin:$PATH" STUB_AGENT_SRC="$HERE/stub-agent"
TOKEN=test-session-token-0123456789 TOKEN2=test-wake-token-9876543210
SVC_KEY=svc-key-never-in-a-sandbox PRIZED_KEY=dcp_never-in-a-sandbox
ID=0b5e2f4a-1c2d-4e5f-8a9b-0c1d2e3f4a5b
FAILS=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; FAILS=$((FAILS + 1)); fi; }

# run NAME [VAR=value...]: run spawn.sh as the controller would, from $T/NAME, against the fake Prized
# there (fresh the first time NAME is used, the same one after). Later VAR=value pairs win.
run() {
  local name=$1; shift
  C=$T/$name; mkdir -p "$C/root"
  export FAKE_PRIZED_ROOT=$C/root
  local start=$SECONDS code=0
  (cd "$C" && env CURSOR_API_KEY=$SVC_KEY PRIZED_API_KEY=$PRIZED_KEY CURSOR_AGENT_WORKER_ID=$ID CURSOR_POOL=prized CURSOR_REQUEST_ID=bc-1234 \
    CURSOR_WORKER_NAME=worker-1 CURSOR_AUTH_TOKEN=$TOKEN PRIZED_CURSOR_LOGS="$C/cursor-workers" PRIZED_CURSOR_POLL=0.2 \
    "$@" "$SPAWN") > "$C/out" 2>&1 || code=$?
  RETURNED=$((SECONDS - start)) CODE=$code
  find_sbx
}
# wake NAME [VAR=value...]: a follow-up for the same worker id after it released (CURSOR_WAKE=1),
# with the fresh session token the controller mints for a wake.
wake() { local name=$1; shift; run "$name" CURSOR_WAKE=1 CURSOR_AUTH_TOKEN=$TOKEN2 "$@"; }
# find_sbx: the first sandbox made in this fake Prized, if any, and its home.
find_sbx() { SBX=$(find "$C/root" -maxdepth 1 -name 'sbx_*' -printf '%f\n' 2>/dev/null | sort | head -1 || true); H=$C/root/$SBX/home; }
# wait_log PATTERN [N]: wait up to 30 s for the logs to say PATTERN (N times; default once).
wait_log() {
  local i
  for i in $(seq 150); do
    [ "$(cat "$C"/cursor-workers/*.log 2>/dev/null | grep -c -- "$1")" -ge "${2:-1}" ] && { find_sbx; return 0; }
    sleep 0.2
  done
  find_sbx; echo "     (timed out waiting for: $1 x${2:-1})"
}
meta() { jq -r "$1" "$C/root/${2:-$SBX}/meta.json"; }
state() { meta .state "$@"; }
sbx_count() { find "$C/root" -maxdepth 1 -name 'sbx_*' | wc -l; }
dev() { cat "$H/work/devserver.state" 2>/dev/null; }    # "pid count" from the server the stub left running
proc_state() { awk '{print $3}' "/proc/$1/stat" 2>/dev/null; }  # T while stopped (paused)
sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
wait_for() { local i; for i in $(seq 100); do eval "$1" && return 0; sleep 0.1; done; return 1; }

echo "# a claim: one sandbox, the worker started under Cursor's ids, paused once the worker releases"
run claim STUB_RUN=4
check "returns at once, exit 0" '[ "$CODE" = 0 ] && [ "$RETURNED" -le 2 ]'
wait_log "worker started"
wait_for '[ -s "$H/.cursor-worker/token-mode" ]' || true
check "one sandbox, labelled with the worker, pool and agent" \
  '[ "$(sbx_count)" = 1 ] && jq -e ".labels == {\"cursor-worker\": \"$ID\", \"cursor-pool\": \"prized\", \"cursor-agent\": \"bc-1234\"}" "$C/root/$SBX/meta.json" >/dev/null'
check "pauses itself 5 min after its last command; deleted after 3 days paused" \
  'jq -e ".flags[\"auto-pause\"] == \"5m\" and .flags[\"auto-delete\"] == \"3d\" and .flags.template == \"prized-base-v1\"" "$C/root/$SBX/meta.json" >/dev/null'
check "token reaches the sandbox only as a 0600 file" '[ "$(cat "$H/.cursor-worker/token")" = "$TOKEN" ] && [ "$(cat "$H/.cursor-worker/token-mode")" = 600 ]'
check "token is never on a command line or in a log" '! grep -rq "$TOKEN" "$C/root/calls.log" "$C/out" "$C/cursor-workers"'
check "worker started as Cursor asked" '[ "$(tr "\n" " " < "$H/.cursor-worker/argv")" = "worker --pool prized --worker-id $ID --auth-token-file $H/.cursor-worker/token --idle-release-timeout 600 --clone-git-repos start " ] || { echo "     got: $(tr "\n" " " < "$H/.cursor-worker/argv")"; false; }'
check "no API key reaches the sandbox, its files or a prized call" '! grep -rqa -e "$SVC_KEY" -e "$PRIZED_KEY" "$C/root"'
check "worker sees its id and name, never an API key" 'grep -qx "CURSOR_AGENT_WORKER_ID=$ID" "$H/.cursor-worker/seen-env" && grep -qx "CURSOR_WORKER_NAME=worker-1" "$H/.cursor-worker/seen-env" && grep -qx "CURSOR_API_KEY=" "$H/.cursor-worker/seen-env" && grep -qx "PWD=$H/work" "$H/.cursor-worker/seen-env"'
check "sandbox running while the worker serves" '[ "$(state)" = running ]'
wait_log "paused $SBX"
check "paused, not deleted, once the worker releases (exit 0)" '[ "$(state)" = paused ] && [ "$(meta .pauses)" = 1 ] && grep -q "worker exited (0)" "$C/cursor-workers/$ID.log"'
check "the paused sandbox keeps no token" '[ ! -e "$H/.cursor-worker/token" ]'
check "worker's log saved beside the request's" 'grep -q "Worker is now running" "$C/cursor-workers/$ID.worker.log"'
check "no lock left behind" '[ ! -e "$C/cursor-workers/$ID.lock" ]'

echo "# a follow-up after the release: the wake resumes the same sandbox, its server still running"
run hib STUB_RUN=1 STUB_DEVSERVER=1
wait_log "paused $SBX"
read -r DEVPID N1 <<< "$(dev)"
check "the server the session started is frozen by the pause, not killed" '[ -n "$DEVPID" ] && [ "$(proc_state "$DEVPID")" = T ]'
sleep 0.5
check "nothing runs while paused (its count stands still)" '[ "$(dev)" = "$DEVPID $N1" ]'
wake hib STUB_RUN=1 STUB_DEVSERVER=1
check "the wake returns at once, exit 0" '[ "$CODE" = 0 ] && [ "$RETURNED" -le 2 ]'
wait_log "worker started" 2
check "the same sandbox, resumed: no new one" '[ "$(sbx_count)" = 1 ] && [ "$(meta .resumes)" -ge 1 ] && grep -q "resumed $SBX" "$C/cursor-workers/$ID.log"'
wait_for '[ "$(cut -d" " -f2 "$H/work/devserver.state")" -gt "$N1" ]' || true
read -r PID2 N2 <<< "$(dev)"
check "the same server process carries on from its count (memory kept)" '[ "$PID2" = "$DEVPID" ] && [ "$N2" -gt "$N1" ] && [ "$N2" -lt $((N1 + 100)) ] && [ "$(proc_state "$DEVPID")" != T ]'
check "the session's files are there" 'grep -q "written by start 1" "$H/work/notes.txt" && grep -q "written by start 2" "$H/work/notes.txt"'
check "the worker started again under the same id, with the wake's token" '[ "$(wc -l < "$H/.cursor-worker/token-seen")" = 2 ] && [ "$(tail -1 "$H/.cursor-worker/token-seen")" = "$(sha "$TOKEN2")" ] && grep -qx -- "$ID" "$H/.cursor-worker/argv"'
check "the wake's token is never on a command line or in a log" '! grep -rq "$TOKEN2" "$C/root/calls.log" "$C/out" "$C/cursor-workers"'
wait_log "paused $SBX" 2
check "paused again when the worker releases again" '[ "$(state)" = paused ] && [ "$(meta .pauses)" = 2 ] && [ "$(proc_state "$DEVPID")" = T ]'

echo "# a wake while the worker still serves (a repeat): no second worker"
run repeat STUB_RUN=3
wait_log "worker started"
wake repeat STUB_RUN=3
wait_log "token replaced"
check "one worker start, the token replaced for its reconnect" '[ "$(wc -l < "$H/.cursor-worker/token-seen")" = 1 ] && [ "$(cat "$H/.cursor-worker/token")" = "$TOKEN2" ]'
wait_log "paused $SBX"
sleep 1
check "paused once, by one watcher" '[ "$(state)" = paused ] && [ "$(meta .pauses)" = 1 ] && [ "$(meta ".resumes // 0")" = 0 ]'

echo "# a wake with nothing kept starts a fresh sandbox under the claimed id"
wake nothing STUB_RUN=0
wait_log "worker started"
check "a fresh sandbox, labelled with the claimed worker id, started with the wake's token" '[ "$(sbx_count)" = 1 ] && [ "$(meta ".labels[\"cursor-worker\"]")" = "$ID" ] && [ "$(head -1 "$H/.cursor-worker/token-seen")" = "$(sha "$TOKEN2")" ] && grep -q "no sandbox kept" "$C/cursor-workers/$ID.log"'

echo "# a wake whose sandbox lost its state (error), or can't resume, gets a fresh one"
run lost STUB_RUN=0
wait_log "paused $SBX"
jq '.state = "error"' "$C/root/$SBX/meta.json" > "$T/m" && mv "$T/m" "$C/root/$SBX/meta.json"
wake lost STUB_RUN=0
wait_log "worker started" 2
check "the errored one deleted, a fresh one started" '[ "$(state)" = deleted ] && [ "$(sbx_count)" = 2 ] && [ "$(state sbx_0000000000000002)" != deleted ]'
run stuck STUB_RUN=0
wait_log "paused $SBX"
jq '.resume_fails = true' "$C/root/$SBX/meta.json" > "$T/m" && mv "$T/m" "$C/root/$SBX/meta.json"
wake stuck STUB_RUN=0
wait_log "worker started" 2
check "one that won't resume is left paused, and a fresh one started" '[ "$(state)" = paused ] && [ "$(sbx_count)" = 2 ] && grep -q "could not resume" "$C/cursor-workers/$ID.log"'

echo "# a wake waits for the lock a watcher holds, and breaks one left 10 minutes ago"
run locked STUB_RUN=0
wait_log "paused $SBX"
mkdir "$C/cursor-workers/$ID.lock"
wake locked STUB_RUN=0
sleep 1.5
check "no resume while the lock is held" '[ "$(state)" = paused ] && [ "$(meta ".resumes // 0")" = 0 ]'
rmdir "$C/cursor-workers/$ID.lock"
wait_log "worker started" 2
check "resumed once it is free" '[ "$(meta .resumes)" -ge 1 ]'
run stale STUB_RUN=0
wait_log "paused $SBX"
mkdir "$C/cursor-workers/$ID.lock" && touch -d '-11 minutes' "$C/cursor-workers/$ID.lock"
wake stale STUB_RUN=0
wait_log "worker started" 2
check "a stale lock is broken" '[ "$(meta .resumes)" -ge 1 ]'

echo "# out of room: the pool's longest-paused worker makes way for a new agent"
run room STUB_RUN=0 CURSOR_AGENT_WORKER_ID=w-old
wait_log "paused"
OLD=$SBX
run room STUB_RUN=0 CURSOR_AGENT_WORKER_ID=w-other CURSOR_POOL=other
wait_log "paused" 2
run room STUB_RUN=3 CURSOR_AGENT_WORKER_ID=w-new FAKE_QUOTA=2
wait_log "worker started" 3
check "the paused worker of this pool deleted, the new agent's sandbox made" '[ "$(state "$OLD")" = deleted ] && [ "$(state sbx_0000000000000002)" = paused ] && [ "$(meta ".labels[\"cursor-worker\"]" sbx_0000000000000003)" = w-new ] && grep -q "to make room" "$C/cursor-workers/w-new.log"'
run room2 STUB_RUN=10 CURSOR_AGENT_WORKER_ID=w-busy
wait_log "worker started"
run room2 STUB_RUN=0 CURSOR_AGENT_WORKER_ID=w-blocked FAKE_QUOTA=1
sleep 1.5
check "a running worker is never deleted for room: the new create fails" '[ "$(state)" = running ] && [ "$(sbx_count)" = 1 ] && grep -q "disk limit" "$C/cursor-workers/w-blocked.log"'
pkill -f "$SPAWN" || true

echo "# CURSOR_HIBERNATE=0: deleted on release, as before"
run delete STUB_RUN=0 CURSOR_HIBERNATE=0
wait_log "deleted"
check "deleted once the worker exits; deleted 10 min after a pause" '[ "$(state)" = deleted ] && [ "$(meta ".flags[\"auto-delete\"]")" = 10m ]'
wake delete-wake STUB_RUN=0 CURSOR_HIBERNATE=0
wait_log "worker started"
check "a wake starts a fresh sandbox" '[ "$(sbx_count)" = 1 ]'

echo "# a worker that fails is deleted"
run failing STUB_RUN=0 STUB_EXIT=3
wait_log "deleted"
check "deleted, exit code logged" '[ "$(state)" = deleted ] && grep -q "worker exited (3)" "$C/cursor-workers/$ID.log"'

echo "# a worker killed outright (no exit code written) is noticed and deleted"
run killed STUB_RUN=30
wait_log "worker started"
wait_for '[ -s "$H/.cursor-worker/pid" ]' || true
kill -KILL -- "-$(cat "$H/.cursor-worker/pid")"
wait_log "deleted"
check "deleted, logged as killed" '[ "$(state)" = deleted ] && grep -q "worker exited (killed)" "$C/cursor-workers/$ID.log"'

echo "# an outage while the worker serves never deletes or pauses its sandbox"
run outage STUB_RUN=6 FAKE_EXEC_FAIL="$T/outage-count"
wait_log "worker started"
echo 8 > "$T/outage-count"  # the next 8 polls fail
for i in $(seq 100); do [ "$(cat "$T/outage-count")" = 0 ] && break; sleep 0.1; done
check "eight failed polls in a row: still running" '[ "$(cat "$T/outage-count")" = 0 ] && [ "$(state)" = running ] && ! grep -q -e deleted -e paused "$C/cursor-workers/$ID.log"'
wait_log "paused $SBX"
check "paused once the worker releases after the outage" '[ "$(state)" = paused ] && grep -q "worker exited (0)" "$C/cursor-workers/$ID.log"'

echo "# a sandbox deleted under its worker: the watcher notices and stops"
run gone STUB_RUN=20
wait_log "worker started"
FAKE_PRIZED_ROOT=$C/root prized sandbox rm "$SBX"
wait_log "is gone"
for i in $(seq 25); do pgrep -f "$SPAWN" >/dev/null || break; sleep 0.2; done
check "logged as gone, watcher exited" 'grep -q "$SBX is gone" "$C/cursor-workers/$ID.log" && ! pgrep -f "$SPAWN" >/dev/null'

echo "# a stopped watcher leaves the sandbox to pause by itself"
run stopped STUB_RUN=20
wait_log "worker started"
pkill -TERM -f "$SPAWN" || true
wait_log "watcher stopped"
check "not deleted or paused by the stopped watcher" '[ "$(state)" = running ]'
kill_sandboxes

echo "# settings"
run settings STUB_RUN=0 PRIZED_TEMPLATE=my-template MAX_RUNTIME=2h CURSOR_IDLE_RELEASE=60 'CURSOR_WORKER_FLAGS=--connect-workspace' CURSOR_HIBERNATE_TTL=12h
wait_log "paused"
check "template, run time, idle window, flags and how long it stays paused" 'jq -e ".flags.template == \"my-template\" and .flags[\"max-runtime\"] == \"2h\" and .flags[\"auto-delete\"] == \"12h\"" "$C/root/$SBX/meta.json" >/dev/null && grep -qx -- "60" "$H/.cursor-worker/argv" && grep -qx -- "--connect-workspace" "$H/.cursor-worker/argv" && ! grep -qx -- "--clone-git-repos" "$H/.cursor-worker/argv"'

echo "# refusals: nothing is created"
run no-token CURSOR_AUTH_TOKEN=; sleep 1; find_sbx
check "no session token: refused, so the service-account key never enters a sandbox" '[ "$CODE" != 0 ] && [ -z "$SBX" ] && grep -q -- "--session-token" "$C/out"'
wake wake-no-token CURSOR_AUTH_TOKEN=; sleep 1; find_sbx
check "a wake without a session token: refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run bad-id 'CURSOR_AGENT_WORKER_ID=x;touch pwned'; sleep 1; find_sbx
check "an unexpected worker id: refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run bad-idle CURSOR_IDLE_RELEASE=10m; sleep 1; find_sbx
check "an idle window that isn't seconds: refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run idle-zero CURSOR_IDLE_RELEASE=0; sleep 1; find_sbx
check "an idle window of 0 (never release): refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run bad-poll PRIZED_CURSOR_POLL=abc; sleep 1; find_sbx
check "a poll interval that isn't seconds: refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run zero-poll PRIZED_CURSOR_POLL=0; sleep 1; find_sbx
check "a poll interval of 0: refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run quoted-flags "CURSOR_WORKER_FLAGS=--on-session-start 'git status'"; sleep 1; find_sbx
check "flags with quotes: refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run default-pool CURSOR_POOL=default; sleep 1; find_sbx
check "the default pool with --clone-git-repos: refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run bad-hibernate CURSOR_HIBERNATE=yes; sleep 1; find_sbx
check "CURSOR_HIBERNATE other than 0 or 1: refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run bad-ttl CURSOR_HIBERNATE_TTL=forever; sleep 1; find_sbx
check "a TTL that isn't a duration: refused" '[ "$CODE" != 0 ] && [ -z "$SBX" ]'
run odd-name STUB_RUN=0 'CURSOR_WORKER_NAME=$(touch pwned) name'
wait_log "paused"
check "a worker name is reduced to safe characters" 'grep -qx "CURSOR_WORKER_NAME=touchpwnedname" "$H/.cursor-worker/seen-env" && [ ! -e "$H/work/pwned" ] && [ ! -e "$H/pwned" ]'

echo "# reap.sh: paused workers past the cutoff are deleted, nothing else"
C=$T/reap; mkdir -p "$C/root" "$C/logs"; export FAKE_PRIZED_ROOT=$C/root
mk() {  # mk STATE DAYS LABEL...: a sandbox in the fake, in STATE, last used DAYS days ago
  local s=$1 d=$2 l sid a=(); shift 2
  for l in "$@"; do a+=(--label "$l"); done
  sid=$(prized sandbox create "${a[@]}")
  jq --arg s "$s" --arg t "$(date -u -d "-$d days" +%Y-%m-%dT%H:%M:%S.123456Z)" '.state = $s | .lastActiveAt = $t' \
    "$C/root/$sid/meta.json" > "$T/m" && mv "$T/m" "$C/root/$sid/meta.json"
  echo "$sid"
}
reap() { (cd "$C" && env PRIZED_CURSOR_LOGS="$C/logs" "$REAP" "$@") > "$C/out" 2>&1 || CODE=$?; }
P_OLD=$(mk paused 4 cursor-worker=w1 cursor-pool=prized)
P_NEW=$(mk paused 1 cursor-worker=w2 cursor-pool=prized)
R_OLD=$(mk running 5 cursor-worker=w3 cursor-pool=prized)
E_OLD=$(mk error 4 cursor-worker=w4 cursor-pool=prized)
O_OLD=$(mk paused 4 cursor-worker=w5 cursor-pool=other)
X_OLD=$(mk paused 9 job=ci)
L_OLD=$(mk paused 4 cursor-worker=w6 cursor-pool=prized)
CODE=0; reap --older-than 3d --pool prized --dry-run
check "--dry-run names the old paused and errored workers, deletes nothing" '[ "$CODE" = 0 ] && grep -q "would delete $P_OLD" "$C/out" && grep -q "would delete $E_OLD" "$C/out" && grep -q "3 to delete" "$C/out" && ! grep -q "$P_NEW" "$C/out" && [ "$(state "$P_OLD")" = paused ]'
mkdir "$C/logs/w6.lock"  # a wake for w6 is under way on this machine
CODE=0; reap --older-than 3d --pool prized
check "deletes paused and errored workers of the pool idle past the cutoff" '[ "$CODE" = 0 ] && [ "$(state "$P_OLD")" = deleted ] && [ "$(state "$E_OLD")" = deleted ] && grep -q "2 deleted" "$C/out"'
check "keeps newer ones, running ones, other pools, a worker being woken, and sandboxes it didn't make" '[ "$(state "$P_NEW")" = paused ] && [ "$(state "$R_OLD")" = running ] && [ "$(state "$O_OLD")" = paused ] && [ "$(state "$L_OLD")" = paused ] && [ "$(state "$X_OLD")" = paused ] && grep -q "skipped $L_OLD" "$C/out"'
rmdir "$C/logs/w6.lock"
CODE=0; reap --older-than 2d
check "without --pool, every pool's (still never other sandboxes)" '[ "$(state "$O_OLD")" = deleted ] && [ "$(state "$L_OLD")" = deleted ] && [ "$(state "$X_OLD")" = paused ] && [ "$(state "$P_NEW")" = paused ]'
CODE=0; reap --older-than forever
check "a cutoff that isn't a duration: refused" '[ "$CODE" = 2 ]'
CODE=0; reap --pool prized
check "no cutoff: refused" '[ "$CODE" = 2 ]'

echo
if [ "$FAILS" = 0 ]; then echo "all passed"; else echo "$FAILS failed"; exit 1; fi
