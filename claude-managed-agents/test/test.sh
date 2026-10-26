#!/usr/bin/env bash
# check() evals its single-quoted condition later, so variables set here are used there.
# shellcheck disable=SC2016,SC2034
# test.sh: runs on-work.sh under the real `ant beta:worker poll --on-work`, against a stand-in for
# Anthropic's work queue and Prized's resume route (mock_api.py) and for the prized CLI (fake-prized),
# with a stub worker in each fake sandbox. No network and no real sandboxes.
#
#   ANT=/path/to/ant test/test.sh                  # the checks below (ONLY="4 9" for some of them)
#   ANT=/path/to/ant test/test.sh --contrast FILE   # run another on-work script, report stop timing
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ANT=${ANT:-ant}
SCRIPT=${ON_WORK:-$HERE/../on-work.sh}
SCRIPT=$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")
CONTRAST=''
[ "${1:-}" != --contrast ] || { SCRIPT=$(cd "$(dirname "$2")" && pwd)/$(basename "$2"); CONTRAST=1; }
ANT=$(command -v "$ANT") || { echo "set ANT to an ant binary (v2.3.0 tested)" >&2; exit 2; }
case $ANT in /*) ;; *) ANT=$PWD/$ANT ;; esac
"$ANT" --version >&2

KEY=sk-ant-oat01-test-environment-key-0123456789
PKEY=dcp_0000000000000000000000000000000000000000000000ab
FAILS=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; FAILS=$((FAILS + 1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

# run_poller DIR QUEUE_JSON [N]: starts the mock API and N pollers (default 1) in DIR, each in a process
# group of its own (so finish can end everything they started); sets MOCK, POLLERS and POLLER (the first).
run_poller() {
  local dir=$1 n=${3:-1} i
  mkdir -p "$dir/bin" "$dir/prized"
  ln -sf "$HERE/fake-prized" "$dir/bin/prized"
  printf '%s' "$2" > "$dir/queue.json"
  python3 -I "$HERE/mock_api.py" "$dir/queue.json" "$dir" & MOCK=$!
  for _ in $(seq 100); do [ -s "$dir/port" ] && break; sleep 0.05; done
  POLLERS=()
  local url
  url="http://127.0.0.1:$(cat "$dir/port")"
  local env=(PATH="$dir/bin:$PATH" FAKE_PRIZED_ROOT="$dir/prized" ANTHROPIC_BASE_URL="$url" PRIZED_API_URL="$url/api/v1"
    PRIZED_API_KEY="$PKEY" ANTHROPIC_ENVIRONMENT_ID=env_test ANTHROPIC_ENVIRONMENT_KEY="$KEY")
  if [ -n "${README_WRAPPER:-}" ]; then  # the README's line for several pollers, run as written
    (cd "$dir" && exec setsid env "${env[@]}" bash -c "for i in \$(seq $n); do \"$ANT\" beta:worker poll --on-work \"$SCRIPT\" --workdir . & done; wait" \
      > "$dir/poller1.log" 2>&1) &
    POLLERS+=($!)
  else
    for i in $(seq "$n"); do
      (cd "$dir" && exec setsid env "${env[@]}" "$ANT" beta:worker poll --on-work "$SCRIPT" --workdir "$dir" > "$dir/poller$i.log" 2>&1) &
      POLLERS+=($!)
    done
  fi
  POLLER=${POLLERS[0]}
}
stopped() { [ -s "$1/stops.json" ] && jq -e --arg w "$2" 'has($w)' "$1/stops.json" >/dev/null; }
wait_for() { local n=$1; shift; for _ in $(seq "$((n * 10))"); do eval "$*" && return 0; sleep 0.1; done; return 1; }
finish() {
  local p
  for p in "${POLLERS[@]}"; do kill -- "-$p" 2>/dev/null; wait "$p" 2>/dev/null; done
  kill "$MOCK" 2>/dev/null; wait "$MOCK" 2>/dev/null
  # Workers a broken on-work.sh left behind in this scenario's fake sandboxes (frozen ones included).
  for p in "${D:-/nonexistent}"/prized/sbx_*/home/.anthropic/*/pid; do [ -s "$p" ] && kill -KILL -- "-$(cat "$p")" 2>/dev/null; done
  return 0
}
sbx_of() { jq -r --arg s "$2" 'select(.labels["anthropic-session"] == $s) | .id' "$1"/prized/sbx_*/meta.json 2>/dev/null | head -1; }
sbx_count() { jq -r --arg s "$2" 'select(.labels["anthropic-session"] == $s) | .id' "$1"/prized/sbx_*/meta.json 2>/dev/null | wc -l; }
meta() { jq -r "$3" "$1/prized/$2/meta.json"; }
# seen DIR WORK: where the stub worker of WORK recorded what it saw, in whichever sandbox it ran.
seen() { local d; for d in "$1"/prized/sbx_*/home/.anthropic/seen/"$2"; do [ -d "$d" ] && { echo "$d"; return; }; done; echo "$1/nowhere/$2"; }
stop_at() { jq -r --arg w "$2" '.[$w]' "$1/stops.json"; }
# held WORK DIR: the worker finished on its own, and the poller posted the stop only after that.
held() { [ -s "$2/end" ] && [ ! -e "$2/term" ] && awk -v s="$(stop_at "$D" "$1")" -v e="$(cat "$2/end")" 'BEGIN { exit !(s >= e) }'; }
logs() { cat "$1"/poller*.log; }
# never_paused DIR: on-work never paused a sandbox (they pause themselves), so no live worker was frozen.
never_paused() { ! grep -q '"pause"' "$1/prized/calls.log" && ! grep -q '"froze_live_workers": [1-9]' "$1"/prized/sbx_*/meta.json; }
# the prized CLI never ran with the environment key in its environment
no_key_in_cli() { ! grep -q '"envkey": true' "$1/prized/calls.log"; }
dump() { [ "$FAILS" = "$1" ] || { echo "--- poller logs"; logs "$D" | tail -40; }; }
queue() { local items=$1; printf '{"environment_id":"env_test","environment_key":"%s","prized_key":"%s","items":[%s]}' "$KEY" "$PKEY" "$items"; }
item() { printf '{"id":"%s","session":"%s"%s%s}' "$1" "$2" "${3:+,\"secret\":\"$3\"}" "${4:+,\"after_file\":\"$4\"}"; }
want() { [ -z "${ONLY:-}" ] || [[ " $ONLY " == *" $1 "* ]]; }  # ONLY="1 4" runs just those scenarios
set_meta() { jq "$3" "$1/prized/$2/meta.json" > "$1/m.json" && mv "$1/m.json" "$1/prized/$2/meta.json"; }

