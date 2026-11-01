#!/usr/bin/env bash
# Offline tests for spawn-runner against fake_prized.py: no network, no real Claude Code, a fake key.
# Run: bash test/test.sh   (needs bash, curl, jq, python3)
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); HOOK=$(cd "$HERE/.." && pwd)/spawn-runner
T=$(mktemp -d); trap 'stop_server; rm -rf "$T"' EXIT
pass=0 fail=0
ok() { pass=$((pass + 1)); echo "ok   $*"; }
no() { fail=$((fail + 1)); echo "FAIL $*"; }
check() { local what=$1; shift; if "$@"; then ok "$what"; else no "$what"; fi; }
KEY="pk_test_$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SP=

start_server() {  # fresh fake Prized in $ROOT
  ROOT=$(mktemp -d "$T/root.XXXX"); CONF="$ROOT/conf"; mkdir -p "$CONF/hooks"
  printf '%s\n' "$KEY" > "$CONF/prized-api-key"
  PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
  python3 -I "$HERE/fake_prized.py" "$PORT" "$ROOT" "$KEY" "$HERE/sandbox-bin" 2> "$ROOT/server.err" & SP=$!
  for _ in $(seq 50); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done
}
stop_server() {
  if [ -n "${ROOT:-}" ]; then for p in "$ROOT"/sbx_*/home/.claude-runner/pid; do [ -f "$p" ] && kill -TERM -- "-$(cat "$p")" 2>/dev/null; done; fi
  if [ -n "${ROOT:-}" ]; then for p in "$ROOT"/box/*/pids; do [ -f "$p" ] && while read -r q; do kill -TERM -- "-$q" 2>/dev/null; done < "$p"; done; fi
  if [ -n "$SP" ]; then kill "$SP" 2>/dev/null; wait "$SP" 2>/dev/null; SP=; fi
}
# hook ORDER [SESSION]: run the hook as the orchestrator does; the work-order file is deleted after it exits
hook() {
  local wo; wo=$(mktemp "$T/wo.XXXX"); if [ -n "${WO:-}" ]; then printf '%s' "$WO" > "$wo"; else printf 'eyJhbGciOiJFUzI1NiJ9.eyJqdGkiOiIk%s.c2ln' "$1" > "$wo"; fi; chmod 600 "$wo"
  local t0; t0=$(date +%s%N)
  mkdir -p "$ROOT/boxtmp"
  env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" ARGV_LOG="$ROOT/argv.log" PRIZED_CLAUDE_DIR="$CONF" TMPDIR="$ROOT/boxtmp" \
    PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" CLAUDE_RUNNER_WORK_ORDER_FILE="$wo" CLAUDE_RUNNER_ORDER_ID="$1" \
    CLAUDE_RUNNER_SESSION_ID="${2-session_01abc}" ${HOOK_ENV:-} bash "$HOOK" > "$ROOT/hook.out" 2> "$ROOT/hook.err"
  rc=$?; ms=$(( ($(date +%s%N) - t0) / 1000000 )); rm -f "$wo"; return 0
}
sbx_ids() { for d in "$ROOT"/sbx_*/; do [ -d "$d" ] && basename "$d"; done; }
nsbx() { sbx_ids | wc -l; }
wait_for() { local i; for i in $(seq 50); do eval "$1" && return 0; sleep 0.1; done; return 1; }

runwatch_() { env -i HOME="$1/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" ARGV_LOG="$1/argv.log" PRIZED_CLAUDE_DIR="$CONF" \
  PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" WATCH_EVERY=0.3 bash "$HOOK" watch > "$1/watch.out" 2> "$1/watch.err" & WP=$!; }

orphan() { curl -s -o /dev/null -X POST -H "Authorization: Bearer $KEY" -H "Idempotency-Key: $1" \
  --data-raw '{"template":"prized-agent-v1","labels":{"managed-by":"claude-orchestrator","claude-order":"'"$1"'"}}' "http://127.0.0.1:$PORT/api/v1/sandboxes"; }

echo "# spawn"
start_server; mkdir -p "$ROOT/boxhome"
hook order_aaa session_01abc
check "exits 0 (got $rc): $(tail -c 200 "$ROOT/hook.err")" [ "$rc" = 0 ]
check "returns quickly (${ms} ms < 10000)" [ "$ms" -lt 10000 ]
check "one sandbox" [ "$(nsbx)" = 1 ]
S=$(sbx_ids | head -1); H="$ROOT/$S/home"
wait_for "[ -s '$H/.stub-runner-args' ]"
check "labels: managed-by, order, session" [ "$(jq -c .labels "$ROOT/$S/sandbox.json")" = '{"managed-by":"claude-orchestrator","claude-order":"order_aaa","claude-session":"session_01abc"}' ]
check "template prized-agent-v1, no resources asked, autoDeleteMin 60" [ "$(jq -c '[.template, .resources, .autoDeleteMin]' "$ROOT/$S/sandbox.json")" = '["prized-agent-v1",null,60]' ]
check "runner got the work order (file deleted on the host after the hook)" cmp -s "$H/.stub-seen-work-order" <(printf 'eyJhbGciOiJFUzI1NiJ9.eyJqdGkiOiIk%s.c2ln' order_aaa)
check "work order is mode 600 in the sandbox" [ "$(stat -c %a "$H/.claude-runner/work-order")" = 600 ]
args=$(cat "$H/.stub-runner-args")
for f in "--capacity 1" "--use-anthropic-git-proxy" "--environment-secret-file $H/.claude-runner/work-order" "--release-idle-session-min 30" \
  "--kill-session-after-min 480" "--push-outcome-on-release" "--configure-git" "--client-label $S" "--health-port 0" "--base-dir $H/work/claude" "--log-file $H/.claude-runner/runner.log"; do
  check "runner flag: $f" grep -q -- "$f" <<<"$args"
done
exp=$(date -d "$(jq -r .expiresAt "$ROOT/$S/sandbox.json")" +%s)
check "--retire-at 10 minutes before the sandbox's run time ends" grep -q -- "--retire-at $((exp - 600))" <<<"$args"
check "--exit-if-unused-min 30 on every runner, so one that never gets its session exits" grep -q -- "--exit-if-unused-min 30" <<<"$args"
check "run time: the session's limit plus an hour" [ "$(jq -r .maxRuntimeMin "$ROOT/$S/sandbox.json")" = 540 ]
check "same Claude Code version as the box: no install" [ ! -e "$H/.installed-version" ]
check "Idempotency-Key cc-<order> on the create" grep -q '"idem": "cc-order_aaa"' "$ROOT/requests.jsonl"
check "the key never reaches curl's argv" bash -c "! grep -qF -- '$KEY' '$ROOT/argv.log'"
check "the hook's output names the sandbox, not the key" bash -c "grep -q '$S' '$ROOT/hook.err' && ! grep -qF -- '$KEY' '$ROOT/hook.err' '$ROOT/hook.out'"

echo "# redelivery and re-requests"
hook order_aaa session_01abc
check "redelivered order exits 0" [ "$rc" = 0 ]
check "redelivered order makes no second sandbox" [ "$(nsbx)" = 1 ]
sleep 0.5; check "and starts no second runner" [ "$(wc -l < "$H/.stub-runner-args")" = 1 ]
hook order_bbb session_01abc
check "a new order for the same session gets its own sandbox" [ "$(nsbx)" = 2 ]
hook order_ccc ""
S3=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_ccc ] && echo "$s"; done)
wait_for "[ -s '$ROOT/$S3/home/.stub-runner-args' ]"
check "standby (no session): --exit-if-unused-min 30" grep -q -- "--exit-if-unused-min 30" "$ROOT/$S3/home/.stub-runner-args"
check "standby: no claude-session label" [ "$(jq -c '.labels | has("claude-session")' "$ROOT/$S3/sandbox.json")" = false ]
stop_server

