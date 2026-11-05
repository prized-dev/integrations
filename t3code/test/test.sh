#!/usr/bin/env bash
# check() evals its single-quoted condition later, so variables set here are used there.
# shellcheck disable=SC2016,SC2034
# Offline tests for prized-t3. `prized` and `ssh` are stand-ins (fakes/): a box is a directory, and
# the script's box-side parts run with the real sh, tar and sha256sum in the box's home. T3 Code's
# releases are fake archives built here. No network, no keys.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPT=$HERE/../prized-t3
T=$(mktemp -d)
trap 'pkill -f "$T/" 2>/dev/null || true; rm -rf "$T"' EXIT
FAILS=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; FAILS=$((FAILS + 1)); fi; }

case "$(uname -m)" in x86_64 | amd64) ARCH=x64 ;; *) ARCH=arm64 ;; esac
case "$(uname -s)" in Darwin) PLAT=darwin ;; *) PLAT=linux ;; esac

# release VERSION: a fake T3 Code release archive and its SHA256SUMS under $T/releases.
release() {
  local v=$1 d=$T/build/t3-$1-$PLAT-$ARCH
  mkdir -p "$d" "$T/releases/v$v"
  cat > "$d/t3" <<EOF
#!/bin/sh
case "\$1" in
  --version) echo "$v" ;;
  browser)
    if [ -n "\${SUDO_USER:-}" ]; then echo "browser setup by \$SUDO_USER" >> "\$FAKE_ROOT/browser.log"; echo "Allowed Chrome's sandbox for T3's browser."; echo
    else echo "This needs root. Run:"; echo; echo "  sudo t3 browser setup"; fi ;;
  serve) while :; do sleep 1; done ;;
esac
EOF
  chmod +x "$d/t3"
  tar -czf "$T/releases/v$v/t3-$v-$PLAT-$ARCH.tar.gz" -C "$T/build" "t3-$v-$PLAT-$ARCH"
  (cd "$T/releases/v$v" && sha256sum "t3-$v-$PLAT-$ARCH.tar.gz" > SHA256SUMS)
}
release 0.1.7
release 0.1.6
release 0.1.5
# A checksum file whose line doesn't match the archive at all.
printf '%064d  t3-0.1.5-%s-%s.tar.gz\n' 0 "$PLAT" "$ARCH" > "$T/releases/v0.1.5/SHA256SUMS"

mkdir -p "$T/bin"
ln -s "$HERE/fakes/prized" "$T/bin/prized"
ln -s "$HERE/fakes/ssh" "$T/bin/ssh"
ln -s "$HERE/box-bin/curl" "$T/bin/curl"
export FAKE_RELEASES=$T/releases FAKE_BOX_BIN=$HERE/box-bin PRIZED_T3_NO_PROMPT=1
export PATH="$T/bin:$PATH"
KEY=dcp_000never-anywhere-0000

# world NAME: a fresh fake Prized with one box, mango (running, in the SSH config), the default.
world() {
  W=$T/$1; export FAKE_ROOT=$W/root
  mkdir -p "$FAKE_ROOT/boxes/mango/home/.local/bin" "$FAKE_ROOT/template-home/.local/bin"
  for h in "$FAKE_ROOT/boxes/mango/home" "$FAKE_ROOT/template-home"; do
    printf 'PATH="$HOME/.local/bin:$PATH"\n' > "$h/.profile"
    printf '#!/bin/sh\n' > "$h/.local/bin/claude"; printf '#!/bin/sh\n' > "$h/.local/bin/codex"
    chmod +x "$h/.local/bin/claude" "$h/.local/bin/codex"
  done
  echo running > "$FAKE_ROOT/boxes/mango/state"; touch "$FAKE_ROOT/boxes/mango/alias"
  echo mango > "$FAKE_ROOT/default"; : > "$FAKE_ROOT/calls.log"
  H=$FAKE_ROOT/boxes/mango/home
}
# run ARGS...: prized-t3 with an API key in its environment (it must never pass it on), from $W.
# TEST_SH=bash (or dash, busybox sh...) runs the script under that shell instead of its #!/bin/sh.
# shellcheck disable=SC2086 # TEST_SH may be two words, such as busybox sh
run() { CODE=0; (cd "$W" && env PRIZED_API_KEY=$KEY ${TEST_SH:-} "$SCRIPT" "$@") > "$W/out" 2>&1 || CODE=$?; }
has() { grep -qF -- "$1" "$W/out"; }
called() { grep -qF -- "$1" "$FAKE_ROOT/calls.log"; }
downloads() { grep -c "$1" "$FAKE_ROOT/downloads.log" 2>/dev/null || true; }

