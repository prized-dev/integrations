#!/usr/bin/env bash
# check() evals its single-quoted condition later, so variables set here are used there.
# shellcheck disable=SC2016,SC2034
# Offline tests for move.sh. `prized` is a stand-in whose box is a folder (fake-prized), tmux on the
# box is a stand-in whose sessions are files (box-bin/tmux), and origin is a local bare repo. No
# network, no keys.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MOVE=$HERE/../move.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/box"
ln -s "$HERE/fake-prized" "$T/bin/prized"
export PATH="$T/bin:$PATH" FAKE_BOX=$T/box
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
B=$T/box
FAILS=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; FAILS=$((FAILS + 1)); fi; }
# run DIR ARGS...: move.sh as `curl | bash` runs it (the script on stdin), from DIR.
run() {
  local dir=$1
  shift
  CODE=0
  (cd "$dir" && bash -s -- "$@" <"$MOVE") >"$T/out" 2>&1 || CODE=$?
}
session() { cat "$B/.faketmux/$1" 2>/dev/null; }

# origin: a bare repo with conductor.json and a pushed branch `existing`; the Mac checkout of it has
# one gitignored .env file, one .env file git doesn't ignore, and one ignored file that isn't .env*.
mkrepo() { # mkrepo NAME: $T/src/NAME pushed to $T/origin/NAME.git, cloned to $T/mac/NAME
  local n=$1
  git init -q -b main "$T/src/$n"
  printf '.env.local\nconfig/local.json\n' >"$T/src/$n/.gitignore"
  cat >"$T/src/$n/conductor.json" <<'JSON'
{
  "scripts": {
    "setup": "echo \"$CONDUCTOR_WORKSPACE_NAME $CONDUCTOR_PORT $CONDUCTOR_ROOT_PATH $CONDUCTOR_IS_LOCAL $CONDUCTOR_DEFAULT_BRANCH $CONDUCTOR_WORKSPACE_PATH\" > setup-ran; echo x >> \"$HOME/setup-count-$CONDUCTOR_WORKSPACE_NAME\"; [ ! -f \"$HOME/fail-setup\" ] || exit 3",
    "run": "echo serving on $CONDUCTOR_PORT",
    "archive": "touch \"$CONDUCTOR_ROOT_PATH/archived-$CONDUCTOR_WORKSPACE_NAME\""
  }
}
JSON
  git -C "$T/src/$n" add -A
  git -C "$T/src/$n" commit -q -m init
  git -C "$T/src/$n" checkout -q -b existing
  echo on-existing >"$T/src/$n/existing.txt"
  git -C "$T/src/$n" add -A
  git -C "$T/src/$n" commit -q -m existing
  git -C "$T/src/$n" checkout -q main
  git clone -q --bare "$T/src/$n" "$T/origin/$n.git"
  git clone -q "$T/origin/$n.git" "$T/mac/$n"
  echo 'SECRET=fake' >"$T/mac/$n/.env.local"
  echo 'not ignored' >"$T/mac/$n/.envrc"
  mkdir -p "$T/mac/$n/config"
  echo '{}' >"$T/mac/$n/config/local.json"
}
mkrepo app
A=$B/code/app