echo "# settings"
start_server; mkdir -p "$ROOT/boxhome" "$CONF/sandbox-home/.claude"; echo '{"x":1}' > "$CONF/sandbox-home/.claude/settings.json"
HOOK_ENV="PRIZED_VCPU=2 PRIZED_MEMORY_GB=4 CLAUDE_IDLE_MIN=45 CLAUDE_MAX_MIN=120 HOST_CLAUDE_VERSION=2.1.312" hook order_set
S=$(sbx_ids | head -1); H="$ROOT/$S/home"; wait_for "[ -s '$H/.stub-runner-args' ]"
check "PRIZED_VCPU/PRIZED_MEMORY_GB set resources" [ "$(jq -c .resources "$ROOT/$S/sandbox.json")" = '{"vcpu":2,"memoryMb":4096}' ]
check "CLAUDE_IDLE_MIN and CLAUDE_MAX_MIN reach the runner" grep -q -- "--release-idle-session-min 45 --kill-session-after-min 120" "$H/.stub-runner-args"
check "the box's newer Claude Code is installed in the sandbox first" [ "$(cat "$H/.installed-version" 2>/dev/null)" = 2.1.312 ]
check "and the runner runs it from ~/.local/bin" grep -q "^via=local self-hosted-runner" "$H/.stub-runner-args"
check "sandbox-home lands in the sandbox's home" [ "$(cat "$H/.claude/settings.json" 2>/dev/null)" = '{"x":1}' ]
HOOK_ENV="PRIZED_VCPU=two" hook order_bad1
check "a non-number setting exits 2 (got $rc)" [ "$rc" = 2 ]
HOOK_ENV="CLAUDE_CODE_VERSION=1;rm" hook order_bad2
check "a bad CLAUDE_CODE_VERSION exits 2 (got $rc)" [ "$rc" = 2 ]
hook 'order bad'
check "an unexpected order id exits 2 (got $rc)" [ "$rc" = 2 ]
hook "o$(printf 'x%.0s' $(seq 130))"
check "an order id too long for an Idempotency-Key is hashed into one (rc $rc)" eval '[ "$rc" = 0 ] && grep -qE "\"idem\": \"cc-[0-9a-f]{64}\"" "$ROOT/requests.jsonl"'
hook "o$(printf 'x%.0s' $(seq 260))"
check "an order id longer than Kubernetes allows exits 2 (got $rc)" [ "$rc" = 2 ]
stop_server

echo "# run time"
FAKE_RUNTIME_MIN=none start_server; mkdir -p "$ROOT/boxhome"; hook order_noexp
S=$(sbx_ids | head -1); wait_for "[ -s '$ROOT/$S/home/.stub-runner-args' ]"
check "no expiresAt (a paused clock): no --retire-at" bash -c "! grep -q -- --retire-at '$ROOT/$S/home/.stub-runner-args'"
stop_server
FAKE_RUNTIME_MIN=60 start_server; mkdir -p "$ROOT/boxhome"; hook order_free
S=$(sbx_ids | head -1); wait_for "[ -s '$ROOT/$S/home/.stub-runner-args' ]"; exp=$(date -d "$(jq -r .expiresAt "$ROOT/$S/sandbox.json")" +%s)
check "free tier's 60 minutes: retire at 50" grep -q -- "--retire-at $((exp - 600))" "$ROOT/$S/home/.stub-runner-args"
stop_server
FAKE_RUNTIME_MIN=15 start_server; mkdir -p "$ROOT/boxhome"; hook order_short
S=$(sbx_ids | head -1); wait_for "[ -s '$ROOT/$S/home/.stub-runner-args' ]"
exp=$(date -d "$(jq -r .expiresAt "$ROOT/$S/sandbox.json")" +%s); ra=$(grep -oE -- '--retire-at [0-9]+' "$ROOT/$S/home/.stub-runner-args" | cut -d' ' -f2)
check "15 minutes of run time: retire a quarter before the end, not in the past (${ra:-none} vs $exp)" eval '[ -n "$ra" ] && [ $((exp - ra)) -ge 200 ] && [ $((exp - ra)) -le 240 ]'

stop_server

echo "# failures"
start_server; mkdir -p "$ROOT/boxhome"
echo '{"create": [503, 503]}' > "$ROOT/fail.json"; hook order_r1
check "two 503s on the create are retried (rc $rc)" [ "$rc" = 0 ]
echo '{"upload": [503]}' > "$ROOT/fail.json"; hook order_r2
check "a 503 on an upload is retried (rc $rc)" [ "$rc" = 0 ]
R2=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_r2 ] && echo "$s"; done)
check "and the retried upload carries the whole work order" cmp -s "$ROOT/$R2/home/.claude-runner/work-order" <(printf 'eyJhbGciOiJFUzI1NiJ9.eyJqdGkiOiIk%s.c2ln' order_r2)
check "the hook leaves no temp files on the box" [ -z "$(find "$ROOT/boxtmp" -mindepth 1 2>/dev/null)" ]
before=$(nsbx); echo '{"create": [401]}' > "$ROOT/fail.json"; hook order_f1
check "401 from Prized exits 2, non-retryable (got $rc)" [ "$rc" = 2 ]
check "with an actionable message" grep -q "Prized refused the sandbox (401): injected: injected 401" "$ROOT/hook.err"
echo '{"create": [422]}' > "$ROOT/fail.json"; hook order_f2
check "422 (shape outside the template or free tier) exits 2 (got $rc)" [ "$rc" = 2 ]
echo '{"create": [{"code": 422, "body": {"error": "invalid_resources", "field": "host_capacity"}}]}' > "$ROOT/fail.json"; hook order_f2b
check "422 host_capacity (a large shape) exits 2, not retried (got $rc)" eval '[ "$rc" = 2 ] && grep -q "Prized refused the sandbox (422): invalid_resources: host_capacity" "$ROOT/hook.err"'
echo '{"create": [409]}' > "$ROOT/fail.json"; hook order_f3
check "409 (balance, conflict) exits 1, retryable (got $rc)" [ "$rc" = 1 ]
echo '{"create": [500, 500, 500, 500]}' > "$ROOT/fail.json"; hook order_f4
check "500 after retries exits 1 (got $rc)" [ "$rc" = 1 ]
check "no sandbox from refused creates" [ "$(nsbx)" = "$before" ]
echo '{"exec": [500, 500, 500, 500]}' > "$ROOT/fail.json"; hook order_f5
check "a start that keeps failing exits 1 (got $rc)" [ "$rc" = 1 ]
rm -f "$ROOT/fail.json"
mv "$CONF/prized-api-key" "$CONF/k.bak"; hook order_f6
check "no key file exits 2 (got $rc)" [ "$rc" = 2 ]
mv "$CONF/k.bak" "$CONF/prized-api-key"
printf 'pk_wrong\n' > "$CONF/prized-api-key"; hook order_f7
check "a rejected key exits 2 (got $rc)" eval '[ "$rc" = 2 ] && grep -q "refused the API key (401)" "$ROOT/hook.err"'
printf '%s\n' "$KEY" > "$CONF/prized-api-key"
PORT0=$PORT; PORT=1; hook order_f8; PORT=$PORT0
check "Prized unreachable exits 1 (got $rc)" [ "$rc" = 1 ]
stop_server