echo "# up on the default box: the server for the requested version, browser setup, the agents"
world up
run up --t3-version 0.1.7 --no-open
check "exits 0" '[ "$CODE" = 0 ] || { cat "$W/out"; false; }'
check "uses the default box" 'called "info --json" && has "Getting mango ready"'
check "server in T3 Code's layout, marked complete" '[ -x "$H/.t3/runtime/versions/0.1.7/t3" ] && [ "$(cat "$H/.t3/runtime/versions/0.1.7/.install-complete")" = 0.1.7 ]'
check "archive and checksums removed, no staging or lock left" '[ ! -e "$H/.t3/runtime/versions/0.1.7/SHA256SUMS" ] && [ -z "$(ls -A "$H/.t3/runtime/versions" | grep -v "^0.1.7$")" ]'
check "browser setup ran with sudo" 'grep -q "browser setup by fake" "$FAKE_ROOT/browser.log" && has "browser:      Allowed Chrome'\''s sandbox"'
check "agents found on the login PATH, sign-in missing" 'has "claude:       $H/.local/bin/claude, no sign-in found" && has "prized --box mango agents handoff claude codex --gh"'
check "tells you what to click" 'has "Add environment > SSH, and pick"'
check "the API key is on no command line and nowhere on the box" '! grep -rqa "$KEY" "$FAKE_ROOT"'

echo "# up again: nothing downloaded twice"
run up mango --t3-version 0.1.7 --no-open
check "already installed, one download in all" '[ "$CODE" = 0 ] && has "0.1.7 already installed" && [ "$(downloads t3-0.1.7)" = 1 ]'

echo "# the desktop app's version, from the t3 command, else the latest release"
mkdir -p "$W/t3bin"; printf '#!/bin/sh\necho "t3 0.1.6"\n' > "$W/t3bin/t3"; chmod +x "$W/t3bin/t3"
PATH="$W/t3bin:$PATH" run up mango --no-open
check "installs the version t3 --version names" '[ "$CODE" = 0 ] && [ -x "$H/.t3/runtime/versions/0.1.6/t3" ] && has "T3 Code here: 0.1.6"'
world latest
run up --no-open
check "no app or t3 here: the latest release" '[ "$CODE" = 0 ] && [ -x "$H/.t3/runtime/versions/0.1.7/t3" ] && has "installing the latest release, 0.1.7"'

echo "# a paused box is resumed, a box missing from the SSH config is added"
world wake
echo suspended > "$FAKE_ROOT/boxes/mango/state"; rm "$FAKE_ROOT/boxes/mango/alias"
run up mango --t3-version 0.1.7 --no-open --auto-pause 2h
check "resumed, then prized init --box" '[ "$CODE" = 0 ] && called "box resume mango --wait" && called "init --box mango --non-interactive --no-daemon"'
check "auto-pause set" '[ "$(cat "$FAKE_ROOT/boxes/mango/auto-pause")" = 2h ]'

echo "# a bad checksum installs nothing"
world checksum
run up --t3-version 0.1.5 --no-open
check "fails, says why" '[ "$CODE" = 1 ] && has "checksum mismatch"'
check "no runtime, staging directory or lock left" '[ -z "$(ls -A "$H/.t3/runtime/versions")" ]'
check "the box is reported as it was: no browser step" '[ ! -e "$FAKE_ROOT/browser.log" ]'

echo "# T3 Code's install lock: a dead owner is taken over, a live one waited for"
world lock
mkdir -p "$H/.t3/runtime/versions"
echo 999999 > "$H/.t3/runtime/versions/.0.1.7.install.lock"
run up --t3-version 0.1.7 --no-open --no-browser-setup
check "stale lock from a dead pid: installed" '[ "$CODE" = 0 ] && [ -x "$H/.t3/runtime/versions/0.1.7/t3" ]'
world lock-live
mkdir -p "$H/.t3/runtime/versions"
sleep 3 & HOLDER=$!
echo "$HOLDER" > "$H/.t3/runtime/versions/.0.1.7.install.lock"
start=$SECONDS
run up --t3-version 0.1.7 --no-open --no-browser-setup
check "lock held by a live pid: waits for it, then installs" '[ "$CODE" = 0 ] && [ $((SECONDS - start)) -ge 2 ] && [ -x "$H/.t3/runtime/versions/0.1.7/t3" ]'
wait "$HOLDER" 2>/dev/null || true

echo "# input that could reach a remote shell is refused"
world input
run up --t3-version '0.1.7;touch /tmp/prized-t3-pwned' --no-open
check "a version with shell syntax" '[ "$CODE" = 1 ] && has "one exact version" && ! grep -q "^ssh" "$FAKE_ROOT/calls.log"'
run up 'mango;id' --no-open
check "a box name with shell syntax" '[ "$CODE" = 1 ] && has "not a box name"'
run up mango --tier lite --no-open
check "--tier without --new" '[ "$CODE" = 1 ] && has "--tier goes with --new"'

echo "# up --new: a box made and waited for"
world new
run up --new kiwi --tier lite --t3-version 0.1.7 --no-open --no-browser-setup
check "prized box new --wait --json, then init" '[ "$CODE" = 0 ] && called "box new kiwi --tier lite --wait --json" && called "init --box kiwi" && [ -x "$FAKE_ROOT/boxes/kiwi/home/.t3/runtime/versions/0.1.7/t3" ]'