echo "# a move from the checkout, under curl | bash: worktrees, files, setup, agents"
run "$T/mac/app" -w feature/login -w existing --files
check "exits 0" '[ "$CODE" = 0 ] || { cat "$T/out"; false; }'
check "prized setup clones the checkout and signs claude in" 'grep -qx "prized setup $T/mac/app --clone --no-start --no-attach --agent claude" "$B/../calls.log"'
check "a worktree per branch beside the clone" '[ "$(git -C "$A" worktree list | wc -l)" = 3 ] && [ -d "$B/code/app-feature-login" ] && [ -d "$B/code/app-existing" ]'
check "a new branch starts from origin's default branch, with no upstream yet" '[ "$(git -C "$B/code/app-feature-login" rev-parse HEAD)" = "$(git -C "$A" rev-parse origin/main)" ] && ! git -C "$B/code/app-feature-login" rev-parse --abbrev-ref "@{u}" >/dev/null 2>&1'
check "a branch origin has is checked out, tracking it" '[ -f "$B/code/app-existing/existing.txt" ] && [ "$(git -C "$B/code/app-existing" rev-parse --abbrev-ref "@{u}")" = origin/existing ]'
check "setup ran in each with Conductor's variables" 'grep -qx "feature-login 4100 $A 1 main $B/code/app-feature-login" "$B/code/app-feature-login/setup-ran" && grep -qx "existing 4110 $A 1 main $B/code/app-existing" "$B/code/app-existing/setup-ran"'
check "the gitignored .env file is copied to the clone and each worktree" '[ "$(cat "$A/.env.local")" = SECRET=fake ] && [ -f "$B/code/app-feature-login/.env.local" ] && [ -f "$B/code/app-existing/.env.local" ]'
check "a .env file git doesn't ignore, and an ignored non-.env file, are not" '[ ! -e "$A/.envrc" ] && [ ! -e "$A/config/local.json" ]'
check "a tmux session per workspace, in its worktree, with its variables" 'session claude-feature-login | grep -qx "dir=$B/code/app-feature-login" && session claude-feature-login | grep -qx "env CONDUCTOR_PORT=4100" && session claude-existing | grep -qx "env CONDUCTOR_PORT=4110"'
check "claude started in each, in auto mode" 'session claude-feature-login | grep -qx "agent=claude dir=code/app-feature-login args=--permission-mode auto" && session claude-existing | grep -q "^agent=claude dir=code/app-existing"'
check "says how to open one" 'grep -q "prized ssh -s claude-feature-login" "$T/out"'
check "the box helper ran from its copy on the box" '[ -f "$B/.local/share/prized-conductor/move.py" ]'

echo "# re-running changes nothing"
run "$T/mac/app" -w feature/login -w existing --files
check "exits 0" '[ "$CODE" = 0 ]'
check "setup didn't run again" '[ "$(wc -l <"$B/setup-count-feature-login")" = 1 ] && grep -q "setup ran here before" "$T/out"'
check "same ports" 'session claude-existing | grep -qx "env CONDUCTOR_PORT=4110"'

echo "# a failed setup is reported, and retried on the next run"
touch "$B/fail-setup"
run "$T/mac/app" -w broken --no-files
check "exits 1, says setup failed" '[ "$CODE" = 1 ] && grep -q "broken: setup failed (exit 3)" "$T/out" && grep -q "setup failed(exit 3)" "$T/out" && grep -q "1 workspace(s) need you" "$T/out"'
rm "$B/fail-setup"
run "$T/mac/app" -w broken --no-files
check "the next run runs it again" '[ "$(wc -l <"$B/setup-count-broken")" = 2 ] && grep -q "setup ok" "$T/out"'

echo "# a worktree whose folder was deleted is made again, and its setup runs again"
rm -rf "$B/code/app-feature-login"
run "$T/mac/app" -w feature/login --no-files
check "exits 0" '[ "$CODE" = 0 ] || { cat "$T/out"; false; }'
check "the worktree is back and setup ran in it" '[ -f "$B/code/app-feature-login/setup-ran" ] && [ "$(wc -l <"$B/setup-count-feature-login")" = 2 ]'

echo "# a branch the box can't get from origin stops the move before it starts"
: >"$B/../calls.log"
git -C "$T/mac/app" branch only-here
run "$T/mac/app" -w only-here
check "a branch only on this machine" '[ "$CODE" = 1 ] && grep -q "git push -u origin only-here" "$T/out"'
git -C "$T/mac/app" checkout -q existing
echo more >"$T/mac/app/existing.txt"
git -C "$T/mac/app" commit -q -am "not pushed"
git -C "$T/mac/app" checkout -q main
run "$T/mac/app" -w existing
check "a branch with commits origin doesn't have" '[ "$CODE" = 1 ] && grep -q "1 commit(s) origin doesn" "$T/out"'
check "neither called prized" '[ ! -s "$B/../calls.log" ]'
git -C "$T/mac/app" branch -q -f existing origin/existing

