#!/usr/bin/env bash
# check() evals its single-quoted condition later, so variables set here are used there.
# shellcheck disable=SC2016,SC2034
# Offline tests for dispatch.sh. The Prized API is a fake on 127.0.0.1 (fake_api.py) whose sandboxes
# are directories, and Amp's installer and CLI are stubs (sandbox-bin/curl, stub-amp). No network
# beyond loopback, no keys.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DISPATCH=$HERE/../dispatch.sh
T=$(mktemp -d)
FAKE_PID=
stop_fake() { if [ -n "$FAKE_PID" ]; then kill "$FAKE_PID" 2>/dev/null || true; wait "$FAKE_PID" 2>/dev/null || true; FAKE_PID=; fi; }
trap 'stop_fake; rm -rf "$T"' EXIT
KEY=dcp_000000000000000000000000000000test
TOKEN=orb-token-0000000000000000-not-real
ID=amp-T-0b5e2f4a-1c2d-4e5f-8a9b-0c1d2e3f4a5b
# A curl and a jq on the provider's PATH that record each command line, so the checks can see that
# neither the API key nor the orb token is ever on one. The values here are fakes.
mkdir -p "$T/bin"
for tool in curl jq; do
  cat > "$T/bin/$tool" <<EOF
#!/usr/bin/env bash
[ -z "\${ARGV_LOG:-}" ] || printf '%s\n' "\$*" >> "\$ARGV_LOG"
exec $(command -v "$tool") "\$@"
EOF
  chmod +x "$T/bin/$tool"
done
export PATH="$T/bin:$PATH"
FAILS=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; FAILS=$((FAILS + 1)); fi; }

# fresh NAME [VAR=value...]: a new fake API in $T/NAME (its state in $S), started with the settings given.
fresh() {
  stop_fake
  S=$T/$1; shift
  mkdir -p "$S"
  [ -z "${SEED:-}" ] || printf '%s' "$SEED" > "$S/seed.json"
  [ -z "${FAULTS:-}" ] || printf '%s' "$FAULTS" > "$S/faults.json"
  env FAKE_PRIZED_KEY=$KEY STUB_EXPECT_TOKEN=$TOKEN "$@" python3 "$HERE/fake_api.py" "$S" &
  FAKE_PID=$!
  local i
  for i in $(seq 100); do [ -s "$S/port" ] && break; sleep 0.05; done
  API=http://127.0.0.1:$(cat "$S/port")/api/v1
}
# job OP [VAR=value...]: run dispatch.sh as amp orb provider would, against the current fake.
job() {
  local op=$1; shift
  CODE=0
  env -u AMP_URL -u ORB_SNAPSHOT_ID PRIZED_API_URL="$API" PRIZED_API_KEY=$KEY ARGV_LOG="$S/argv" \
    ORB_OPERATION="$op" ORB_ID=$ID ORB_TOKEN=$TOKEN ORB_IMAGE=us-central1-docker.pkg.dev/amp-prod-0/amp-orb/amp-orb:latest \
    "$@" "$DISPATCH" > "$S/out" 2>&1 || CODE=$?
  SBX=$(jq -r '.[0].id // empty' "$S/state.json")
  H=$S/$SBX/home
}
sandboxes() { jq length "$S/state.json"; }
state() { jq -r --arg id "${1:-$SBX}" '.[] | select(.id == $id) | .state' "$S/state.json"; }
requests() { jq -c "select($1)" "$S/requests.jsonl" 2>/dev/null | wc -l | tr -d ' '; }
seen() { cat "$H/.amp-orb/seen" 2>/dev/null || true; }
# Wait for the detached bootstrap to have written what it saw.
wait_seen() { local i; for i in $(seq 100); do [ "$(grep -c '^PWD=' "$H/.amp-orb/seen" 2>/dev/null || echo 0)" -ge "${1:-1}" ] && return 0; sleep 0.05; done; }

