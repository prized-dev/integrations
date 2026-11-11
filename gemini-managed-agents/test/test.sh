#!/usr/bin/env bash
# check() evals its single-quoted condition later, so variables set here are used there.
# shellcheck disable=SC2016,SC2034
# test.sh: runs provision.sh the way `gemini-api environments poll --provision-cmd` runs it, against
# a stand-in for Google's side (mock_google.py: the poller's handoff, the endpoint a worker attaches
# to, and Prized's resume route), a stand-in for the prized CLI (fake-prized) and a stub worker in
# each fake sandbox (stub-gemini-api). No network, no keys, no real sandboxes.
#
#   test/test.sh              # every scenario (ONLY="1 4" for some of them; KEEP_TMP=1 keeps the files)
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT=${PROVISION:-$HERE/../provision.sh}  # PROVISION=/path/to/another/provision.sh tests that one
SCRIPT=$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")

KEY=ek_test_environment_key_0123456789
PKEY=dcp_0000000000000000000000000000000000000000000000ab
FAILS=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; FAILS=$((FAILS + 1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
want() { [ -z "${ONLY:-}" ] || [[ " $ONLY " == *" $1 "* ]]; }  # ONLY="1 4" runs just those scenarios
wait_for() { local n=$1; shift; for _ in $(seq "$((n * 10))"); do eval "$*" && return 0; sleep 0.1; done; return 1; }

T=$(mktemp -d)
if [ -n "${KEEP_TMP:-}" ]; then echo "temp: $T" >&2; else trap 'rm -rf "$T"' EXIT; fi
# The gemini-api release the fake curl serves: the stub, as a reproducible tarball whose sha256
# provision.sh is told to expect; and a different "gemini-api" with another hash, which must never run.
tarball() { mkdir -p "$T/$1" && cp "$2" "$T/$1/gemini-api" && chmod 755 "$T/$1/gemini-api" &&
  tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -cf - -C "$T/$1" gemini-api | gzip -n > "$T/$1.tgz"; }
printf '#!/bin/sh\ntouch "$HOME/EVIL-CLI-RAN"\n' > "$T/evil-cli"
tarball stub "$HERE/stub-gemini-api" && tarball evil "$T/evil-cli"
export STUB_CLI_TGZ=$T/stub.tgz STUB_EVIL_TGZ=$T/evil.tgz
GEMINI_API_SHA256=$(sha256sum "$STUB_CLI_TGZ" | cut -d' ' -f1)
export GEMINI_API_SHA256

# run_mock DIR QUEUE_JSON: starts the mock (poller and server) in DIR, in a process group of its own.
run_mock() {
  local dir=$1
  mkdir -p "$dir/bin" "$dir/prized" "$dir/home"
  ln -sf "$HERE/fake-prized" "$dir/bin/prized"
  printf '%s' "$2" > "$dir/queue.json"
  (cd "$dir" && exec setsid env PATH="$dir/bin:$PATH" HOME="$dir/home" FAKE_PRIZED_ROOT="$dir/prized" \
    GEMINI_PRIZED_STATE="$dir/state" PRIZED_API_KEY="$PKEY" GEMINI_ENVIRONMENT_KEY="$KEY" GEMINI_API_KEY="AIza-test-$KEY" \
    python3 -I "$HERE/mock_google.py" "$dir/queue.json" "$dir" > "$dir/mock.log" 2>&1) &
  MOCK=$!
  wait_for 10 "[ -s $dir/port ]" || bad "the mock did not start"
}
finish() {
  local p
  kill -- "-$MOCK" 2>/dev/null; wait "$MOCK" 2>/dev/null
  # Anything a scenario left running in its fake sandboxes; a watcher still attached exits once its worker is gone.
  for p in "${D:-/nonexistent}"/prized/sbx_*/procs/*.pid; do [ -s "$p" ] && kill -KILL -- "-$(cat "$p")" 2>/dev/null; done
  return 0
}
queue() { printf '{"prized_key":"%s","provision_cmd":"%s","provision_timeout":%s,"max_tries":%s,%s"items":[%s]}' "$PKEY" "$SCRIPT" "${TIMEOUT:-60}" "${TRIES:-1}" "${EXTRA:-}" "$1"; }
# item ID ENVIRONMENT TOKEN [AFTER_FILE] [MORE_JSON]
item() { printf '{"id":"%s","environment":"%s","token":"%s"%s%s}' "$1" "$2" "$3" "${4:+,\"after_file\":\"$4\"}" "${5:+,$5}"; }
res() { jq -r --arg i "$2" ".[\$i] | $3" "$1/results.json" 2>/dev/null; }
provisioned() { [ "$(res "$1" "$2" 'last | .exit')" = 0 ]; }
gave_up() { [ "$(res "$1" "$2" length)" = "$3" ] && [ "$(res "$1" "$2" 'last | .exit')" != 0 ]; }
completed() { [ -s "$1/completes.json" ] && jq -e --arg i "$2" 'has($i)' "$1/completes.json" >/dev/null; }
watched() { grep -q "interaction ended" "$1/state/logs/$2.log" 2>/dev/null; }
sbx_of() { jq -r --arg e "$2" 'select(.labels["gemini-environment"] == $e and .state != "deleted") | .id' "$1"/prized/sbx_*/meta.json 2>/dev/null | tail -1; }
sbx_count() { jq -r --arg e "$2" 'select(.labels["gemini-environment"] == $e) | .id' "$1"/prized/sbx_*/meta.json 2>/dev/null | wc -l; }
meta() { jq -r "$3" "$1/prized/$2/meta.json"; }
set_meta() { jq "$3" "$1/prized/$2/meta.json" > "$1/m.json" && mv "$1/m.json" "$1/prized/$2/meta.json"; }
# seen DIR ID: where the stub worker of interaction ID recorded what it saw, in whichever sandbox it ran.
seen() { local d; for d in "$1"/prized/sbx_*/home/.gemini-managed-agents/seen/"$2"; do [ -d "$d" ] && { echo "$d"; return; }; done; echo "$1/nowhere/$2"; }
creates() { grep -c '"sandbox", "create"' "$1/prized/calls.log"; }
# kept_awake DIR ID: a follow was attached from the handoff until the worker ended.
kept_awake() {
  local end; end=$(cat "$(seen "$1" "$2")/end") || return 1
  jq -s -e --argjson handoff "$(res "$1" "$2" 'last | .end')" --argjson end "$end" \
    'any(.[]; (.dropped | not) and .start <= $handoff + 4 and .end >= $end - 0.5)' "$1/prized/follows.log" >/dev/null
}
before() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a < b) }'; }
dump() { [ "$FAILS" = "$1" ] || { echo "--- results"; jq -c . "$D/results.json" 2>/dev/null | cut -c1-3000; echo "--- logs"; tail -n 30 "$D"/state/logs/*.log "$D/mock.log" 2>/dev/null; }; }

if want 1; then
echo "== 1. two interactions on one environment, the sandbox paused in between"
export STUB_RUN_SECONDS=12
D=$T/one; f=$FAILS
run_mock "$D" "$(queue "$(item s1-a envA lt_v1_token-one),$(item s1-b envA lt_v1_token-two go-2)")"
wait_for 60 'completed "$D" s1-a && watched "$D" s1-a' || bad "s1-a did not finish in 60 s"
A=$(sbx_of "$D" envA)
set_meta "$D" "$A" '.state = "paused"'; touch "$D/go-2"   # it paused itself after its idle time
wait_for 60 'completed "$D" s1-b && watched "$D" s1-b' || bad "s1-b did not finish in 60 s"
finish
a=$(seen "$D" s1-a) b=$(seen "$D" s1-b)
check "the first try of each provision exited 0" '[ "$(res "$D" s1-a length)" = 1 ] && provisioned "$D" s1-a && provisioned "$D" s1-b'
check "the script returned while the worker was still running, and let go of its stderr" \
  'before "$(res "$D" s1-a "last | .end")" "$(cat "$a/end")" && [ "$(res "$D" s1-a "last | .stderr_held")" = false ]'
check "a follow stayed attached until each worker ended, so the sandbox was never idle under it" 'kept_awake "$D" s1-a && kept_awake "$D" s1-b'
check "one sandbox for the environment: created once, labelled, with --auto-pause 2m --auto-delete 1d from prized-base-v1" \
  '[ "$(creates "$D")" = 1 ] && [ "$(meta "$D" "$A" '\''.labels["gemini-environment"] + " " + .flags["auto-pause"] + " " + .flags["auto-delete"] + " " + .flags.template'\'')" = "envA 2m 1d prized-base-v1" ]'
check "the second interaction resumed it through Prized's resume route, and found the first one's files" \
  '[ "$(meta "$D" "$A" .resume_calls)" = 1 ] && grep -qx made-by-s1-a "$b/files-before"'
check "the script never paused the sandbox (it pauses itself)" '! grep -q "\"sandbox\", \"pause\"" "$D/prized/calls.log"'
check "each worker got its own lease token, and attached with it and nothing stronger" \
  '[ "$(cat "$a/token")" = lt_v1_token-one ] && [ "$(cat "$b/token")" = lt_v1_token-two ] && [ "$(jq -c "[.[][] | [.ok, .stronger_credential]] | unique" "$D/attaches.json")" = "[[true,false]]" ]'
check "the worker ran gemini-api environments serve in the workspace, pointed at the lease's server" \
  'grep -q "^environments serve --interaction-id s1-a --lease-token --server-url http://127.0.0.1:$(cat "$D/port") --root-dir $D/prized/$A/home/work --workdir $D/prized/$A/home/work$" "$a/args"'
check "no lease token on a command line of this machine, or in its logs" \
  '! grep -q "lt_v1_token" "$D/prized/calls.log" && ! grep -rq "lt_v1_token" "$D/state" "$D/mock.log" "$D/results.json"'
check "no lease file is left in the sandbox" '[ -z "$(find "$D/prized" -name lease)" ]'
check "no Gemini key reached the prized CLI, a worker, or any file of a sandbox" \
  '! grep -q "\"gemini_credential\": true" "$D/prized/calls.log" && ! grep -Eq "^(GEMINI_API_KEY|GEMINI_ENVIRONMENT_KEY|PRIZED_API_KEY)=" "$a/env" && ! grep -rqF "$KEY" "$D/prized" && ! grep -rqF "$PKEY" "$D/prized"'
check "gemini-api was downloaded once, for the pinned release" '[ "$(wc -l < "$D/prized/$A/home/curl.log")" = 1 ]'
check "this machine kept a log per interaction, and the worker's own" \
  'grep -q "handed off" "$D/state/logs/s1-a.log" && grep -q "worker exit 0" "$D/state/logs/s1-a.log" && grep -q "Completed interaction session" "$D/state/logs/s1-a.worker.log"'
dump "$f"
fi

if want 2; then
echo "== 2. two interactions of a new environment arrive together"
export STUB_RUN_SECONDS=12 FAKE_CREATE_DELAY=1
D=$T/two; f=$FAILS
run_mock "$D" "$(queue "$(item s2-a envB lt_v1_b-one),$(item s2-b envB lt_v1_b-two)")"
wait_for 90 'completed "$D" s2-a && completed "$D" s2-b && watched "$D" s2-a && watched "$D" s2-b' || bad "both did not finish in 90 s"
finish
unset FAKE_CREATE_DELAY
a=$(seen "$D" s2-a) b=$(seen "$D" s2-b)
check "they share one sandbox: one create" '[ "$(creates "$D")" = 1 ] && [ "$(sbx_count "$D" envB)" = 1 ]'
check "both ran there at the same time, each with its own token" \
  'before "$(cat "$a/start")" "$(cat "$b/end")" && before "$(cat "$b/start")" "$(cat "$a/end")" && [ "$(cat "$a/token")" = lt_v1_b-one ] && [ "$(cat "$b/token")" = lt_v1_b-two ]'
check "neither worker was stopped, and both were kept awake to the end" '[ ! -e "$a/term" ] && [ ! -e "$b/term" ] && kept_awake "$D" s2-a && kept_awake "$D" s2-b'
check "the lock was released" '[ -z "$(ls -A "$D/state/locks")" ]'
dump "$f"
fi

if want 3; then
echo "== 3. two environments, one naming its template; a poller that did not scrub its keys"
export STUB_RUN_SECONDS=1
D=$T/three; f=$FAILS
# envC's lock was left by a script the poller killed: its holder's pid is of a process that is gone.
mkdir -p "$D/state/locks/envC"; (exit 0) & wait $!; echo "$!" > "$D/state/locks/envC/pid"
run_mock "$D" "$(EXTRA='"leak_credentials":true,' queue "$(item s3-a envC lt_v1_c '' '"custom_config":{"prized":{"template":"prized-agent-v1"}}'),$(item s3-b envD lt_v1_d '' '"custom_config":{"daytona":{"snapshot":"x"}}'),$(item s3-c envE lt_v1_e '' '"custom_config":"not-an-object"')")"
wait_for 90 'completed "$D" s3-a && completed "$D" s3-b && completed "$D" s3-c' || bad "the three did not finish in 90 s"
wait_for 20 'watched "$D" s3-a && watched "$D" s3-b && watched "$D" s3-c'
finish
check "a sandbox each" '[ "$(creates "$D")" = 3 ] && [ "$(sbx_of "$D" envC)" != "$(sbx_of "$D" envD)" ]'
check "a lock whose holder was killed was taken over on the first try, and released" \
  '[ "$(res "$D" s3-a length)" = 1 ] && provisioned "$D" s3-a && [ -z "$(ls -A "$D/state/locks")" ]'
check "custom_config.prized.template chose the template; somebody else's custom_config was ignored" \
  '[ "$(meta "$D" "$(sbx_of "$D" envC)" .flags.template)" = prized-agent-v1 ] && [ "$(meta "$D" "$(sbx_of "$D" envD)" .flags.template)" = prized-base-v1 ] && [ "$(meta "$D" "$(sbx_of "$D" envE)" .flags.template)" = prized-base-v1 ]'
check "keys left in the script's environment still never reached the prized CLI or a worker" \
  '! grep -q "\"gemini_credential\": true" "$D/prized/calls.log" && ! grep -Eq "^(GEMINI_API_KEY|GEMINI_ENVIRONMENT_KEY)=" "$(seen "$D" s3-a)/env" && ! grep -rqF "$KEY" "$D/prized"'
dump "$f"
fi

if want 4; then
echo "== 4. a lease Google no longer honours, a worker that fails once connected, and one that never starts"
# The worker logs its first line before it connects, as the real one does, and is refused 3 s later.
export STUB_RUN_SECONDS=1 STUB_CONNECT_SECONDS=3
D=$T/four; f=$FAILS
run_mock "$D" "$(queue "$(item s4-a envF lt_v1_stale '' '"reject_attach":true')")"
wait_for 60 'gave_up "$D" s4-a 1' || bad "the provision did not fail in 60 s"
finish
unset STUB_CONNECT_SECONDS
F=$(sbx_of "$D" envF)
check "a refused worker fails the handoff although it logged its first line, with the end of its log" \
  'res "$D" s4-a "last | .stderr" | grep -q "exited (1) right after it started" && res "$D" s4-a "last | .stderr" | grep -q "attach failed: HTTP 403"'
check "nothing was left behind: no lease, no pid file, no watcher; the sandbox is kept" \
  '[ -z "$(find "$D/prized" -name lease -o -name pid)" ] && [ ! -e "$D/prized/follows.log" ] && [ "$(meta "$D" "$F" .state)" = running ]'
dump "$f"
export STUB_FAIL_AFTER_ATTACH=1
D=$T/four-b; f=$FAILS
run_mock "$D" "$(queue "$(item s4-b envG lt_v1_g)")"
wait_for 60 'gave_up "$D" s4-b 1' || bad "the provision did not fail in 60 s"
finish
unset STUB_FAIL_AFTER_ATTACH
check "a worker that dies right after it connected fails the handoff, so the poller tries again" \
  'res "$D" s4-b "last | .stderr" | grep -q "exited (3) right after it started" && [ ! -e "$D/prized/follows.log" ]'
dump "$f"
D=$T/four-c; f=$FAILS
mkdir -p "$D"; touch "$D/hang-once"
export STUB_NEVER_ATTACH_ONCE=$D/hang-once START_TIMEOUT=4
run_mock "$D" "$(queue "$(item s4-c envM lt_v1_m)")"
wait_for 60 'gave_up "$D" s4-c 1' || bad "the provision did not fail in 60 s"
finish
unset STUB_NEVER_ATTACH_ONCE START_TIMEOUT
check "a worker that logs nothing within START_TIMEOUT fails the try, and is stopped" \
  'res "$D" s4-c "last | .stderr" | grep -q "did not start on the interaction within 4s" && [ -e "$(seen "$D" s4-c)/term" ] && [ ! -e "$D/prized/follows.log" ]'
dump "$f"
fi

if want 5; then
echo "== 5. the gemini-api download fails once, then serves another binary once"
export STUB_RUN_SECONDS=1
D=$T/five; f=$FAILS
mkdir -p "$D"; touch "$D/fail-once" "$D/evil-once"
export STUB_CURL_FAIL_ONCE=$D/fail-once STUB_EVIL_ONCE=$D/evil-once
run_mock "$D" "$(TRIES=3 queue "$(item s5-a envH lt_v1_h)")"
wait_for 90 'completed "$D" s5-a && watched "$D" s5-a' || bad "s5-a did not finish in 90 s"
finish
unset STUB_CURL_FAIL_ONCE STUB_EVIL_ONCE
H=$(sbx_of "$D" envH)
check "the failed download failed the first try, before any worker" \
  '[ "$(res "$D" s5-a ".[0].exit")" != 0 ] && res "$D" s5-a ".[0].stderr" | grep -q "curl: (22)" && res "$D" s5-a ".[0].stderr" | grep -q "could not prepare"'
check "the binary with the wrong sha256 was refused and never run" \
  '[ "$(res "$D" s5-a ".[1].exit")" != 0 ] && res "$D" s5-a ".[1].stderr" | grep -q "FAILED" && [ ! -e "$D/prized/$H/home/EVIL-CLI-RAN" ]'
check "the third try installed the right one in the same sandbox and ran" '[ "$(res "$D" s5-a length)" = 3 ] && provisioned "$D" s5-a && [ "$(creates "$D")" = 1 ]'
check "no download was left behind" '! ls -d "$D/prized/$H"/home/.local/bin/.gemini-api.* >/dev/null 2>&1'
dump "$f"
fi

if want 6; then
echo "== 6. the environment's sandbox won't resume: a quota refusal, then max_runtime"
export STUB_RUN_SECONDS=1
D=$T/six; f=$FAILS
run_mock "$D" "$(queue "$(item s6-a envI lt_v1_i1),$(item s6-b envI lt_v1_i2 go-b),$(item s6-c envI lt_v1_i3 go-c)")"
wait_for 60 'completed "$D" s6-a && watched "$D" s6-a' || bad "s6-a did not finish in 60 s"
I1=$(sbx_of "$D" envI)
set_meta "$D" "$I1" '.state = "paused" | .resume_fails = true | .resume_reason = "running_vcpu"'; touch "$D/go-b"
wait_for 60 'gave_up "$D" s6-b 1' || bad "s6-b did not fail in 60 s"
c2=$(meta "$D" "$I1" .resume_calls) n2=$(sbx_count "$D" envI)
set_meta "$D" "$I1" '.resume_reason = "max_runtime"'; touch "$D/go-c"
wait_for 60 'completed "$D" s6-c && watched "$D" s6-c' || bad "s6-c did not finish in 60 s"
finish
check "a quota refusal was retried 3 times, then the try failed with no new sandbox and no worker" \
  '[ "$c2" = 3 ] && [ "$n2" = 1 ] && [ ! -e "$(seen "$D" s6-b)/start" ] && res "$D" s6-b "last | .stderr" | grep -q "could not be resumed now"'
check "max_runtime: the next interaction ran in a new sandbox, and the old one was kept, not deleted" \
  '[ "$(sbx_count "$D" envI)" = 2 ] && [ "$(sbx_of "$D" envI)" != "$I1" ] && [ -e "$(seen "$D" s6-c)/end" ] && [ "$(meta "$D" "$I1" .state)" = paused ]'
dump "$f"
fi

if want 7; then
echo "== 7. leases the script must refuse, run directly"
D=$T/seven; f=$FAILS
mkdir -p "$D/bin" "$D/prized" "$D/home"; ln -sf "$HERE/fake-prized" "$D/bin/prized"
direct() { printf '%s' "$1" | env PATH="$D/bin:$PATH" HOME="$D/home" FAKE_PRIZED_ROOT="$D/prized" GEMINI_PRIZED_STATE="$D/state" PRIZED_API_KEY="$PKEY" "$SCRIPT" 2>"$D/err"; }
refused() { ! direct "$1" && grep -q "$2" "$D/err" && [ ! -e "$D/prized/calls.log" ]; }
good='"schema_version":"v1","interaction_id":"s7-a","environment_id":"envJ","lease_token":"lt_v1_j"'
check "not JSON" 'refused "hello" "not a lease"'
check "another schema version" 'refused "{\"schema_version\":\"v2\",\"interaction_id\":\"s7-a\",\"lease_token\":\"x\"}" "unsupported lease schema v2"'
check "no lease token" 'refused "{\"schema_version\":\"v1\",\"interaction_id\":\"s7-a\"}" "no usable lease_token"'
check "an interaction id that is not an id" 'refused "{\"interaction_id\":\"a;touch $D/pwned\",\"lease_token\":\"x\"}" "no usable interaction_id" && [ ! -e "$D/pwned" ]'
check "an environment id that is not an id" 'refused "{\"interaction_id\":\"s7-a\",\"lease_token\":\"x\",\"environment_id\":\"../x\"}" "unexpected environment_id"'
check "a server_url with shell in it, or plain http to another host" \
  'refused "{$good,\"server_url\":\"https://x.example/\$(touch $D/pwned)\"}" "unexpected server_url" && refused "{$good,\"server_url\":\"http://10.0.0.5:8080\"}" "unexpected server_url" && [ ! -e "$D/pwned" ]'
check "a template that is not a template name" 'refused "{$good,\"environment_config\":{\"custom_config\":{\"prized\":{\"template\":\"a b;c\"}}}}" "unexpected custom_config.prized.template"'
check "no Prized key: the message says how to get one" \
  '! printf "{$good}" | env -u PRIZED_API_KEY PATH="$D/bin:$PATH" HOME="$D/home" "$SCRIPT" 2>"$D/err" && grep -q "auth.md?via=gemini-managed-agents" "$D/err"'
check "no lease token in any of the refusals' logs" '! grep -rq "lt_v1_j" "$D/state" 2>/dev/null'
dump "$f"
fi

if want 8; then
echo "== 8. the poller kills a provision that ran out of time, then tries again"
export STUB_RUN_SECONDS=2 START_TIMEOUT=60
D=$T/eight; f=$FAILS
mkdir -p "$D"; touch "$D/hang-once"
export STUB_NEVER_ATTACH_ONCE=$D/hang-once
run_mock "$D" "$(TIMEOUT=8 TRIES=2 queue "$(item s8-a envK lt_v1_k)")"
wait_for 90 'completed "$D" s8-a && watched "$D" s8-a' || bad "s8-a did not finish in 90 s"
finish
unset STUB_NEVER_ATTACH_ONCE START_TIMEOUT
k=$(seen "$D" s8-a)
check "the first try was killed at the poller's timeout, the second handed off" \
  '[ "$(res "$D" s8-a ".[0].timed_out")" = true ] && [ "$(res "$D" s8-a length)" = 2 ] && provisioned "$D" s8-a'
check "the second try stopped the worker the first one left, in the same sandbox, and its own worker ran with the new lease" \
  '[ -e "$k/hung" ] && [ -e "$k/term" ] && [ -e "$k/end" ] && [ "$(creates "$D")" = 1 ] && [ "$(cat "$k/token")" = lt_v1_k.try2 ]'
dump "$f"
fi

if want 9; then
echo "== 9. the watcher's connection drops while the worker runs"
export STUB_RUN_SECONDS=16
D=$T/nine; f=$FAILS
mkdir -p "$D"; touch "$D/drop-once"
export FAKE_FOLLOW_DROP_ONCE=$D/drop-once
run_mock "$D" "$(queue "$(item s9-a envL lt_v1_l)")"
wait_for 90 'completed "$D" s9-a && watched "$D" s9-a' || bad "s9-a did not finish in 90 s"
finish
unset FAKE_FOLLOW_DROP_ONCE
check "it attached again and stayed until the worker ended" \
  '[ ! -e "$D/drop-once" ] && [ "$(jq -s "[.[] | select(.dropped)] | length" "$D/prized/follows.log")" = 1 ] && kept_awake "$D" s9-a && grep -q "worker exit 0" "$D/state/logs/s9-a.log"'
dump "$f"
# The same drop, and the status read that follows it is refused for a reason that has nothing to do with
# the sandbox being gone (the CLI exits 5 for a rate limit as it does for a missing sandbox).
D=$T/nine-b; f=$FAILS
mkdir -p "$D"; touch "$D/drop-once" "$D/refuse-once"
export FAKE_FOLLOW_DROP_ONCE=$D/drop-once FAKE_STATUS_REFUSED_ONCE=$D/refuse-once
run_mock "$D" "$(queue "$(item s9-b envO lt_v1_o)")"
wait_for 90 'completed "$D" s9-b && watched "$D" s9-b' || bad "s9-b did not finish in 90 s"
finish
unset FAKE_FOLLOW_DROP_ONCE FAKE_STATUS_REFUSED_ONCE
check "a refused status read is not taken for a deleted sandbox: the watcher stayed to the end" \
  '[ ! -e "$D/refuse-once" ] && ! grep -q "is gone" "$D/state/logs/s9-b.log" && kept_awake "$D" s9-b && grep -q "worker exit 0" "$D/state/logs/s9-b.log"'
dump "$f"
fi

if want 10; then
echo "== 10. the sandbox is deleted while its worker runs"
export STUB_RUN_SECONDS=120
D=$T/ten; f=$FAILS
run_mock "$D" "$(queue "$(item s10-a envN lt_v1_n),$(item s10-b envP lt_v1_p)")"
wait_for 60 'provisioned "$D" s10-a && provisioned "$D" s10-b' || bad "the two were not handed off in 60 s"
N=$(sbx_of "$D" envN) P=$(sbx_of "$D" envP)
set_meta "$D" "$N" '.state = "deleted"'    # gone: prized answers "no sandbox ... in this workspace"
set_meta "$D" "$P" '.state = "deleting"'   # on its way out, as right after `prized sandbox rm`
wait_for 30 'grep -q "is gone" "$D/state/logs/s10-a.log" && grep -q "is gone" "$D/state/logs/s10-b.log"' || bad "the watchers did not notice in 30 s"
finish
check "each watcher stops instead of trying for the rest of RUN_TIMEOUT" \
  'grep -q "$N is gone (deleted)" "$D/state/logs/s10-a.log" && grep -q "$P is gone (deleting)" "$D/state/logs/s10-b.log" && ! grep -q "interaction ended" "$D"/state/logs/s10-*.log'
dump "$f"
fi

echo "== $FAILS failed"
[ "$FAILS" = 0 ]