echo "# run from a Conductor workspace (a linked worktree), it moves the main checkout"
git -C "$T/mac/app" worktree add -q "$T/mac/ws-tokyo" -b tokyo
for i in $(seq 1 80); do git -C "$T/mac/app" worktree add -q "$T/mac/wt/ws-$i" -b "city-$i"; done
run "$T/mac/ws-tokyo" -w feature/login -w nofiles --no-files
check "exits 0 among 80 more worktrees, the same repo" '[ "$CODE" = 0 ] && grep -q "is a worktree of $T/mac/app" "$T/out" && grep -q "prized setup $T/mac/app --clone" "$B/../calls.log" && [ ! -e "$B/code/ws-tokyo" ] || { tail -3 "$T/out"; false; }'
check "--no-files copies nothing into a new worktree, though the clone has the file" '[ -f "$A/.env.local" ] && [ -d "$B/code/app-nofiles" ] && [ ! -e "$B/code/app-nofiles/.env.local" ]'

echo "# many matching files, node_modules and a virtualenv named .env"
mkdir -p "$T/mac/app/.envs" "$T/mac/app/.env/lib" "$T/mac/app/web/node_modules/pkg"
for i in $(seq 1 3000); do echo "x" >"$T/mac/app/.envs/f$i"; done
echo 'home = /usr' >"$T/mac/app/.env/pyvenv.cfg"
echo x >"$T/mac/app/.env/lib/site.py"
echo x >"$T/mac/app/web/node_modules/pkg/.env.example"
printf '.envs/\n.env/\nweb/node_modules/\n' >>"$T/mac/app/.git/info/exclude"
run "$T/mac/app" -w many --files --agent none
check "exits 0 with a list longer than a pipe holds" '[ "$CODE" = 0 ] || { tail -5 "$T/out"; false; }'
check "says how many and shows 15" 'grep -q "copies 3001 gitignored file" "$T/out" && grep -q "and 2986 more" "$T/out"'
check "the virtualenv and node_modules stay out" '[ -f "$B/code/app-many/.envs/f3000" ] && [ ! -e "$A/.env/pyvenv.cfg" ] && [ ! -e "$A/web/node_modules/pkg/.env.example" ]'

echo "# .conductor/settings.toml wins over conductor.json; settings.local.toml and .worktreeinclude count"
mkrepo app2
mkdir -p "$T/src/app2/.conductor"
cat >"$T/src/app2/.conductor/settings.toml" <<'TOML'
"$schema" = "https://conductor.build/schemas/settings.repo.schema.json"
[scripts]
setup = """
echo "toml $FOO $ONLY_LOCAL $CONDUCTOR_PORT" > toml-ran
"""
run_mode = "nonconcurrent"

[scripts.run.cloudonly]
command = "echo cloud only"
default = true
available_in = "cloud"

[scripts.run.web]
command = "echo web"

[scripts.run.api]
command = "echo api on $((CONDUCTOR_PORT + 1))"
default = true

[environment_variables]
FOO = "shared"
PATH = "/nope"
MULTI = """line one
line two"""

[environment_variables.local]
ONLY_LOCAL = "yes"
TOML
printf 'config/local.json\n' >"$T/src/app2/.worktreeinclude"
git -C "$T/src/app2" add -A
git -C "$T/src/app2" commit -q -m settings
git -C "$T/src/app2" push -q "$T/origin/app2.git" main
git -C "$T/mac/app2" pull -q
printf '[environment_variables]\nFOO = "mine"\n' >"$T/mac/app2/.conductor/settings.local.toml"
run "$T/mac/app2" -w a -w b --run --files --agent codex
check "exits 0" '[ "$CODE" = 0 ] || { cat "$T/out"; false; }'
check "the settings.toml setup ran, not conductor.json's, with settings.local.toml on top" '[ "$(cat "$B/code/app2-a/toml-ran")" = "toml mine yes 4150" ] && [ ! -e "$B/code/app2-a/setup-ran" ]'
check "a reserved name stays out" '! session codex-a | grep -q "^env PATH=" && grep -q "not set (reserved" "$T/out"'
check "ports don't overlap another repo's" '[ "$(cat "$B/code/app2-b/toml-ran")" = "toml mine yes 4160" ]'
check ".worktreeinclude replaces the .env* default" '[ -f "$B/code/app2-a/config/local.json" ] && [ ! -e "$B/code/app2-a/.env.local" ]'
check "settings.local.toml came from the checkout" '[ -f "$B/code/app2/.conductor/settings.local.toml" ]'
check "a multi-line variable reaches the session" 'session codex-a | grep -qx "env MULTI=line one"'
check "nonconcurrent: one run script, the default action" '[ -f "$B/.faketmux/run-a" ] && [ ! -e "$B/.faketmux/run-b" ] && grep -qx "echo api on \$((CONDUCTOR_PORT + 1))" "$B/.local/share/prized-conductor/app2/a-run.sh"'
check "codex starts in its sandbox, never asking" 'session codex-a | grep -qx "agent=codex dir=code/app2-a args=--sandbox workspace-write --ask-for-approval never"'