echo "# check changes nothing"
world check
mkdir -p "$H/.t3/userdata"; echo old-id > "$H/.t3/userdata/environment-id"
before=$(cd "$H" && find . -printf '%p %s %T@\n' | sort)
run check mango
check "reports, exits 0" '[ "$CODE" = 0 ] && has "T3 identity:  old-id" && has "T3 server:    not installed yet"'
check "the box is untouched" '[ "$(cd "$H" && find . -printf "%p %s %T@\n" | sort)" = "$before" ]'

echo "# fork: a copy that T3 Code sees as a machine of its own"
world fork
U=$H/.t3/userdata
mkdir -p "$U/secrets" "$H/.t3/ssh-launch/900c6b2493d5458d" "$H/.t3/runtime/versions/0.1.7"
echo source-id > "$U/environment-id"; echo '{"version":1,"pid":4242,"port":3773}' > "$U/server-runtime.json"
echo anon > "$U/anonymous-id"; echo mango > "$H/.t3/usage-host-id"; echo '{"theme":"dark"}' > "$U/settings.json"; echo db > "$U/statev2.sqlite"
for s in server-signing-key guest-share-signing-key mcp-oauth-signing-key cloud-link-ed25519-key-pair cloud-cli-desired-link bitbucket-access-token secret-request-salt; do echo x > "$U/secrets/$s.bin"; done
echo 4242 > "$H/.t3/ssh-launch/900c6b2493d5458d/pid"
run fork mango --name mango-2 --t3-version 0.1.7 --no-open --no-browser-setup
F=$FAKE_ROOT/boxes/mango-2/home
FU=$F/.t3/userdata
check "exits 0, prized fork --name --wait --json, then init" '[ "$CODE" = 0 ] && called "fork mango --name mango-2 --wait --json" && called "init --box mango-2" || { cat "$W/out"; false; }'
check "the copy's T3 Code identity is gone" '[ ! -e "$FU/environment-id" ] && [ ! -e "$FU/server-runtime.json" ] && [ ! -e "$FU/anonymous-id" ] && [ ! -e "$F/.t3/usage-host-id" ] && [ ! -e "$F/.t3/ssh-launch" ]'
check "its signing keys and T3 Connect link are gone" '[ "$(ls "$FU/secrets" | sort | tr "\n" " ")" = "bitbucket-access-token.bin secret-request-salt.bin " ]'
check "projects, threads, settings and saved secrets stay" '[ -s "$FU/statev2.sqlite" ] && [ -s "$FU/settings.json" ] && [ -s "$FU/secrets/bitbucket-access-token.bin" ]'
check "the source is untouched" '[ "$(cat "$U/environment-id")" = source-id ] && [ -e "$U/secrets/server-signing-key.bin" ] && [ -e "$H/.t3/usage-host-id" ]'
check "says the old id is removed, then readies the copy" 'has "source-id removed" && has "mango-2 is ready"'

echo "# reset-identity stops T3 Code's server, and nothing else"
world reset
U=$H/.t3/userdata; mkdir -p "$U" "$H/.t3/ssh-launch/k1" "$H/.t3/ssh-launch/k2" "$H/.t3/runtime/versions/0.1.7"
cp "$T/build/t3-0.1.7-$PLAT-$ARCH/t3" "$H/.t3/runtime/versions/0.1.7/t3"
(cd "$H" && exec sh "$H/.t3/runtime/versions/0.1.7/t3" serve --host 127.0.0.1 --port 3773) & SERVER=$!
sleep 300 & OTHER=$!   # an unrelated process whose pid a stale pid file happens to hold
echo "$SERVER" > "$H/.t3/ssh-launch/k1/pid"; echo "$OTHER" > "$H/.t3/ssh-launch/k2/pid"
echo id-1 > "$U/environment-id"
sleep 0.5
run reset-identity mango
check "exits 0, says what it stopped" '[ "$CODE" = 0 ] && has "stopped T3 Code server $SERVER" || { cat "$W/out"; false; }'
check "the T3 Code server is stopped" '! kill -0 "$SERVER" 2>/dev/null'
check "an unrelated process with a recorded pid keeps running" 'kill -0 "$OTHER" 2>/dev/null'
kill "$OTHER" 2>/dev/null || true
check "identity removed" '[ ! -e "$U/environment-id" ] && has "id-1 removed"'
world service
mkdir -p "$H/.t3/userdata"; echo id-2 > "$H/.t3/userdata/environment-id"
FAKE_T3_SERVICE=1 run reset-identity mango
check "a running t3code.service is stopped, then started again" '[ "$CODE" = 0 ] && grep -q "systemctl --user stop t3code.service" "$FAKE_ROOT/calls.log" && grep -q "systemctl --user start t3code.service" "$FAKE_ROOT/calls.log"'

echo "# without sudo, browser setup only says what it would do"
world nosudo
FAKE_SUDO=0 run up --t3-version 0.1.7 --no-open
check "still exits 0, shows the command" '[ "$CODE" = 0 ] && has "sudo t3 browser setup" && [ ! -e "$FAKE_ROOT/browser.log" ]'

echo
if [ "$FAILS" = 0 ]; then echo "all passed"; else echo "$FAILS failed"; exit 1; fi