echo "# spawn: a new orb is a new sandbox running amp orb bootstrap with the job's token"
fresh spawn
job spawn
wait_seen
check "exit 0" '[ "$CODE" = 0 ] || { cat "$S/out"; false; }'
check "one sandbox, labelled with the orb id, from prized-base-v1, the template's own shape" \
  '[ "$(sandboxes)" = 1 ] && jq -e --arg id "$ID" ".[0] | .labels == {\"amp-orb\": \$id} and .template == \"prized-base-v1\" and .resources == {} and .maxRuntimeMin == null and .autoDeleteMin == null and .state == \"running\"" "$S/state.json" >/dev/null'
check "Amp CLI installed with Amp's installer" 'grep -q "https://ampcode.com/install.sh" "$H/.curl-calls" && [ -x "$H/.amp/bin/amp" ]'
check "bootstrap started detached, in the home folder, with the job's token" \
  'grep -qx "argv=orb bootstrap" <<<"$(seen)" && grep -qx "token=the job'"'"'s" <<<"$(seen)" && grep -qx "PWD=$H" <<<"$(seen)"'
check "AMP_URL left unset when the job has none" 'grep -qx "AMP_URL=unset" <<<"$(seen)"'
check "the orb never sees the Prized API key" 'grep -qx "PRIZED_API_KEY=" <<<"$(seen)" && ! grep -rqa -e "$KEY" "$S/$SBX"'
check "the token travels only in a command's env, never in a command or a URL" \
  '[ "$(jq -c --arg t "$TOKEN" "select(tostring | contains(\$t))" "$S/requests.jsonl" | jq -s --arg t "$TOKEN" "map(select((.path | endswith(\"/process/execute\") | not) or (.body | del(.env.ORB_TOKEN) | tostring | contains(\$t)))) | length")" = 0 ] && [ "$(requests ".body.env.ORB_TOKEN == \"$TOKEN\"")" = 1 ]'
check "neither the key nor the token is ever on a command line (curl's, jq's) or in the output" \
  '! grep -q -e "$KEY" -e "$TOKEN" "$S/argv" "$S/out" && grep -q "api/v1/sandboxes" "$S/argv" && grep -q "^-n " "$S/argv"'
check "says what it did" 'grep -q "created $SBX from prized-base-v1 in" "$S/out" && grep -q "ready in" "$S/out"'

echo "# spawn again while it runs: the same sandbox, Amp not installed twice"
job spawn
wait_seen 2
check "exit 0, still one sandbox, no second create" '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 1 ] && [ "$(requests ".method == \"POST\" and .path == \"/api/v1/sandboxes\"")" = 1 ]'
check "installer ran once, bootstrap started again" '[ "$(grep -c install.sh "$H/.curl-calls")" = 1 ] && [ "$(grep -c "^argv=" <<<"$(seen)")" = 2 ]'

echo "# pause: keeps the sandbox, paused with its memory"
job pause
check "exit 0, paused, not deleted" '[ "$CODE" = 0 ] && [ "$(state)" = paused ] && [ "$(sandboxes)" = 1 ] && grep -q "paused $SBX in" "$S/out"'
check "asked to wait for paused" '[ "$(requests ".path == \"/api/v1/sandboxes/$SBX/pause\" and .query == \"wait=paused\"")" = 1 ]'
job pause
check "pause again: exit 0, no second pause request" '[ "$CODE" = 0 ] && grep -q "already paused" "$S/out" && [ "$(requests ".path | endswith(\"/pause\")")" = 1 ]'

echo "# spawn after a pause: the same sandbox resumed"
job spawn AMP_URL=https://amp.example.internal
wait_seen 3
check "exit 0, resumed, the same sandbox, no create" \
  '[ "$CODE" = 0 ] && [ "$(state)" = running ] && [ "$(sandboxes)" = 1 ] && [ "$(requests ".path == \"/api/v1/sandboxes/$SBX/resume\" and .query == \"wait=running\"")" = 1 ] && [ "$(requests ".method == \"POST\" and .path == \"/api/v1/sandboxes\"")" = 1 ]'