T=$(mktemp -d)
if [ -n "${KEEP_TMP:-}" ]; then echo "temp: $T" >&2; else trap 'rm -rf "$T"' EXIT; fi
S1=$(printf '{"sessions_token":"sess-token-one"}' | base64 -w0 | tr '+/' '-_' | tr -d '=')
S2=$(printf '{"sessions_token":"sess-token-two"}' | base64 -w0 | tr '+/' '-_' | tr -d '=')
# The ant release the fake curl serves: the stub, as a reproducible tarball whose sha256 on-work.sh
# is told to expect; and a different "ant" with another hash, which must never run.
tarball() { mkdir -p "$T/$1" && cp "$2" "$T/$1/ant" && chmod 755 "$T/$1/ant" &&
  tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -cf - -C "$T/$1" ant | gzip -n > "$T/$1.tgz"; }
printf '#!/bin/sh\ntouch "$HOME/EVIL-ANT-RAN"\n' > "$T/evil-ant"
tarball stub "$HERE/stub-ant" && tarball evil "$T/evil-ant"
export STUB_ANT_TGZ=$T/stub.tgz STUB_EVIL_TGZ=$T/evil.tgz
ANT_SHA256=$(sha256sum "$STUB_ANT_TGZ" | cut -d' ' -f1)
export ANT_SHA256

