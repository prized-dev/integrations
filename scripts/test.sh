#!/usr/bin/env bash
# Lint and test one integration offline, the way CI does: a fresh virtualenv (or a temp copy for
# Node), no API key, and, where the machine allows it, no network while the tests run.
#
#   scripts/test.sh <folder>            # latest dependencies
#   FLOOR=1 scripts/test.sh <folder>    # the oldest framework versions the package supports
#   PYTHON=python3.10 scripts/test.sh inspect
#
# Live tests are never run here: they need PRIZED_API_KEY plus a flag, and this script unsets both.
#
# SDK-PRERELEASE: the Prized SDKs (`prized` on PyPI, `@prized/sdk` on npm) are published on 2026-11-05.
# Until then, set PRIZED_SDK_DIST to a directory holding prized-*.whl and prized-sdk-*.tgz built
# from the SDK sources; without it, a folder that needs an SDK the registry doesn't have yet is
# skipped with a warning (REQUIRE_SDK=1 makes it an error, as the publish workflow does). Once both
# are published, delete the skip in need_sdk() so a missing SDK fails the run instead.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DIR=${1:?usage: scripts/test.sh <folder>}
DIR=${DIR%/}
[ -d "$ROOT/$DIR" ] || { echo "no folder $DIR" >&2; exit 2; }
PYTHON=${PYTHON:-python3}
FLOOR=${FLOOR:-}
HERMES_AGENT_SHA=05d7b5af1c42d524762c7ffce991c1fe1aa2adb1 # NousResearch/hermes-agent main, 2026-10-26
HERMES_UV_VERSION=0.12.13 # the uv Hermes pins in its own CI
ANT_VERSION=2.3.0
ANT_SHA256=cd3fa57af3efbdbf68d569d0b921a69171b53be14cc5e013b35018ccb0f1d8ab # ant_2.3.0_linux_amd64.tar.gz

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
note() { echo "::$1::$DIR: $2"; }

# need_sdk python|node: install the SDK from PRIZED_SDK_DIST, or check the registry has it.
need_sdk() {
  if [ -n "${PRIZED_SDK_DIST:-}" ]; then
    case $1 in
      python) "$WORK/venv/bin/pip" install -q "$PRIZED_SDK_DIST"/prized-*.whl ;;
      node) SDK_TGZ=$(ls "$PRIZED_SDK_DIST"/prized-sdk-*.tgz) ;;
    esac
    return 0
  fi
  case $1 in
    python) curl -fsS -o /dev/null https://pypi.org/pypi/prized/json && return 0 ;;
    node) curl -fsS -o /dev/null https://registry.npmjs.org/@prized%2fsdk && return 0 ;;
  esac
  if [ -n "${REQUIRE_SDK:-}" ]; then note error "the Prized $1 SDK is not on its registry"; exit 1; fi
  note warning "skipped: the Prized $1 SDK is not on its registry yet (set PRIZED_SDK_DIST)"
  exit 0
}

# offline CMD...: run CMD as this user in a network namespace of its own (loopback only), through
# sudo (CI runners) or user namespaces, whichever works here; else as is, with a notice.
via_sudo() {
  sudo -n --preserve-env unshare --net -- sh -c 'ip link set lo up && exec "$@"' sh \
    setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups -- "$@"
}
via_userns() {
  unshare --user --map-root-user --net -- sh -c 'ip link set lo up && exec "$@"' sh \
    unshare --user --map-user="$(id -u)" --map-group="$(id -g)" -- "$@"
}
offline() {
  local clean=(env -u PRIZED_API_KEY -u PRIZED_LIVE -u PRIZED_LIVE_TESTS -u ANTHROPIC_API_KEY -u OPENROUTER_API_KEY
    PATH="$PATH" HOME="$HOME")
  local isolated me
  me=$(id -u)
  isolated="[ \"\$(id -u)\" = $me ] && ! getent ahosts pypi.org >/dev/null"
  if via_sudo sh -c "$isolated" 2>/dev/null; then
    via_sudo "${clean[@]}" "$@"
  elif via_userns sh -c "$isolated" 2>/dev/null; then
    via_userns "${clean[@]}" "$@"
  else
    note notice "network stays on for the tests (no sudo or user namespaces here)"
    "${clean[@]}" "$@"
  fi
}