check "says it resumed with its memory" 'grep -q "resumed $SBX in .* with its memory and processes" "$S/out"'
check "AMP_URL passed into the orb when the job has one" '[ "$(grep -c "^AMP_URL=https://amp.example.internal$" <<<"$(seen)")" = 1 ]'

echo "# delete: removes the sandbox, by its label; again is a no-op"
job delete
check "exit 0, gone" '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 0 ] && [ "$(requests ".method == \"DELETE\" and .query == \"label=amp-orb%3D$ID\"")" = 1 ]'
job delete
check "delete again: exit 0" '[ "$CODE" = 0 ] && grep -q "deleting 0 sandbox" "$S/out"'
job pause
check "pause with no sandbox: exit 0, nothing created" '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 0 ] && grep -q "nothing to pause" "$S/out"'

echo "# a project snapshot id is ignored: the orb still pauses"
fresh snapshot
job spawn
job pause ORB_SNAPSHOT_ID=amp-snap-1234
check "paused, says snapshots aren't supported" '[ "$CODE" = 0 ] && [ "$(state)" = paused ] && grep -q "snapshots are not supported" "$S/out"'

echo "# settings"
fresh settings
job spawn PRIZED_TEMPLATE=prized-agent-v1 PRIZED_ORB_VCPU=8 PRIZED_ORB_MEMORY_GB=16 PRIZED_ORB_DISK_GB=40 \
  PRIZED_ORB_MAX_RUNTIME_MIN=240 PRIZED_ORB_AUTO_DELETE_MIN=20160 PRIZED_ORB_LABELS=team=web,cost-center=eng-1
check "template, shape, run time, auto-delete and labels on the create" \
  '[ "$CODE" = 0 ] && jq -e --arg id "$ID" ".[0] | .template == \"prized-agent-v1\" and .resources == {vcpu: 8, memoryMb: 16384, diskGb: 40} and .maxRuntimeMin == 240 and .autoDeleteMin == 20160 and .labels == {team: \"web\", \"cost-center\": \"eng-1\", \"amp-orb\": \$id}" "$S/state.json" >/dev/null'
check "no envVars and no auto-pause on the sandbox" 'jq -e ".[0] | .envVars == [] and .autoPauseMin == null" "$S/state.json" >/dev/null'
fresh label-override
job spawn PRIZED_ORB_LABELS=amp-orb=other
check "a label can't take over amp-orb" '[ "$CODE" = 0 ] && jq -e --arg id "$ID" ".[0].labels[\"amp-orb\"] == \$id" "$S/state.json" >/dev/null'

echo "# new orbs as live copies of a prepared sandbox"
PARENT=sbx_00000000000000f0
SEED='[{"id":"'$PARENT'","state":"paused","template":"prized-agent-v1","labels":{"role":"amp-orb-template"},"resources":{"vcpu":8},"autoPauseMin":null}]' fresh fork
mkdir -p "$S/$PARENT/home/.amp/bin" && cp "$HERE/stub-amp" "$S/$PARENT/home/.amp/bin/amp" && echo prepared > "$S/$PARENT/home/work/marker"
job spawn PRIZED_ORB_FORK_FROM=$PARENT PRIZED_ORB_LABELS=team=web PRIZED_ORB_MAX_RUNTIME_MIN=120
SBX=$(jq -r --arg p "$PARENT" '.[] | select(.id != $p) | .id' "$S/state.json"); H=$S/$SBX/home
wait_seen
check "a copy of the parent, labelled for the orb, the parent left paused" \
  '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 2 ] && jq -e --arg id "$ID" --arg p "$PARENT" --arg c "$SBX" ".[] | select(.id == \$c) | .forkedFrom == \$p and .labels == {team: \"web\", \"amp-orb\": \$id} and .maxRuntimeMin == 120 and .state == \"running\"" "$S/state.json" >/dev/null && [ "$(state $PARENT)" = paused ]'
check "the copy has the parent's files and Amp CLI: no install, no create" \
  '[ "$(cat "$H/work/marker")" = prepared ] && [ ! -e "$H/.curl-calls" ] && [ "$(requests ".method == \"POST\" and .path == \"/api/v1/sandboxes\"")" = 0 ] && grep -q "created $SBX from a copy of $PARENT" "$S/out"'