if [ -n "$CONTRAST" ]; then
  export STUB_RUN_SECONDS=5 STUB_CURL_ANY=1
  run_poller "$T/c" "$(queue "$(item work_1 sesn_A "$S1")")"
  wait_for 60 stopped "$T/c" work_1 && wait_for 30 "[ -e \"\$(seen $T/c work_1)/end\" ]"
  finish
  s=$(seen "$T/c" work_1)
  echo "work_1: worker started $(cat "$s/start" 2>/dev/null), stop posted $(stop_at "$T/c" work_1), worker ended $(cat "$s/end" 2>/dev/null)"
  if [ ! -s "$s/end" ]; then echo "=> the worker never finished a run, so stop timing can't be compared"; exit 1; fi
  awk -v stop="$(stop_at "$T/c" work_1)" -v end="$(cat "$s/end")" 'BEGIN { if (stop < end) print "=> the poller stopped the work item " end - stop " s before the run ended"; else print "=> the poller held the work item until the run ended" }'
  exit 0
fi

if want 1; then
echo "== 1. two messages to one session, then a work item with no secret"
export STUB_RUN_SECONDS=3 STUB_TAIL_SECONDS=0
D=$T/one; f=$FAILS
run_poller "$D" "$(queue "$(item work_1 sesn_A "$S1"),$(item work_2 sesn_A "$S2"),$(item work_3 sesn_B)")"
wait_for 120 stopped "$D" work_3 || bad "the queue was not drained in 120 s"
finish
A=$(sbx_of "$D" sesn_A) w1=$(seen "$D" work_1) w2=$(seen "$D" work_2)
check "work_1 ran in a sandbox labelled with its session" '[ -n "$A" ] && [ -e "$w1/start" ]'
check "the poller held work_1 until its run ended: the worker finished, then the stop" 'held work_1 "$w1"'
check "work_2 held too" 'held work_2 "$w2"'
check "one sandbox for the session: created once, with --auto-pause 2m --auto-delete 1d" \
  '[ "$(grep -c "\"create\"" "$D/prized/calls.log")" = 1 ] && [ "$(meta "$D" "$A" '\''.flags["auto-pause"] + " " + .flags["auto-delete"]'\'')" = "2m 1d" ]'
check "the follow-up resumed it through Prized's resume route, with the first run's files" \
  '[ "$(meta "$D" "$A" .resume_calls)" = 1 ] && grep -qx made-by-work_1 "$w2/files-before"'
check "on-work never paused it (it pauses itself)" 'never_paused "$D"'
check "each run got its own work item's secret, in a 0600 file" \
  '[ "$(cat "$w1/secret")" = "$S1" ] && [ "$(cat "$w2/secret")" = "$S2" ] && [ "$(cat "$w1/secret-mode")" = 600 ]'
check "the worker got the session's ids" \
  'grep -qx ANTHROPIC_SESSION_ID=sesn_A "$w1/env" && grep -qx ANTHROPIC_WORK_ID=work_1 "$w1/env" && grep -qx ANTHROPIC_ENVIRONMENT_ID=env_test "$w1/env"'
check "the worker reached the queue's base URL" 'grep -q "^ANTHROPIC_BASE_URL=http://127.0.0.1:" "$w1/env"'
check "the environment key never entered a sandbox (env or any file)" \
  '! grep -q ANTHROPIC_ENVIRONMENT_KEY "$w1/env" && ! grep -rqF "$KEY" "$D/prized"'
check "and never reached the prized CLI's environment" 'no_key_in_cli "$D"'
check "no secret is left in the sandbox after the runs" '! ls "$D/prized/$A"/home/.anthropic/*/secret >/dev/null 2>&1'
check "no secret on a command line" '! grep -qF "$S1" "$D/prized/calls.log" && ! grep -qF "$KEY" "$D/prized/calls.log"'
check "a work item with no secret is refused, with no sandbox" '[ -z "$(sbx_of "$D" sesn_B)" ] && stopped "$D" work_3'
dump "$f"