echo "# review findings (10-29)"
start_server; mkdir -p "$ROOT/boxhome"
echo '{"exec_delay": [60]}' > "$ROOT/fail.json"; hook order_slow1
S=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_slow1 ] && echo "$s"; done)
check "a start whose answer never comes is asked again, and finds it started (rc $rc, ${ms} ms)" [ "$rc" = 0 ]
sleep 0.5; check "with one runner" [ "$(wc -l < "$ROOT/$S/home/.stub-runner-args")" = 1 ]
echo '{"exec_delay": [60, 60, 60, 60]}' > "$ROOT/fail.json"; hook order_slow2
S=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_slow2 ] && echo "$s"; done)
check "a start that never answers ends the hook in time for the orchestrator's 60 s (${ms} ms, rc $rc)" eval '[ "$rc" = 1 ] && [ "$ms" -lt 50000 ]'
check "and deletes the sandbox it made" [ -e "$ROOT/$S/deleted" ]
echo '{"upload_delay": [13, 13], "exec_delay": [60, 60, 60, 60]}' > "$ROOT/fail.json"; hook order_slow3
check "with under 15 s left, a call that never answers is not retried past the orchestrator's 60 s (${ms} ms, rc $rc)" eval '[ "$rc" = 1 ] && [ "$ms" -lt 50000 ]'
rm -f "$ROOT/fail.json"
HOOK_ENV="PRIZED_VCPU=" hook order_k
before=$(nsbx); HOOK_ENV="PRIZED_VCPU=2" hook order_k
check "the same order after a settings change uses its first sandbox (rc $rc)" eval '[ "$rc" = 0 ] && [ "$(nsbx)" = "$before" ]'
HOOK_ENV="HOST_CLAUDE_VERSION=garbage" hook order_j
S=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_j ] && echo "$s"; done)
check "a box Claude Code version it can't read: the template's Claude Code, no failure (rc $rc)" eval '[ "$rc" = 0 ] && grep -q '\''if \[ -n "" \]'\'' "$ROOT/$S/home/.claude-runner/run.sh"'
before=$(grep -c '"POST", "p": "/api/v1/sandboxes"' "$ROOT/requests.jsonl")
env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" PRIZED_CLAUDE_DIR="$CONF" PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" \
  CLAUDE_RUNNER_WORK_ORDER_FILE="$ROOT/no-such-file" CLAUDE_RUNNER_ORDER_ID=order_g CLAUDE_RUNNER_SESSION_ID=s bash "$HOOK" 2> "$ROOT/hook.err"; rc=$?
check "no work order: exit 1 before any sandbox (rc $rc)" eval '[ "$rc" = 1 ] && [ "$(grep -c '\''"POST", "p": "/api/v1/sandboxes"'\'' "$ROOT/requests.jsonl")" = "$before" ]'
mkdir -p "$ROOT/nojq" && ln -sf "$HERE/host-bin/curl" "$ROOT/nojq/curl"   # curl, and no jq
env -i HOME="$ROOT/boxhome" PATH="$ROOT/nojq" PRIZED_CLAUDE_DIR="$CONF" CLAUDE_RUNNER_ORDER_ID=order_d /bin/bash "$HOOK" 2> "$ROOT/hook.err"; rc=$?
check "no jq on the box: exit 2 saying so (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "needs curl and jq" "$ROOT/hook.err"'
hook order_gone; G=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_gone ] && echo "$s"; done)
curl -s -o /dev/null -X DELETE -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/api/v1/sandboxes/$G"; hook order_gone
check "a redelivered order whose sandbox is gone exits 1 saying so (rc $rc)" eval '[ "$rc" = 1 ] && grep -q "sandbox is gone" "$ROOT/hook.err"'

HOOK_ENV="CLAUDE_IDLE_MIN=0" hook order_i0
check "CLAUDE_IDLE_MIN=0 (never release) is refused (rc $rc)" [ "$rc" = 2 ]
HOOK_ENV="CLAUDE_IDLE_MIN=045" hook order_i45
S=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_i45 ] && echo "$s"; done)
wait_for "[ -s '$ROOT/$S/home/.stub-runner-args' ]"
check "a leading zero is read as decimal" grep -q -- "--release-idle-session-min 45 " "$ROOT/$S/home/.stub-runner-args"
stop_server
FAKE_SEED_SETSID_FAIL=1 start_server; mkdir -p "$ROOT/boxhome"; hook order_h
S=$(sbx_ids | head -1)
check "a start that fails after its marker exits 1 (rc $rc)" [ "$rc" = 1 ]
check "takes the marker back, so a redelivery would start it" [ ! -e "$ROOT/$S/home/.claude-runner/started" ]
check "and deletes the sandbox it made" [ -e "$ROOT/$S/deleted" ]
stop_server