check "bootstrap started in the copy with the job's token" 'grep -qx "token=the job'"'"'s" <<<"$(seen)"'
job pause PRIZED_ORB_FORK_FROM=$PARENT
check "pause pauses the copy, never the parent" '[ "$CODE" = 0 ] && [ "$(state)" = paused ] && [ "$(requests ".path == \"/api/v1/sandboxes/$PARENT/pause\"")" = 0 ]'
job delete PRIZED_ORB_FORK_FROM=$PARENT
check "delete removes the copy, never the parent" '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 1 ] && [ "$(state $PARENT)" = paused ]'
SEED='[{"id":"'$PARENT'","state":"paused","template":"prized-base-v1","labels":{},"resources":{},"autoPauseMin":null}]' fresh fork-full FAKE_FORK_FULL=1
job spawn PRIZED_ORB_FORK_FROM=$PARENT
SBX=$(jq -r --arg p "$PARENT" '.[] | select(.id != $p) | .id' "$S/state.json"); H=$S/$SBX/home
check "no room for a copy: created from the template instead, and says so" \
  '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 2 ] && grep -q "could not fork $PARENT (fork_capacity)" "$S/out" && [ -x "$H/.amp/bin/amp" ]'
SEED='[{"id":"'$PARENT'","state":"paused","template":"prized-agent-v1","labels":{},"resources":{"vcpu":8,"memoryMb":16384,"diskGb":20},"autoPauseMin":null}]' fresh fork-full-shape FAKE_FORK_FULL=1
job spawn PRIZED_ORB_FORK_FROM=$PARENT
check "...from the parent's template and shape" \
  '[ "$CODE" = 0 ] && jq -e --arg p "$PARENT" ".[] | select(.id != \$p) | .template == \"prized-agent-v1\" and .resources == {vcpu: 8, memoryMb: 16384, diskGb: 20}" "$S/state.json" >/dev/null'
SEED='[{"id":"'$PARENT'","state":"paused","template":"prized-base-v1","labels":{"amp-orb":"amp-T-other"},"resources":{},"autoPauseMin":null}]' fresh fork-orb
job spawn PRIZED_ORB_FORK_FROM=$PARENT
check "a parent that is another orb's sandbox: refused" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 1 ] && grep -q "an orb.s own sandbox" "$S/out"'
SEED='[{"id":"'$PARENT'","state":"paused","template":"prized-base-v1","labels":{},"resources":{},"autoPauseMin":null}]' FAULTS='[{"method":"POST","path":"/fork","status":409,"times":1,"body":{"error":"insufficient_balance"}}]' fresh fork-broke
job spawn PRIZED_ORB_FORK_FROM=$PARENT
check "a fork refused for another reason: exit 1, no create" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 1 ] && grep -q "409 insufficient_balance" "$S/out"'
FAULTS=
SEED='[{"id":"'$PARENT'","state":"paused","template":"prized-base-v1","labels":{},"resources":{},"autoPauseMin":10}]' fresh fork-autopause
job spawn PRIZED_ORB_FORK_FROM=$PARENT
check "a parent with auto-pause on: refused, nothing forked" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 1 ] && grep -q "auto-pause on" "$S/out"'
fresh fork-missing
job spawn PRIZED_ORB_FORK_FROM=sbx_0000000000000404
check "a parent that doesn't exist: exit 1, 404 named" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 0 ] && grep -q "404 not_found" "$S/out"'
job spawn PRIZED_ORB_FORK_FROM='sbx_1;x'
check "a fork-from that isn't a sandbox id: refused" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 0 ]'
SEED=