fi
if want 2; then
echo "== 2. the poller is stopped mid-run"
export STUB_RUN_SECONDS=120
D=$T/two; f=$FAILS
run_poller "$D" "$(queue "$(item work_5 sesn_C "$S1")")"
wait_for 60 "[ -e \"\$(seen $D work_5)/start\" ]" || bad "the worker never started"
t0=$(date +%s)
kill -TERM "$POLLER"
wait_for 45 "! kill -0 $POLLER 2>/dev/null" || bad "the poller did not exit within 45 s of SIGTERM"
check "on SIGTERM the run ends within 45 s ($(($(date +%s) - t0)) s)" '! kill -0 "$POLLER" 2>/dev/null'
finish
check "the worker was stopped, and nothing paused the sandbox" '[ -e "$(seen "$D" work_5)/term" ] && never_paused "$D"'
dump "$f"

fi
if want 3; then
echo "== 3. Ctrl-C and the poller's SIGTERM land together, then a burst of both"
D=$T/three; f=$FAILS
run_poller "$D" "$(queue "$(item work_6 sesn_E "$S1")")"
wait_for 60 "[ -e \"\$(seen $D work_6)/start\" ]" || bad "the worker never started"
script_pid=$(pgrep -P "$POLLER" | head -1)
kill -INT "$script_pid"; kill -TERM "$POLLER"
for _ in $(seq 2000); do  # and a stream more, until the script is gone
  kill -INT "$script_pid" 2>/dev/null || break
  kill -TERM "$script_pid" 2>/dev/null || break
done
wait_for 45 "! kill -0 $POLLER 2>/dev/null" || bad "the poller did not exit within 45 s"
finish
check "cleanup still stopped the worker" '[ -e "$(seen "$D" work_6)/term" ]'
dump "$f"

fi
if want 4; then
echo "== 4. two pollers: the session's next message arrives while the last run's poller is cleaning up"
# work_r2 is handed out the moment work_r1's worker has done its work, as the real worker stops its
# work item then; work_r1's worker lingers 4 s more, so the second poller starts work_r2's worker in
# the same sandbox while the first poller is still waiting and then cleaning up. A pause freezes the
# sandbox's processes, as a real one does, so a poller that paused the sandbox when its own run ended,
# or stopped workers other than its own, would hit work_r2's worker.
export STUB_RUN_SECONDS=3 STUB_TAIL_SECONDS=4
D=$T/four; f=$FAILS
run_poller "$D" "$(queue "$(item work_r1 sesn_R "$S1"),$(item work_r2 sesn_R "$S2" "prized/sbx_*/home/.anthropic/seen/work_r1/end")")" 2
wait_for 90 'stopped "$D" work_r1 && stopped "$D" work_r2' || bad "both work items were not stopped within 90 s"
finish
R=$(sbx_of "$D" sesn_R) r1=$(seen "$D" work_r1) r2=$(seen "$D" work_r2)
check "the two runs overlapped in one sandbox (work_r2 started before work_r1's worker exited)" \
  '[ "$(sbx_count "$D" sesn_R)" = 1 ] && [ -e "$r2/start" ] && awk -v b="$(cat "$r2/start")" -v e="$(cat "$r1/end")" "BEGIN { exit !(b < e + 4) }"'
check "neither poller stopped or froze the other's worker" '[ ! -e "$r1/term" ] && [ ! -e "$r2/term" ] && never_paused "$D"'
check "both runs finished and were held" 'held work_r1 "$r1" && held work_r2 "$r2"'
check "each worker got its own secret" '[ "$(cat "$r1/secret")" = "$S1" ] && [ "$(cat "$r2/secret")" = "$S2" ]'
dump "$f"