echo "# review 2 findings (10-29)"
start_server; mkdir -p "$ROOT/boxhome"
HOOK_ENV="CLAUDE_MAX_MIN=20000" hook order_cap1; check "CLAUDE_MAX_MIN over 10080 is refused (rc $rc)" [ "$rc" = 2 ]
HOOK_ENV="CLAUDE_MAX_MIN=0" hook order_cap2; check "CLAUDE_MAX_MIN=0 (no backstop) is refused (rc $rc)" [ "$rc" = 2 ]
mkdir -p "$CONF/sandbox-home"; echo secret > "$CONF/sandbox-home/unreadable"; chmod 000 "$CONF/sandbox-home/unreadable"
before=$(nsbx); hook order_tar
check "a sandbox-home it can't pack fails the hook (rc $rc) and leaves no sandbox" eval '[ "$rc" = 1 ] && [ "$(ls "$ROOT"/sbx_*/deleted 2>/dev/null | wc -l)" -ge 1 ]'
chmod 600 "$CONF/sandbox-home/unreadable"; mv "$CONF/sandbox-home" "$ROOT/sh.saved"; ln -s "$ROOT" "$CONF/sandbox-home"
hook order_link; check "a sandbox-home that holds the keys is refused (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "must not hold" "$ROOT/hook.err"'
mv "$CONF/sandbox-home" "$ROOT/link.saved"
echo '{"exec_delay": [60, 60, 60]}' > "$ROOT/fail.json"
wo=$(mktemp "$T/wo.XXXX"); printf 'a.b.c' > "$wo"
env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" PRIZED_CLAUDE_DIR="$CONF" PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" \
  CLAUDE_RUNNER_WORK_ORDER_FILE="$wo" CLAUDE_RUNNER_ORDER_ID=order_term CLAUDE_RUNNER_SESSION_ID=s bash "$HOOK" 2> "$ROOT/hook.err" & HP=$!
sleep 4; kill -TERM "$HP"; wait "$HP"; rc=$?; rm -f "$ROOT/fail.json"
S=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_term ] && echo "$s"; done)
check "SIGTERM mid-hook (the orchestrator's timeout) exits 1 (rc $rc) and deletes the sandbox it made" eval '[ "$rc" = 1 ] && [ -e "$ROOT/$S/deleted" ]'
hook order_deg; D=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_deg ] && echo "$s"; done)
hook order_slowpid; P=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_slowpid ] && echo "$s"; done)
wait_for "[ -s '$ROOT/$D/home/.stub-runner-args' ] && [ -s '$ROOT/$P/home/.stub-runner-args' ]"
curl -s -o /dev/null -X POST -H "Authorization: Bearer $KEY" --data-binary error:runtime_degraded "http://127.0.0.1:$PORT/api/v1/sandboxes/$D/state"
: > "$ROOT/$P/home/.slow-head"
runwatch_ "$ROOT"; sleep 4
check "a sandbox in error with runtime_degraded is given time, not deleted" [ ! -e "$ROOT/$D/deleted" ]
check "a live runner whose pid reads slowly is not taken for exited" [ ! -e "$ROOT/$P/deleted" ]
kill "$WP" 2>/dev/null; wait "$WP" 2>/dev/null
stop_server
FAKE_AGE=3700 start_server; mkdir -p "$ROOT/boxhome"   # creates are dated just over an hour back
orphan order_forged; F=$(sbx_ids | head -1); mkdir -p "$ROOT/$F/home/.claude-runner"; echo -1 > "$ROOT/$F/home/.claude-runner/pid"
env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" PRIZED_CLAUDE_DIR="$CONF" CLAUDE_MAX_MIN=1 \
  PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" WATCH_EVERY=0.3 bash "$HOOK" watch > "$ROOT/watch.out" 2>&1 & WP=$!
wait_for "[ -e '$ROOT/$F/deleted' ]"
check "a sandbox past its session's limit plus an hour is deleted, whatever its pid file says" [ -e "$ROOT/$F/deleted" ]
kill "$WP" 2>/dev/null; wait "$WP" 2>/dev/null; stop_server

echo "# watch: a session cannot stall it"
start_server; mkdir -p "$ROOT/boxhome"; hook order_fa; hook order_fb
A=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_fa ] && echo "$s"; done)
B=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_fb ] && echo "$s"; done)
wait_for "[ -s '$ROOT/$A/home/.stub-runner-args' ] && [ -s '$ROOT/$B/home/.stub-runner-args' ]"
APID=$(cat "$ROOT/$A/home/.claude-runner/pid"); mv "$ROOT/$A/home/.claude-runner/pid" "$ROOT/$A/pid.saved"; mkfifo "$ROOT/$A/home/.claude-runner/pid"
echo 0 > "$ROOT/$B/home/.stub-stop"
t0=$(date +%s); runwatch_ "$ROOT"; wait_for "[ -e '$ROOT/$B/deleted' ]" || sleep 20
check "a pid file turned into a FIFO does not hold up the others ($(( $(date +%s) - t0 )) s)" eval '[ -e "$ROOT/$B/deleted" ] && [ $(( $(date +%s) - t0 )) -lt 10 ]'
kill "$WP" 2>/dev/null; wait "$WP" 2>/dev/null; kill -TERM -- "-$APID" 2>/dev/null
stop_server

echo "# watch"
start_server; mkdir -p "$ROOT/boxhome"
hook order_w1; hook order_w2
W1=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_w1 ] && echo "$s"; done)
W2=$(for s in $(sbx_ids); do [ "$(jq -r '.labels["claude-order"]' "$ROOT/$s/sandbox.json")" = order_w2 ] && echo "$s"; done)
wait_for "[ -s '$ROOT/$W1/home/.stub-runner-args' ] && [ -s '$ROOT/$W2/home/.stub-runner-args' ]"
echo "runner log line" > "$ROOT/$W1/home/.claude-runner/runner.log"
echo 0 > "$ROOT/$W1/home/.stub-stop"   # W1's runner exits; W2's keeps running
env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" ARGV_LOG="$ROOT/argv.log" PRIZED_CLAUDE_DIR="$CONF" \
  PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" WATCH_EVERY=0.3 bash "$HOOK" watch > "$ROOT/watch.out" 2> "$ROOT/watch.err" & WP=$!
wait_for "[ -e '$ROOT/$W1/deleted' ]"
check "watch deletes the sandbox whose runner exited" [ -e "$ROOT/$W1/deleted" ]
check "and keeps its log on the box" grep -q "runner log line" "$CONF/logs/$W1.log"
check "and says so" grep -q "deleted $W1" "$ROOT/watch.out"
sleep 1; check "watch leaves a running runner's sandbox alone" [ ! -e "$ROOT/$W2/deleted" ]
curl -s -o /dev/null -X POST -H "Authorization: Bearer $KEY" --data-binary error "http://127.0.0.1:$PORT/api/v1/sandboxes/$W2/state"
wait_for "[ -e '$ROOT/$W2/deleted' ]"
check "watch deletes a sandbox in error" [ -e "$ROOT/$W2/deleted" ]
kill "$WP" 2>/dev/null; wait "$WP" 2>/dev/null
stop_server

echo "# watch: a sandbox whose runner never started (its hook was killed outright)"
start_server; mkdir -p "$ROOT/boxhome"; orphan order_ns; NS=$(sbx_ids | head -1)
runwatch_ "$ROOT"; sleep 1.5
check "a fresh never-started sandbox is kept (its hook may still be starting it)" [ ! -e "$ROOT/$NS/deleted" ]
kill "$WP" 2>/dev/null; wait "$WP" 2>/dev/null; stop_server
FAKE_AGE=660 start_server; mkdir -p "$ROOT/boxhome"; orphan order_old; OLD=$(sbx_ids | head -1)   # created 11 minutes back
runwatch_ "$ROOT"; wait_for "[ -e '$ROOT/$OLD/deleted' ]"
check "a sandbox that has not started its runner in 10 minutes is deleted" [ -e "$ROOT/$OLD/deleted" ]
kill "$WP" 2>/dev/null; wait "$WP" 2>/dev/null; stop_server