echo "# the same branch in two repos gets two sessions"
run "$T/mac/app2" -w feature/login --no-files
check "exits 0" '[ "$CODE" = 0 ]'
check "the second is named for its repo" 'session claude-app2-feature-login | grep -qx "dir=$B/code/app2-feature-login" && session claude-feature-login | grep -qx "dir=$B/code/app-feature-login" && grep -q "prized ssh -s claude-app2-feature-login" "$T/out"'
run "$T/mac/app2" -w feature/login --no-files
check "and keeps that name on a re-run" '[ "$CODE" = 0 ] && [ "$(ls "$B/.faketmux" | grep -c feature-login)" = 2 ]'

echo "# --setup: the Conductor Cloud setup script, as a cloud workspace runs it"
printf 'echo "cloud $CONDUCTOR_IS_LOCAL $CONDUCTOR_ROOT_PATH" > cloud-ran\n' >"$T/cloud-setup.sh"
run "$T/mac/app" -w c --setup "$T/cloud-setup.sh" --agent none --no-files
check "exits 0" '[ "$CODE" = 0 ]'
check "it runs instead of the repo's, with CONDUCTOR_IS_LOCAL=0 and the workspace as root" '[ "$(cat "$B/code/app-c/cloud-ran")" = "cloud 0 $B/code/app-c" ] && [ ! -e "$B/code/app-c/setup-ran" ]'
check "--agent none starts no session" '[ ! -e "$B/.faketmux/none-c" ] && [ ! -e "$B/.faketmux/claude-c" ]'

echo "# --archive: the archive script, the sessions stopped, the worktree gone, the branch kept"
run "$T/mac/app" --archive existing
check "uncommitted changes stop it before anything runs" '[ "$CODE" = 1 ] && grep -q "uncommitted change" "$T/out" && [ -f "$B/.faketmux/claude-existing" ] && [ ! -e "$A/archived-existing" ]'
run "$T/mac/app" --archive existing --force
check "--force goes ahead" '[ "$CODE" = 0 ] || { cat "$T/out"; false; }'
check "archive script ran with the workspace's variables" '[ -f "$A/archived-existing" ]'
check "its session stopped, its worktree removed, its branch kept" '[ ! -e "$B/.faketmux/claude-existing" ] && [ ! -d "$B/code/app-existing" ] && git -C "$A" show-ref --verify --quiet refs/heads/existing'
check "other workspaces' sessions keep running" '[ -f "$B/.faketmux/claude-feature-login" ] && [ -f "$B/.faketmux/claude-app2-feature-login" ]'

run "$T/mac/app2" --archive b --force
check "archiving a workspace with a multi-line variable works" '[ "$CODE" = 0 ] && [ ! -d "$B/code/app2-b" ]'

echo "# bad input stops before anything runs"
: >"$B/../calls.log"
for bad in -x a..b "a b" "a;b" "/x"; do
  run "$T/mac/app" -w "$bad"
  check "refuses branch \"$bad\"" '[ "$CODE" = 1 ] && grep -q "is not a branch name" "$T/out"'
done
run "$T/mac/app" --agent cursor
check "refuses an agent it can't start" '[ "$CODE" = 1 ]'
mkdir -p "$T/notgit"
run "$T/notgit" -w x
check "a folder that isn't a checkout" '[ "$CODE" = 1 ] && grep -q "is not a git checkout" "$T/out"'
check "none of them called prized" '[ ! -s "$B/../calls.log" ]'

echo "# --print-helper prints the box helper, which compiles"
bash "$MOVE" --print-helper >"$T/helper.py"
check "compiles" 'python3 -I -m py_compile "$T/helper.py"'

if [ "$FAILS" != 0 ]; then echo "$FAILS failed"; exit 1; fi
echo "all passed"