venv() {
  "$PYTHON" -m venv "$WORK/venv"
  "$WORK/venv/bin/pip" install -q --upgrade pip
  export PATH="$WORK/venv/bin:$PATH"
}

# py FOLDER_EXTRAS SDK FLOOR_PINS LINT: the usual Python package flow.
py() {
  local extras=$1 sdk=$2 floor=$3 lint=$4
  venv
  [ "$sdk" = no ] || need_sdk python
  # shellcheck disable=SC2086 # pins are a word list
  pip install -q -e ".[$extras]" ${FLOOR:+$floor}
  pip list 2>/dev/null | grep -i -E "^(prized|inspect.ai|google.adk|strands.agents|agent.framework|pydantic.ai|harbor)[-_ ]" || true
  if [ -z "$FLOOR" ]; then sh -c "$lint"; else echo "(lint runs on the latest-dependencies row)"; fi
  offline pytest -rs -p no:cacheprovider
}

cd "$ROOT/$DIR"
case $DIR in
  inspect)
    py dev yes "inspect-ai==0.3.280" "ruff check . && ruff format --check ." ;;
  adk)
    py "test" yes "google-adk==1.30.0" \
      "pip install -q pyink==25.12.0 isort==8.0.1 && pyink --check src tests examples && isort --check-only src tests examples" ;;
  strands)
    py dev yes "strands-agents==1.59.0" "ruff check . && ruff format --check . && mypy src" ;;
  agent-framework)
    py dev yes "agent-framework-core==1.19.0 agent-framework-tools==1.0.0b260918" \
      "ruff check . && ruff format --check . && mypy src" ;;
  pydantic-ai)
    py "test" yes "pydantic-ai-slim==2.59.0" "pip install -q ruff && ruff check . && ruff format --check ." ;;
  harbor)
    py "test" no "harbor==0.24.0" "pip install -q ruff && ruff check . && ruff format --check ." ;;
  hermes)
    # The plugin's tests import Hermes itself, so they run in a pinned hermes-agent checkout's own
    # environment, installed from its lockfile the way Hermes' CI does (uv, Python 3.14).
    venv
    pip install -q "uv==$HERMES_UV_VERSION"
    git -c advice.detachedHead=false clone -q --filter=blob:none https://github.com/NousResearch/hermes-agent "$WORK/hermes-agent"
    git -C "$WORK/hermes-agent" checkout -q "$HERMES_AGENT_SHA"
    (cd "$WORK/hermes-agent" && uv sync -q --locked --python 3.14 --group dev)
    HERMES_AGENT_REPO="$WORK/hermes-agent" offline "$WORK/hermes-agent/.venv/bin/python" -m pytest -rs -p no:cacheprovider tests ;;
  claude-managed-agents)
    # The test drives on-work.sh with the real `ant` poller, checked against its published sha256.
    curl -fsSL -o "$WORK/ant.tgz" \
      "https://github.com/anthropics/anthropic-cli/releases/download/v$ANT_VERSION/ant_${ANT_VERSION}_linux_amd64.tar.gz"
    echo "$ANT_SHA256  $WORK/ant.tgz" | sha256sum -c --quiet -
    tar -xzf "$WORK/ant.tgz" -C "$WORK" ant
    ANT="$WORK/ant" offline bash test/test.sh ;;
  langchain-js)
    # No lockfile until @prized/sdk is on npm (the one made here would point at a local tarball).
    need_sdk node
    cp -r "$ROOT/$DIR/." "$WORK/pkg"
    cd "$WORK/pkg"
    [ -z "${SDK_TGZ:-}" ] || npm pkg set "dependencies.@prized/sdk=file:$SDK_TGZ"
    npm install --no-audit --no-fund --no-package-lock --loglevel=error
    npm run typecheck
    npm run build
    offline npm test ;;
  *)
    echo "scripts/test.sh doesn't know $DIR yet: add a case for it" >&2; exit 2 ;;
esac