echo "# waits: a slow create, resume and pause are waited for"
fresh slow FAKE_SLOW_CREATE=3 FAKE_SLOW_RESUME=3 FAKE_SLOW_PAUSE=3
job spawn
check "create answered starting: waits for running" '[ "$CODE" = 0 ] && [ "$(state)" = running ] && [ -x "$H/.amp/bin/amp" ]'
job pause
check "pause answered pausing: waits for paused" '[ "$CODE" = 0 ] && [ "$(state)" = paused ]'
job spawn
check "resume answered starting: waits for running" '[ "$CODE" = 0 ] && [ "$(state)" = running ]'
fresh stuck FAKE_SLOW_CREATE=100000
job spawn PRIZED_ORB_DEADLINE=3
check "a sandbox that never comes up: gives up within the deadline, exit 1" '[ "$CODE" = 1 ] && grep -q -e "still starting" -e "out of time" "$S/out"'
FAULTS='[{"method":"POST","path":"/api/v1/sandboxes","delay":6,"times":1}]' fresh slow-answer
START_T=$SECONDS
job spawn PRIZED_ORB_DEADLINE=3
check "an answer slower than the time left: the job still ends in time, exit 1" '[ "$CODE" = 1 ] && [ $((SECONDS - START_T)) -le 5 ]'
FAULTS=
SEED='[{"id":"sbx_00000000000000b1","state":"starting","slow":2,"target":"running","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' fresh starting
job spawn
check "spawn on a starting sandbox: waits for it, no create, no resume" \
  '[ "$CODE" = 0 ] && [ "$(state)" = running ] && [ "$(requests ".method == \"POST\" and (.path | test(\"/sandboxes$|/resume$\")))")" = 0 ]'
SEED='[{"id":"sbx_00000000000000b2","state":"resizing","slow":2,"target":"paused","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' fresh resizing
job spawn
check "spawn on a resizing sandbox: waits for paused, then resumes" \
  '[ "$CODE" = 0 ] && [ "$(state)" = running ] && [ "$(requests ".path == \"/api/v1/sandboxes/sbx_00000000000000b2/resume\"")" = 1 ] && [ "$(jq -s "map(.path) | (index(\"/api/v1/sandboxes/sbx_00000000000000b2\") // 1e9) < index(\"/api/v1/sandboxes/sbx_00000000000000b2/resume\")" "$S/requests.jsonl")" = true ]'
SEED='[{"id":"sbx_00000000000000b3","state":"starting","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' fresh pause-starting
job pause
check "pause on a starting sandbox: paused" '[ "$CODE" = 0 ] && [ "$(state)" = paused ] && [ "$(requests ".path == \"/api/v1/sandboxes/sbx_00000000000000b3/pause\"")" = 1 ]'
SEED=

echo "# retries: the 429 and 503 answers that say nothing happened are retried after Retry-After"
FAULTS='[{"method":"POST","path":"/api/v1/sandboxes","status":503,"retryAfter":2,"times":2,"body":{"error":"sandbox_capacity"}},
         {"method":"POST","path":"/process/execute","status":503,"retryAfter":1,"times":1,"body":{"error":"sandbox_not_ready"}},
         {"method":"GET","path":"/api/v1/sandboxes","status":429,"retryAfter":1,"times":1,"body":{"error":"rate_limited"}}]' fresh retry
job spawn
wait_seen
check "exit 0, one sandbox, bootstrap started once" '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 1 ] && [ "$(grep -c "^argv=" <<<"$(seen)")" = 1 ]'
check "create sent three times with one Idempotency-Key, the list retried" \
  '[ "$(requests ".method == \"POST\" and .path == \"/api/v1/sandboxes\"")" = 3 ] && [ "$(jq -s "map(select(.method == \"POST\" and .path == \"/api/v1/sandboxes\") | .idem) | unique | length" "$S/requests.jsonl")" = 1 ] && [ "$(requests ".method == \"GET\" and .path == \"/api/v1/sandboxes\"")" = 3 ]'
check "Retry-After honoured: the creates 2 s apart" \
  '[ "$(jq -s "map(select(.method == \"POST\" and .path == \"/api/v1/sandboxes\") | .t) | (.[1] - .[0]) >= 1.9" "$S/requests.jsonl")" = true ]'