echo "# serve"
start_server; mkdir -p "$ROOT/boxhome"; : > "$ROOT/orch-args"
env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" ARGV_LOG="$ROOT/argv.log" PRIZED_CLAUDE_DIR="$CONF" ORCH_ARGS="$ROOT/orch-args" \
  PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" WATCH_EVERY=0.3 bash "$HOOK" serve > "$ROOT/serve.out" 2>&1 & SV=$!
wait_for "[ -s '$ROOT/orch-args' ] && grep -q 'GET.*/api/v1/sandboxes' '$ROOT/requests.jsonl'"
check "serve execs the orchestrator with the documented flags" grep -qx -- "self-hosted-runner orchestrator --environment-secret-file $CONF/environment-secret --hooks-dir $(dirname "$HOOK") --health-port 0 --shutdown-drain-sec 70" "$ROOT/orch-args"
check "as the same process, which holds the box awake" [ "$(cat "$ROOT/boxhome/.prized/keep-awake/claude-orchestrator" 2>/dev/null)" = "$SV" ]
check "and starts the watch beside it" grep -q 'GET.*/api/v1/sandboxes' "$ROOT/requests.jsonl"
killtree() { local c; for c in $(pgrep -P "$1"); do killtree "$c"; done; kill "$1" 2>/dev/null; }
killtree "$SV"; wait "$SV" 2>/dev/null   # serve's restart loop and the watch under it
stop_server


# ---------- own-box mode ----------
# box ID HOST STATE: a fake box (its home, Claude Code 2.1.312 on its PATH)
box() { mkdir -p "$ROOT/box/$1/home"; echo "$3" > "$ROOT/box/$1/state"; echo 2.1.312 > "$ROOT/box/$1/home/.stub-version"; }
team() { printf '%s' "$1" > "$ROOT/team.json"; }
TEAM1='{"scope":"workspace","members":[{"userId":"u_kat","name":"Kat","email":"kat@example.com","role":"member"},{"userId":"u_lee","name":"Lee","email":"lee@example.com","role":"member"},{"userId":"u_ann","name":"Ann","email":"ann@example.com","role":"owner"}],
 "boxes":[{"id":"b-mango","hostname":"mango","ownerUserId":"u_kat","ownerEmail":"kat@example.com"},
          {"id":"b-pear","hostname":"pear","ownerUserId":"u_ann","ownerEmail":"ann@example.com"},
          {"id":"b-fig","hostname":"fig","ownerUserId":"u_ann","ownerEmail":"ann@example.com"},
          {"id":"b-old","hostname":"old","ownerUserId":"u_lee","ownerEmail":"lee@example.com","desiredState":"terminated"}]}'
# obhook ORDER [SESSION] [EMAIL]: the hook in own-box mode, the session started by EMAIL (default Kat, in odd case)
obhook() { HOOK_ENV="PRIZED_RUN_ON=own-box PRIZED_WAKE_POLL=0.2 CLAUDE_RUNNER_ACCOUNT_EMAIL=${3-Kat@Example.COM} ${OB_ENV:-}" hook "$1" "${2-session_01abc}"; }
wo_of() { printf 'eyJhbGciOiJFUzI1NiJ9.eyJqdGkiOiIk%s.c2ln' "$1"; }
noemail() { ! grep -rqiF -e kat@example -e ann@example -e lee@example "$@" 2>/dev/null; }
ob_start() { start_server; mkdir -p "$ROOT/boxhome"; team "$TEAM1"; box b-mango mango running; box b-pear pear running; box b-fig fig running; box b-old old terminated; }

echo "# own-box: a session runs on its person's own box"
ob_start; M="$ROOT/box/b-mango/home"
obhook order_o1
check "exits 0 (got $rc): $(tail -c 200 "$ROOT/hook.err")" [ "$rc" = 0 ]
check "returns quickly (${ms} ms < 10000)" [ "$ms" -lt 10000 ]
wait_for "[ -s '$M/.stub-runner-args' ]"
check "the runner starts on the box of the member whose email started the session (any case)" [ -s "$M/.stub-runner-args" ]
check "and on no one else's" [ ! -e "$ROOT/box/b-pear/home/.stub-runner-args" ]
check "no sandbox" [ "$(nsbx)" = 0 ]
check "the runner got the work order" cmp -s "$M/.stub-seen-work-order" <(wo_of order_o1)
check "which sits on the box mode 600" [ "$(stat -c %a "$M/.claude-runner/orders/cc-order_o1/work-order")" = 600 ]
check "and never in a command" bash -c "! grep -qF -- '$(wo_of order_o1)' '$ROOT/requests.jsonl'"
args=$(cat "$M/.stub-runner-args")
for f in "--capacity 1" "--environment-secret-file $M/.claude-runner/orders/cc-order_o1/work-order" "--base-dir $M/claude-sessions/1" \
  "--client-label mango" "--health-port 0" "--release-idle-session-min 30" "--kill-session-after-min 480" "--exit-if-unused-min 30" \
  "--push-outcome-on-release" "--log-file $M/.claude-runner/orders/cc-order_o1/runner.log"; do
  check "runner flag: $f" grep -q -- "$f" <<<"$args"
done
for f in --use-anthropic-git-proxy --configure-git --retire-at; do
  check "no $f: the box's own git sign-in, its own gitconfig, no run-time limit" bash -c "! grep -q -- '$f' <<<'$args'"
done
check "the start is detached and holds the box awake" grep -q '"detached": true, "keepAwake": true' "$ROOT/requests.jsonl"
check "a running box is not woken" [ ! -e "$ROOT/box/b-mango/wakes" ]
check "the key never reaches curl's argv" bash -c "! grep -qF -- '$KEY' '$ROOT/argv.log'"
check "the hook names the box and session, never the email" eval 'grep -q "starting on mango" "$ROOT/hook.err" && noemail "$ROOT/hook.err" "$ROOT/hook.out"'
check "the email never reaches jq's or awk's argv" eval 'grep -q "^jq " "$ROOT/argv.log" && noemail "$ROOT/argv.log"'