fi
if want 10; then
echo "== 10. two pollers: the next message arrives just as the last run ends, and a pause takes 4 s"
# The interleaving this scenario reproduces: work_r2 is handed out when work_r1's worker
# writes its exit code. A poller that checked for other workers and then paused would have its slow
# pause land on work_r2's running worker.
export STUB_RUN_SECONDS=3 STUB_TAIL_SECONDS=0 FAKE_PAUSE_DELAY=4
D=$T/ten; f=$FAILS
run_poller "$D" "$(queue "$(item work_q1 sesn_Q "$S1"),$(item work_q2 sesn_Q "$S2" "prized/sbx_*/home/.anthropic/work_q1/done")")" 2
wait_for 90 'stopped "$D" work_q1 && stopped "$D" work_q2' || bad "both work items were not stopped within 90 s"
finish
unset FAKE_PAUSE_DELAY
check "no worker was frozen or stopped by the other poller, and both runs were held" \
  'never_paused "$D" && held work_q1 "$(seen "$D" work_q1)" && held work_q2 "$(seen "$D" work_q2)"'
dump "$f"
fi
if want 5; then
echo "== 5. the ant download fails once, then serves another binary once"
export STUB_RUN_SECONDS=2 STUB_TAIL_SECONDS=0
D=$T/five; f=$FAILS
mkdir -p "$D"; touch "$D/fail-once" "$D/evil-once"
export STUB_CURL_FAIL_ONCE=$D/fail-once STUB_EVIL_ONCE=$D/evil-once
run_poller "$D" "$(queue "$(item work_i1 sesn_I "$S1"),$(item work_i2 sesn_I "$S2"),$(item work_i3 sesn_I "$S1")")"
wait_for 90 stopped "$D" work_i3 || bad "work_i3 was not stopped within 90 s"
finish
unset STUB_CURL_FAIL_ONCE STUB_EVIL_ONCE
I=$(sbx_of "$D" sesn_I)
check "the failed download stopped work_i1 before any worker" \
  '[ ! -e "$D/fail-once" ] && logs "$D" | grep -q "curl: (22)" && [ ! -e "$(seen "$D" work_i1)/start" ]'
check "the binary with the wrong sha256 was refused, never run, and work_i2 never started" \
  '[ ! -e "$D/evil-once" ] && logs "$D" | grep -q "FAILED" && [ ! -e "$D/prized/$I/home/EVIL-ANT-RAN" ] && [ ! -e "$(seen "$D" work_i2)/start" ]'
check "the next message resumed the same sandbox, installed the right ant, and ran" \
  '[ "$(sbx_count "$D" sesn_I)" = 1 ] && held work_i3 "$(seen "$D" work_i3)"'
check "no download was left behind" '! ls -d "$D/prized/$I"/home/.local/bin/.ant.* >/dev/null 2>&1'
check "the curl stub only ever saw the pinned URL" '! logs "$D" | grep -q "unexpected request"'
dump "$f"

fi
if want 6; then
echo "== 6. the session's sandbox won't resume: a quota refusal, no host capacity, then max_runtime"
D=$T/six; f=$FAILS
run_poller "$D" "$(queue "$(item work_x1 sesn_X "$S1"),$(item work_x2 sesn_X "$S2" go-x2),$(item work_x3 sesn_X "$S1" go-x3),$(item work_x4 sesn_X "$S2" go-x4)")"
wait_for 60 stopped "$D" work_x1 || bad "work_x1 was not stopped within 60 s"
X1=$(sbx_of "$D" sesn_X)
set_meta "$D" "$X1" '.resume_fails = true | .resume_reason = "running_vcpu"'; touch "$D/go-x2"
wait_for 60 stopped "$D" work_x2 || bad "work_x2 was not stopped within 60 s"
c2=$(meta "$D" "$X1" .resume_calls)
set_meta "$D" "$X1" '.resume_reason = "invalid_resources"'; touch "$D/go-x3"
wait_for 60 stopped "$D" work_x3 || bad "work_x3 was not stopped within 60 s"
c3=$(meta "$D" "$X1" .resume_calls) n3=$(sbx_count "$D" sesn_X)
set_meta "$D" "$X1" '.resume_reason = "max_runtime"'; touch "$D/go-x4"
wait_for 60 stopped "$D" work_x4 || bad "work_x4 was not stopped within 60 s"
finish
check "a quota refusal was retried 3 times, then the work item failed, with no new sandbox" \
  '[ "$c2" = 3 ] && [ ! -e "$(seen "$D" work_x2)/start" ] && logs "$D" | grep -q "failing this work item"'