job delete
job spawn
check "another job gets another Idempotency-Key" '[ "$(jq -s "map(select(.method == \"POST\" and .path == \"/api/v1/sandboxes\") | .idem) | unique | length" "$S/requests.jsonl")" = 2 ] && [ "$(sandboxes)" = 1 ]'
FAULTS='[{"method":"POST","path":"/api/v1/sandboxes","status":429,"times":5,"body":{"error":"sandbox_quota_exceeded","reason":"max_runtime"}}]' fresh quota
START_T=$SECONDS
job spawn
check "a 429 that won't clear (quota): exit 1 at once, named, sent once" \
  '[ "$CODE" = 1 ] && [ $((SECONDS - START_T)) -le 2 ] && [ "$(requests ".method == \"POST\" and .path == \"/api/v1/sandboxes\"")" = 1 ] && grep -q "429 sandbox_quota_exceeded: max_runtime" "$S/out"'
FAULTS='[{"method":"POST","path":"/api/v1/sandboxes","dropAfter":true,"times":1}]' fresh lost
job spawn
wait_seen
check "a create whose answer is lost: sent again with its Idempotency-Key, one sandbox" \
  '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 1 ] && [ "$(requests ".method == \"POST\" and .path == \"/api/v1/sandboxes\"")" = 2 ] && [ "$(jq -s "map(select(.method == \"POST\" and .path == \"/api/v1/sandboxes\") | .idem) | unique | length" "$S/requests.jsonl")" = 1 ]'
FAULTS='[{"method":"GET","path":"/api/v1/sandboxes","drop":true,"times":1}]' fresh lost-get
job spawn
check "a list whose answer is lost: asked again" '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 1 ]'
FAULTS='[{"method":"POST","path":"/process/execute","dropAfter":true,"times":1}]' fresh lost-exec
job spawn
check "a command whose answer is lost is never sent again: exit 1, says so" \
  '[ "$CODE" = 1 ] && [ "$(requests ".path | endswith(\"/process/execute\")")" = 1 ] && grep -q "no answer" "$S/out"'
FAULTS=

echo "# an orb whose sandbox is in error is started afresh"
SEED='[{"id":"sbx_00000000000000e1","state":"error","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' fresh error
job spawn
check "the failed sandbox deleted, a new one running" \
  '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 1 ] && [ "$SBX" != sbx_00000000000000e1 ] && [ "$(state)" = running ] && [ "$(requests ".method == \"DELETE\" and .path == \"/api/v1/sandboxes/sbx_00000000000000e1\"")" = 1 ]'
SEED='[{"id":"sbx_00000000000000e2","state":"error","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' fresh error-pause2
job pause
check "pause on a sandbox in error deletes it" '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 0 ]'
SEED='[{"id":"sbx_00000000000000e3","state":"error","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' FAULTS='[{"method":"DELETE","path":"/api/v1/sandboxes/sbx_00000000000000e3","status":500,"times":5}]' fresh error-undeletable
job spawn
check "a sandbox in error that can't be deleted: exit 1, no second sandbox" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 1 ] && grep -q "delete sbx_00000000000000e3: 500" "$S/out"'
FAULTS=
SEED='[{"id":"sbx_00000000000000d1","state":"error","errorReason":"runtime_degraded: not answering","slow":2,"target":"running","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' fresh degraded
job spawn
check "a degraded sandbox that answers again is kept and used" '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 1 ] && [ "$(state sbx_00000000000000d1)" = running ] && [ "$(requests ".method == \"DELETE\"")" = 0 ]'
SEED='[{"id":"sbx_00000000000000d2","state":"error","errorReason":"runtime_degraded: not answering","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' fresh degraded-pause
job pause
check "pause on a degraded sandbox pauses it, keeping it" '[ "$CODE" = 0 ] && [ "$(state sbx_00000000000000d2)" = paused ]'
SEED=