echo "# own-box: redelivery, more sessions, slots"
obhook order_o1
check "a redelivered order exits 0 (rc $rc) and says so" eval '[ "$rc" = 0 ] && grep -q "already started on mango" "$ROOT/hook.err"'
sleep 0.5; check "and starts no second runner" [ "$(wc -l < "$M/.stub-runner-args")" = 1 ]
obhook order_o2 session_02
wait_for "[ \$(wc -l < '$M/.stub-runner-args') = 2 ]"
check "a second session on the same box takes slot 2 (rc $rc)" eval '[ "$rc" = 0 ] && tail -n 1 "$M/.stub-runner-args" | grep -q -- "--base-dir $M/claude-sessions/2"'
obhook order_o3 session_03
check "with both slots busy, a third exits 1 (retryable) saying so (rc $rc)" eval '[ "$rc" = 1 ] && grep -q "already runs 2 sessions" "$ROOT/hook.err"'
echo 0 > "$M/.stub-stop"
wait_for "[ -e '$M/.claude-runner/orders/cc-order_o1/done' ] && [ -e '$M/.claude-runner/orders/cc-order_o2/done' ]"
check "a runner that exits leaves done and takes its work order off the box" eval '[ "$(cat "$M/.claude-runner/orders/cc-order_o1/done")" = 0 ] && [ ! -e "$M/.claude-runner/orders/cc-order_o1/work-order" ]'
rm -f "$M/.stub-stop"; obhook order_o4 session_04
wait_for "[ \$(wc -l < '$M/.stub-runner-args') = 3 ]"
check "its slot is free again: the next session reuses slot 1's clone" eval 'tail -n 1 "$M/.stub-runner-args" | grep -q -- "--base-dir $M/claude-sessions/1 "'
OB_ENV="CLAUDE_BOX_SLOTS=1" obhook order_o5 session_05
check "CLAUDE_BOX_SLOTS=1: one session at a time (rc $rc)" eval '[ "$rc" = 1 ] && grep -q "already runs 1 sessions" "$ROOT/hook.err"'
echo 0 > "$M/.stub-stop"
stop_server

echo "# own-box: waking the box"
ob_start; M="$ROOT/box/b-mango/home"; echo suspended > "$ROOT/box/b-mango/state"; echo 1 > "$ROOT/box/b-mango/wake_after"
obhook order_w1
wait_for "[ -s '$M/.stub-runner-args' ]"
check "a box that wakes in a second: woken, runner started within the hook (rc $rc, ${ms} ms)" eval '[ "$rc" = 0 ] && [ -e "$ROOT/box/b-mango/wakes" ] && grep -q "starting on mango" "$ROOT/hook.err"'
echo 0 > "$M/.stub-stop"; wait_for "[ -e '$M/.claude-runner/orders/cc-order_w1/done' ]"; rm -f "$M/.stub-stop"
echo suspended > "$ROOT/box/b-mango/state"; echo 4 > "$ROOT/box/b-mango/wake_after"
OB_ENV="PRIZED_WAKE_WAIT=1" obhook order_w2 session_w2
check "a box still waking: the hook hands off and exits 0 at once (rc $rc, ${ms} ms)" eval '[ "$rc" = 0 ] && [ "$ms" -lt 8000 ] && grep -q "waking mango for session_w2" "$ROOT/hook.err"'
check "keeping its own copy of the work order, mode 600" [ "$(stat -c %a "$CONF/orders/cc-order_w2/work-order" 2>/dev/null)" = 600 ]
WP=$(pgrep -f "$HOOK own-box-wait b-mango mango cc-order_w2 " | head -n 1)
check "the waiter (pid ${WP:-none}) runs without the email in its environment" eval '[ -n "$WP" ] && ! tr "\\0" "\\n" < /proc/$WP/environ | grep -q CLAUDE_RUNNER_ACCOUNT_EMAIL'
OB_ENV="PRIZED_WAKE_WAIT=1" obhook order_w2 session_w2
check "a redelivery while it waits starts no second waiter (rc $rc)" eval '[ "$rc" = 0 ] && grep -q "already waking mango" "$ROOT/hook.err"'
wait_for "grep -q 'order_w2' '$M/.stub-seen-work-order' 2>/dev/null" || sleep 5; wait_for "grep -q 'order_w2' '$M/.stub-seen-work-order' 2>/dev/null"
check "once the box is up, the waiter starts the runner with the work order the orchestrator already deleted" cmp -s "$M/.stub-seen-work-order" <(wo_of order_w2)
wait_for "[ ! -e '$CONF/orders/cc-order_w2' ]"
check "and removes its copy from this box" [ ! -e "$CONF/orders/cc-order_w2" ]
check "its log says what happened, without the email" eval 'grep -q "mango is up" "$CONF/logs/own-box.log" && grep -q "starting on mango" "$CONF/logs/own-box.log" && noemail "$CONF/logs/own-box.log"'
echo 0 > "$M/.stub-stop"; stop_server

echo "# own-box: a box that never wakes in time"
ob_start; echo suspended > "$ROOT/box/b-mango/state"; echo 1000 > "$ROOT/box/b-mango/wake_after"; mkdir -p "$CONF/orders/cc-order_late"; printf x > "$CONF/orders/cc-order_late/work-order"
t0=$(date +%s)
env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" PRIZED_CLAUDE_DIR="$CONF" PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" PRIZED_RUN_ON=own-box \
  PRIZED_WAKE_POLL=0.2 bash "$HOOK" own-box-wait b-mango mango cc-order_late "$(($(date +%s) + 23))" session_late > "$ROOT/wait.out" 2>&1; rc=$?
check "the waiter gives up before the work order expires (rc $rc, $(( $(date +%s) - t0 )) s)" eval '[ "$rc" = 1 ] && grep -q "was not up in time" "$ROOT/wait.out"'
check "and removes the work order's copy" [ ! -e "$CONF/orders/cc-order_late" ]
env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" PRIZED_CLAUDE_DIR="$CONF" PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" \
  bash "$HOOK" own-box-wait 'b-mango;x' mango cc-o 1 s > /dev/null 2>&1; rc=$?
check "the waiter refuses odd arguments (rc $rc)" [ "$rc" = 2 ]
stop_server