check "so was a refusal for host capacity (invalid_resources): the sandbox and its files were kept" \
  '[ "$c3" = 6 ] && [ "$n3" = 1 ] && [ ! -e "$(seen "$D" work_x3)/start" ] && [ -e "$D/prized/$X1/home/work/made-by-work_x1" ]'
check "max_runtime: work_x4 ran in a new sandbox" \
  '[ "$(sbx_count "$D" sesn_X)" = 2 ] && held work_x4 "$(seen "$D" work_x4)" && [ "$(dirname "$(dirname "$(dirname "$(dirname "$(seen "$D" work_x4)")")")")" != "$D/prized/$X1" ]'
check "the old sandbox was kept, not deleted" '[ "$(meta "$D" "$X1" .state)" != deleted ]'
dump "$f"

fi
if want 7; then
echo "== 7. the worker dies without writing an exit code"
export STUB_DIE=1 STUB_RUN_SECONDS=120
D=$T/seven; f=$FAILS
run_poller "$D" "$(queue "$(item work_d1 sesn_D "$S1")")"
wait_for 60 "[ -e \"\$(seen $D work_d1)/died\" ]" || bad "the stub never died"
wait_for 30 stopped "$D" work_d1 || bad "the work item was not stopped within 30 s of the worker dying"
finish
unset STUB_DIE
check "the script stopped waiting and said why" 'stopped "$D" work_d1 && logs "$D" | grep -q "stopped without finishing"'
dump "$f"

fi
if want 8; then
echo "== 8. a run that never ends (say, waiting on a tool approval) hits RUN_TIMEOUT"
export STUB_RUN_SECONDS=600 RUN_TIMEOUT=5s
D=$T/eight; f=$FAILS
run_poller "$D" "$(queue "$(item work_t1 sesn_T "$S1")")"
wait_for 60 "[ -e \"\$(seen $D work_t1)/start\" ]" || bad "the worker never started"
wait_for 40 stopped "$D" work_t1 || bad "the work item was not stopped within 40 s"
finish
unset RUN_TIMEOUT
t1=$(seen "$D" work_t1)
check "the worker was stopped about RUN_TIMEOUT after it started" \
  '[ -e "$t1/term" ] && awk -v b="$(cat "$t1/start")" -v e="$(cat "$t1/term")" "BEGIN { exit !(e - b >= 3 && e - b < 25) }"'
check "and the log says why" 'logs "$D" | grep -q "passed RUN_TIMEOUT"'
dump "$f"

fi
if want 9; then
echo "== 9. Ctrl-C on the README's several-pollers line"
export STUB_RUN_SECONDS=120
D=$T/nine; f=$FAILS
README_WRAPPER=1 run_poller "$D" "$(queue "$(item work_c1 sesn_F "$S1")")" 2
wait_for 60 "[ -e \"\$(seen $D work_c1)/start\" ]" || bad "the worker never started"
kill -INT -- "-$POLLER"  # what Ctrl-C does: SIGINT to the whole foreground process group
wait_for 45 "! pgrep -g $POLLER >/dev/null" || { bad "something in the group was still running 45 s after Ctrl-C"; ps -o pid,stat,etimes,args -g "$POLLER"; }
check "every poller and script exited" '! pgrep -g "$POLLER" >/dev/null'
finish
check "the worker was stopped" '[ -e "$(seen "$D" work_c1)/term" ]'
dump "$f"

fi
echo "== $FAILS failed"
[ "$FAILS" = 0 ]