echo "# two sandboxes for one orb"
SEED='[{"id":"sbx_00000000000000c2","state":"paused","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}},
       {"id":"sbx_00000000000000c1","state":"running","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' fresh two
job spawn
check "spawn uses the running one: no resume, no create" '[ "$CODE" = 0 ] && [ "$(requests ".method == \"POST\" and (.path | test(\"/sandboxes$|/resume$\")))")" = 0 ] && grep -q "sbx_00000000000000c1 is already running" "$S/out"'
job pause
check "pause pauses every running one" '[ "$CODE" = 0 ] && [ "$(state sbx_00000000000000c1)" = paused ] && [ "$(state sbx_00000000000000c2)" = paused ]'
SEED='[{"id":"sbx_00000000000000c3","state":"running","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}},
       {"id":"sbx_00000000000000c4","state":"paused","template":"prized-base-v1","labels":{"amp-orb":"'$ID'"},"resources":{}}]' fresh two-delete FAKE_DELETE_PAGE=1
job delete
check "delete takes every page: both gone" '[ "$CODE" = 0 ] && [ "$(sandboxes)" = 0 ] && [ "$(requests ".method == \"DELETE\"")" = 2 ]'
SEED=
FAULTS='[{"method":"GET","path":"/api/v1/sandboxes","delayAnswer":1,"times":2}]' fresh race
job spawn & RACE1=$!
sleep 0.3
job spawn
CODE2=$CODE
wait "$RACE1" || true
SBX=$(jq -r '.[0].id // empty' "$S/state.json"); H=$S/$SBX/home
wait_seen 2
check "two spawns at once: both end on one sandbox" \
  '[ "$CODE2" = 0 ] && [ "$(sandboxes)" = 1 ] && [ "$(requests ".method == \"POST\" and .path == \"/api/v1/sandboxes\"")" = 2 ] && [ "$(grep -c "^argv=" <<<"$(seen)")" = 2 ]'
FAULTS=

echo "# only this orb's sandboxes"
SEED='[{"id":"sbx_00000000000000a1","state":"running","template":"prized-base-v1","labels":{"amp-orb":"amp-T-another"},"resources":{}}]' fresh others
job spawn
job delete
check "another orb's sandbox is never touched" '[ "$(sandboxes)" = 1 ] && [ "$(state sbx_00000000000000a1)" = running ] && [ "$(requests ".path | contains(\"sbx_00000000000000a1\")")" = 0 ]'
SEED=

echo "# refusals: nothing is created"
FAULTS='[{"method":"POST","path":"/api/v1/sandboxes","status":409,"times":1,"body":{"error":"insufficient_balance","message":"Top up to start sandboxes"}}]' fresh broke
job spawn
check "Prized refuses the create: exit 1, its error named" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 0 ] && grep -q "409 insufficient_balance: Top up to start sandboxes" "$S/out"'
FAULTS=
fresh refusals
job spawn ORB_TOKEN=
check "no ORB_TOKEN: refused" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 0 ]'
job spawn PRIZED_API_KEY=
check "no PRIZED_API_KEY: refused, says where to get one" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 0 ] && grep -q "prized.dev/dashboard/workspace/api-keys" "$S/out"'
job spawn PRIZED_API_KEY=dcp_wrong
check "a wrong key: exit 1, 401 named" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 0 ] && grep -q "401 unauthorized" "$S/out"'
job spawn 'ORB_ID=amp-T-1;touch pwned'
check "an unexpected orb id: refused" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 0 ] && [ ! -e pwned ]'
job spawn PRIZED_ORB_VCPU=4x
check "a shape that isn't a number: refused" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 0 ]'
job spawn 'PRIZED_ORB_LABELS=team=web;x'
check "labels that aren't key=value pairs: refused" '[ "$CODE" = 1 ] && [ "$(sandboxes)" = 0 ]'
job resume
check "an unknown operation: exit 1" '[ "$CODE" = 1 ] && grep -q "unknown ORB_OPERATION" "$S/out"'
fresh no-install STUB_CURL_FAIL=1
job spawn
check "Amp's installer fails: exit 1, says so" '[ "$CODE" = 1 ] && grep -q "could not install the Amp CLI" "$S/out" && grep -q "Could not resolve host" "$S/out"'

stop_fake
echo
if [ "$FAILS" = 0 ]; then echo "all passed"; else echo "$FAILS failed"; exit 1; fi