echo "# own-box: a waiter killed with the orchestrator (review 11-01)"
ob_start; echo suspended > "$ROOT/box/b-mango/state"; echo 1000 > "$ROOT/box/b-mango/wake_after"
OB_ENV="PRIZED_WAKE_WAIT=1" obhook order_k1 session_k1
K1=$(pgrep -f "$HOOK own-box-wait b-mango mango cc-order_k1 " | head -n 1)
check "a waiter is waiting (pid ${K1:-none})" [ -n "$K1" ]
kill -KILL -- "-$K1" 2>/dev/null; sleep 0.3  # its whole process group, as systemd kills the unit
OB_ENV="PRIZED_WAKE_WAIT=1" obhook order_k1 session_k1
K2=$(pgrep -f "$HOOK own-box-wait b-mango mango cc-order_k1 " | head -n 1)
check "after a SIGKILL its order is not stuck: a redelivery starts a new waiter (rc $rc, pid ${K2:-none})" eval '[ "$rc" = 0 ] && grep -q "waking mango" "$ROOT/hook.err" && [ -n "$K2" ] && [ "$K2" != "$K1" ]'
OB_ENV="PRIZED_WAKE_WAIT=1" obhook order_k1 session_k1
check "while the new one lives, a redelivery starts nothing (rc $rc)" eval '[ "$rc" = 0 ] && grep -q "already waking mango" "$ROOT/hook.err"'
kill -KILL -- "-$K2" 2>/dev/null
mkdir -p "$CONF/orders/cc-old" && printf x > "$CONF/orders/cc-old/work-order" && touch -d '30 minutes ago' "$CONF/orders/cc-old"
OB_ENV="PRIZED_WAKE_WAIT=1" obhook order_k2 session_k2
check "a hook clears a copy no waiter holds, once past any lease" [ ! -e "$CONF/orders/cc-old" ]
check "and keeps a recent one, in case its waiter is starting" [ -e "$CONF/orders/cc-order_k1" ]
K3=$(pgrep -f "$HOOK own-box-wait b-mango mango cc-order_k2 " | head -n 1); kill -KILL -- "-$K3" 2>/dev/null; sleep 0.3
: > "$ROOT/orch-args"
env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" PRIZED_CLAUDE_DIR="$CONF" ORCH_ARGS="$ROOT/orch-args" PRIZED_RUN_ON=own-box \
  PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" bash "$HOOK" serve > "$ROOT/serve.out" 2>&1 & SV=$!
wait_for "[ -s '$ROOT/orch-args' ]"
check "serve clears every copy a dead waiter left" eval '[ -z "$(ls -A "$CONF/orders" 2>/dev/null)" ]'
killtree "$SV"; wait "$SV" 2>/dev/null
stop_server

echo "# own-box: the waiter's deadline is the work order's (review 11-01)"
ob_start; echo suspended > "$ROOT/box/b-mango/state"; echo 1000 > "$ROOT/box/b-mango/wake_after"
b64() { base64 -w0 | tr '+/' '-_' | tr -d =; }
srv=$(( $(date +%s) - 3600 ))   # Anthropic's clock an hour behind this one
WO="$(printf '{"alg":"ES256"}' | b64).$(printf '{"jti":"order_e1","exp":%s}' $((srv + 70)) | b64).c2ln" \
  OB_ENV="PRIZED_WAKE_WAIT=1 CLAUDE_RUNNER_ORDER_SERVER_TIME=$(date -u -d "@$srv" +%Y-%m-%dT%H:%M:%SZ)" obhook order_e1 session_e1
t0=$(date +%s); for _ in $(seq 40); do grep -q 'not up in time for session_e1' "$CONF/logs/own-box.log" 2>/dev/null && break; sleep 1; done
el=$(( $(date +%s) - t0 ))
check "a box that doesn't wake: the waiter gives up by the work order's exp, on Anthropic's clock (${el} s after the hook)" eval 'grep -q "not up in time for session_e1" "$CONF/logs/own-box.log" && [ "$el" -ge 3 ] && [ "$el" -le 25 ]'
stop_server

echo "# own-box: a slow Claude Code on a box just woken (review 11-01)"
ob_start; : > "$ROOT/box/b-mango/home/.slow-version"; obhook order_v1
check "a version that doesn't come in time exits 1, retryable, not 'no Claude Code' (rc $rc)" eval '[ "$rc" = 1 ] && grep -q "did not tell its version in time" "$ROOT/hook.err"'
rm -f "$ROOT/box/b-mango/home/.slow-version"; obhook order_v1
check "and the retry starts it (rc $rc)" eval '[ "$rc" = 0 ] && grep -q "starting on mango" "$ROOT/hook.err"'
echo 0 > "$ROOT/box/b-mango/home/.stub-stop"; stop_server

echo "# own-box: emails compare in ASCII only (review 11-01)"
ob_start
team '{"scope":"workspace","members":[{"userId":"u_kit","name":"Kit","email":"kit@example.com","role":"member"},{"userId":"u_ann","name":"Ann","email":"ann@example.com","role":"owner"}],"boxes":[{"id":"b-plum","hostname":"plum","ownerUserId":"u_kit","ownerEmail":"kit@example.com"},{"id":"b-fig","hostname":"fig","ownerUserId":"u_ann","ownerEmail":"ann@example.com"}]}'
box b-plum plum running; u=0
for e in "$(printf 'K\xc4\xb0T@example.com')" "$(printf '\xe2\x84\xaaIT@example.com')"; do
  OB_ENV="LANG=C.UTF-8 LC_ALL=C.UTF-8" obhook "order_u$((++u))" session_u "$e"
  check "a Claude email with a non-ASCII letter (Turkish İ, Kelvin sign) is not taken for kit@ (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "no member of the Prized workspace" "$ROOT/hook.err"'
done
check "and kit's box got no runner" [ ! -e "$ROOT/box/b-plum/home/.stub-runner-args" ]
box b-fig fig running
printf '\xe2\x84\xaaIT@example.com fig\n' > "$CONF/own-boxes"; OB_ENV="LANG=C.UTF-8 LC_ALL=C.UTF-8" obhook order_u9 session_u "kit@example.com"
check "nor does an own-boxes line with one send kit@'s session to another box (rc $rc)" eval '[ "$rc" = 0 ] && grep -q "starting on plum" "$ROOT/hook.err" && [ ! -e "$ROOT/box/b-fig/home/.stub-runner-args" ]'
rm -f "$CONF/own-boxes"; echo 0 > "$ROOT/box/b-plum/home/.stub-stop"; stop_server

