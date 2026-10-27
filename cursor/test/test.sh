#!/usr/bin/env bash
# check() evals its single-quoted condition later, so variables set here are used there.
# shellcheck disable=SC2016,SC2034
# Offline tests for spawn.sh. `prized` is a fake whose sandboxes are directories (fake-prized), and
# Cursor's installer and `agent` CLI are stubs (sandbox-bin/curl, stub-agent). No network, no keys.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SPAWN=$HERE/../spawn.sh
T=$(mktemp -d)
trap 'pkill -f "$T/" 2>/dev/null || true; pkill -f "$SPAWN" 2>/dev/null || true; rm -rf "$T"' EXIT
mkdir -p "$T/bin"
ln -s "$HERE/fake-prized" "$T/bin/prized"
export PATH="$T/bin:$PATH" STUB_AGENT_SRC="$HERE/stub-agent"
TOKEN=test-session-token-0123456789
SVC_KEY=svc-key-never-in-a-sandbox PRIZED_KEY=dcp_never-in-a-sandbox
ID=0b5e2f4a-1c2d-4e5f-8a9b-0c1d2e3f4a5b
FAILS=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; FAILS=$((FAILS + 1)); fi; }

# run NAME [VAR=value...]: run spawn.sh as the controller would, in a fresh fake Prized, from $T/NAME.
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
# find_sbx: the sandbox this run created, if any, and its home.
find_sbx() { SBX=$(cd "$C/root" && ls -d sbx_* 2>/dev/null | head -1 || true); H=$C/root/$SBX/home; }
# wait_log PATTERN: wait up to 30 s for the background part's log to say PATTERN.
wait_log() { local i; for i in $(seq 150); do grep -q "$1" "$C"/cursor-workers/*.log 2>/dev/null && { find_sbx; return 0; }; sleep 0.2; done; find_sbx; echo "     (timed out waiting for: $1)"; }
state() { jq -r .state "$C/root/$SBX/meta.json"; }

echo "# a claim: one sandbox, the worker started under Cursor's ids, deleted after the worker exits"
run claim STUB_RUN=4
check "returns at once, exit 0" '[ "$CODE" = 0 ] && [ "$RETURNED" -le 2 ]'
wait_log "worker started"
for i in $(seq 50); do [ -s "$H/.cursor-worker/token-mode" ] && break; sleep 0.2; done
check "one sandbox, labelled with the worker, pool and agent" \
  '[ "$(ls -d "$C"/root/sbx_* | wc -l)" = 1 ] && jq -e ".labels == {\"cursor-worker\": \"$ID\", \"cursor-pool\": \"prized\", \"cursor-agent\": \"bc-1234\"}" "$C/root/$SBX/meta.json" >/dev/null'
check "pauses itself 5 min after its last command, deleted 10 min later" \
  'jq -e ".flags[\"auto-pause\"] == \"5m\" and .flags[\"auto-delete\"] == \"10m\" and .flags.template == \"prized-base-v1\"" "$C/root/$SBX/meta.json" >/dev/null'
check "token reaches the sandbox only as a 0600 file" '[ "$(cat "$H/.cursor-worker/token")" = "$TOKEN" ] && [ "$(cat "$H/.cursor-worker/token-mode")" = 600 ]'
check "token is never on a command line or in a log" '! grep -rq "$TOKEN" "$C/root/calls.log" "$C/out" "$C/cursor-workers"'
check "worker started as Cursor asked" '[ "$(tr "\n" " " < "$H/.cursor-worker/argv")" = "worker --pool prized --worker-id $ID --auth-token-file $H/.cursor-worker/token --idle-release-timeout 600 --clone-git-repos start " ] || { echo "     got: $(tr "\n" " " < "$H/.cursor-worker/argv")"; false; }'
check "no API key reaches the sandbox, its files or a prized call" '! grep -rqa -e "$SVC_KEY" -e "$PRIZED_KEY" "$C/root"'
check "worker sees its id and name, never an API key" 'grep -qx "CURSOR_AGENT_WORKER_ID=$ID" "$H/.cursor-worker/seen-env" && grep -qx "CURSOR_WORKER_NAME=worker-1" "$H/.cursor-worker/seen-env" && grep -qx "CURSOR_API_KEY=" "$H/.cursor-worker/seen-env" && grep -qx "PWD=$H/work" "$H/.cursor-worker/seen-env"'
check "sandbox running while the worker serves" '[ "$(state)" = running ]'
wait_log "deleted"
check "deleted once the worker exits" '[ "$(state)" = deleted ]'
check "worker's log saved beside the request's" 'grep -q "Worker is now running" "$C/cursor-workers/$ID.worker.log" && grep -q "worker exited (0)" "$C/cursor-workers/$ID.log"'

echo "# a worker that fails is cleaned up the same way"
run failing STUB_RUN=0 STUB_EXIT=3
wait_log "deleted"
check "deleted, exit code logged" '[ "$(state)" = deleted ] && grep -q "worker exited (3)" "$C/cursor-workers/$ID.log"'

echo "# a worker killed outright (no exit code written) is noticed and cleaned up"
run killed STUB_RUN=30
wait_log "worker started"
for i in $(seq 50); do [ -s "$H/.cursor-worker/pid" ] && break; sleep 0.2; done
kill -KILL -- "-$(cat "$H/.cursor-worker/pid")"
wait_log "deleted"
check "deleted, logged as killed" '[ "$(state)" = deleted ] && grep -q "worker exited (killed)" "$C/cursor-workers/$ID.log"'

echo "# an outage while the worker serves never deletes its sandbox"
run outage STUB_RUN=6 FAKE_EXEC_FAIL="$T/outage-count"
wait_log "worker started"
echo 8 > "$T/outage-count"  # the next 8 polls fail
for i in $(seq 100); do [ "$(cat "$T/outage-count")" = 0 ] && break; sleep 0.1; done
check "eight failed polls in a row: still running" '[ "$(cat "$T/outage-count")" = 0 ] && [ "$(state)" = running ] && ! grep -q deleted "$C/cursor-workers/$ID.log"'
wait_log "deleted"
check "deleted once the worker exits after the outage" '[ "$(state)" = deleted ] && grep -q "worker exited (0)" "$C/cursor-workers/$ID.log"'

echo "# a sandbox deleted under its worker: the watcher notices and stops"
run gone STUB_RUN=20
wait_log "worker started"
FAKE_PRIZED_ROOT=$C/root prized sandbox rm "$SBX"
wait_log "is gone"
for i in $(seq 25); do pgrep -f "$SPAWN" >/dev/null || break; sleep 0.2; done
check "logged as gone, watcher exited" 'grep -q "$SBX is gone" "$C/cursor-workers/$ID.log" && ! pgrep -f "$SPAWN" >/dev/null'

echo "# a stopped watcher leaves the sandbox to pause and delete itself"
run stopped STUB_RUN=20
wait_log "worker started"
pkill -TERM -f "$SPAWN" || true
wait_log "watcher stopped"
check "not deleted by the stopped watcher" '[ "$(state)" = running ]'

echo "# settings"
run settings STUB_RUN=0 PRIZED_TEMPLATE=my-template MAX_RUNTIME=2h CURSOR_IDLE_RELEASE=60 'CURSOR_WORKER_FLAGS=--connect-workspace'
wait_log "deleted"
check "template, run time, idle window and flags" 'jq -e ".flags.template == \"my-template\" and .flags[\"max-runtime\"] == \"2h\"" "$C/root/$SBX/meta.json" >/dev/null && grep -qx -- "60" "$H/.cursor-worker/argv" && grep -qx -- "--connect-workspace" "$H/.cursor-worker/argv" && ! grep -qx -- "--clone-git-repos" "$H/.cursor-worker/argv"'

echo "# refusals: nothing is created"
run no-token CURSOR_AUTH_TOKEN=; sleep 1; find_sbx
check "no session token: refused, so the service-account key never enters a sandbox" '[ "$CODE" != 0 ] && [ -z "$SBX" ] && grep -q -- "--session-token" "$C/out"'
run wake CURSOR_WAKE=1; sleep 1; find_sbx
check "a wake: exit 0, no sandbox (Cursor queues the request again)" '[ "$CODE" = 0 ] && [ -z "$SBX" ]'
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
run odd-name STUB_RUN=0 'CURSOR_WORKER_NAME=$(touch pwned) name'
wait_log "deleted"
check "a worker name is reduced to safe characters" 'grep -qx "CURSOR_WORKER_NAME=touchpwnedname" "$H/.cursor-worker/seen-env" && [ ! -e "$H/work/pwned" ] && [ ! -e "$H/pwned" ]'

echo
[ "$FAILS" = 0 ] && echo "all passed" || { echo "$FAILS failed"; exit 1; }