echo "# own-box: refusals"
ob_start
obhook order_r1 session_r1 ""
check "no account email (Slack, a service session) exits 2 (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "no account email" "$ROOT/hook.err"'
obhook order_r2 "" 
check "a standby request (no session) exits 2 (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "sessions only" "$ROOT/hook.err"'
obhook order_r3 session_r3 stranger@elsewhere.example
check "an email no member signs in with exits 2 (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "no member of the Prized workspace" "$ROOT/hook.err" && ! grep -q stranger "$ROOT/hook.err"'
obhook order_r4 session_r4 lee@example.com
check "a member whose only box is being deleted has no box: exits 2 (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "has no box in the Prized workspace" "$ROOT/hook.err" && noemail "$ROOT/hook.err"'
obhook order_r5 session_r5 ann@example.com
check "a member with two boxes exits 2 naming them (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "has 2 boxes (pear, fig)" "$ROOT/hook.err"'
printf '# claude email, box\nANN@example.com fig\nkat.work@corp.example mango\n' > "$CONF/own-boxes"
obhook order_r6 session_r6 ann@example.com
wait_for "[ -s '$ROOT/box/b-fig/home/.stub-runner-args' ]"
check "own-boxes picks one of them (rc $rc)" eval '[ "$rc" = 0 ] && [ -s "$ROOT/box/b-fig/home/.stub-runner-args" ] && [ ! -e "$ROOT/box/b-pear/home/.stub-runner-args" ]'
obhook order_r7 session_r7 kat.work@corp.example
check "and maps a Claude email Prized doesn't know to a box (rc $rc)" eval '[ "$rc" = 0 ] && grep -q "starting on mango" "$ROOT/hook.err"'
printf 'ann@example.com plum\n' > "$CONF/own-boxes"; obhook order_r8 session_r8 ann@example.com
check "own-boxes naming no live box exits 2 (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "no live box by that name" "$ROOT/hook.err"'
rm -f "$CONF/own-boxes"; echo 0 > "$ROOT/box/b-fig/home/.stub-stop"; echo 0 > "$ROOT/box/b-mango/home/.stub-stop"
echo provision_failed > "$ROOT/box/b-mango/state"; obhook order_r9
check "a box that failed to start exits 2 (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "mango failed to start" "$ROOT/hook.err"'
echo suspended > "$ROOT/box/b-mango/state"
echo '{"wake": [{"code": 409, "body": {"error": "wake_rejected", "message": "Out of credit: add credit to wake it", "reason": "out_of_credit"}}]}' > "$ROOT/fail.json"; obhook order_r10
check "a wake the balance refuses exits 2 with Prized's words (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "Prized would not wake mango (409): wake_rejected: out_of_credit: Out of credit" "$ROOT/hook.err"'
echo '{"wake": [{"code": 410, "body": {"error": "box_deleted"}}]}' > "$ROOT/fail.json"; obhook order_r11
check "a box being deleted exits 2 (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "(410): box_deleted" "$ROOT/hook.err"'
echo '{"wake": [503, 503, 503]}' > "$ROOT/fail.json"; obhook order_r12
check "a wake that keeps failing with 503 exits 1, retryable (rc $rc)" [ "$rc" = 1 ]
echo running > "$ROOT/box/b-mango/state"; rm -f "$ROOT/fail.json"
echo '{"team": [{"code": 403, "body": {"error": "key_access_denied", "area": "workspace", "level": "read", "message": "this key lacks Workspace: Read"}}]}' > "$ROOT/fail.json"; obhook order_r13
check "a key without Workspace: Read exits 2 saying what it needs (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "needs Boxes: Write and Workspace: Read" "$ROOT/hook.err"'
echo '{"team": [500, 500, 500]}' > "$ROOT/fail.json"; obhook order_r14
check "team status failing with 500 exits 1 (rc $rc)" [ "$rc" = 1 ]
echo '{"exec": [503, 503, 503]}' > "$ROOT/fail.json"; obhook order_r15
check "the box's edge failing with 503 exits 1 (rc $rc)" [ "$rc" = 1 ]
rm -f "$ROOT/fail.json"
rm -f "$ROOT/box/b-mango/home/.stub-stop"; echo 2.1.291 > "$ROOT/box/b-mango/home/.stub-version"; obhook order_r16
wait_for "grep -q order_r16 '$ROOT/box/b-mango/home/.stub-runner-args' 2>/dev/null"
check "Claude Code older than 2.1.309 on the box (a fresh box has 2.1.291) is updated first (rc $rc)" eval '[ "$rc" = 0 ] && grep -q "once its Claude Code 2.1.291 is updated" "$ROOT/hook.err" && grep -q "updated from 2.1.291" "$ROOT/box/b-mango/home/.claude-runner/orders/cc-order_r16/run.log"'
check "then the runner starts on the new version" grep -q "orders/cc-order_r16/work-order" "$ROOT/box/b-mango/home/.stub-runner-args"
echo 0 > "$ROOT/box/b-mango/home/.stub-stop"
echo 2.1.312 > "$ROOT/box/b-mango/home/.stub-version"; : > "$ROOT/box/b-mango/noclaude"; obhook order_r17
check "no Claude Code on the box exits 2 (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "has no Claude Code" "$ROOT/hook.err"'
rm -f "$ROOT/box/b-mango/noclaude"; : > "$ROOT/box/b-mango/old-agent"; obhook order_r18
check "a box agent that can't hold the box awake exits 2 with the edge's words (rc $rc)" eval '[ "$rc" = 2 ] && grep -q "agent_upgrade_required" "$ROOT/hook.err"'
check "and starts no runner" [ ! -e "$ROOT/box/b-mango/home/.claude-runner/orders/cc-order_r18/started" ]
rm -f "$ROOT/box/b-mango/old-agent"
for bad in PRIZED_RUN_ON=boxes CLAUDE_BOX_SLOTS=9 CLAUDE_SPAWN_SECONDS=60 CLAUDE_SPAWN_SECONDS=4000; do
  OB_ENV="$bad" obhook order_r19; check "$bad is refused (rc $rc)" [ "$rc" = 2 ]
done
check "no refusal printed an email" noemail "$ROOT/hook.err"
stop_server

echo "# own-box: a contractor's key sees only its own boxes"
start_server; mkdir -p "$ROOT/boxhome"; box b-kiwi kiwi running
team '{"scope":"own","members":[],"boxes":[{"id":"b-kiwi","hostname":"kiwi","ownerUserId":"u_c","ownerEmail":"cy@contract.example"}]}'
obhook order_c1 session_c1 cy@contract.example
wait_for "[ -s '$ROOT/box/b-kiwi/home/.stub-runner-args' ]"
check "its own box is found by the box's owner email (rc $rc)" eval '[ "$rc" = 0 ] && [ -s "$ROOT/box/b-kiwi/home/.stub-runner-args" ]'
echo 0 > "$ROOT/box/b-kiwi/home/.stub-stop"; stop_server

echo "# own-box: serve"
start_server; mkdir -p "$ROOT/boxhome"; : > "$ROOT/orch-args"
env -i HOME="$ROOT/boxhome" PATH="$HERE/host-bin:/usr/bin:/bin" PRIZED_CLAUDE_DIR="$CONF" ORCH_ARGS="$ROOT/orch-args" PRIZED_RUN_ON=own-box \
  PRIZED_API_URL="http://127.0.0.1:$PORT/api/v1" WATCH_EVERY=0.3 bash "$HOOK" serve > "$ROOT/serve.out" 2>&1 & SV=$!
wait_for "[ -s '$ROOT/orch-args' ]"; sleep 1
check "serve gives the orchestrator 600 s to see a waking box's runner register" grep -qx -- "self-hosted-runner orchestrator --environment-secret-file $CONF/environment-secret --hooks-dir $(dirname "$HOOK") --health-port 0 --shutdown-drain-sec 70 --expected-spawn-seconds 600" "$ROOT/orch-args"
check "and starts no sandbox watch" bash -c "! grep -q 'GET.*/api/v1/sandboxes' '$ROOT/requests.jsonl' 2>/dev/null"
killtree "$SV"; wait "$SV" 2>/dev/null
stop_server

echo "# $pass passed, $fail failed"
[ "$fail" = 0 ]
