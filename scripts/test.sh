#!/usr/bin/env bash
# Lint and test one integration offline, the way CI does: a fresh virtualenv (or a temp copy for
# Node), tests run with an empty environment (env -i) and a fresh HOME, and, where the machine allows
# it, no network while they run.
#
#   scripts/test.sh <folder>            # latest dependencies
#   FLOOR=1 scripts/test.sh <folder>    # the oldest framework versions the package supports
#   PYTHON=python3.10 scripts/test.sh inspect
#
# Live tests are never run here: they need PRIZED_API_KEY plus a flag, and no test step sees either.
#
# The Prized SDKs come from their registries (`prized` on PyPI, `@prized/sdk` on npm), as each
# folder's dependencies say. To test against an SDK build that isn't published, set PRIZED_SDK_DIST to a
# directory holding the prized-*.whl and prized-sdk-*.tgz built from the SDK sources.
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
DIFY_CLI_VERSION=0.6.12 # langgenius/dify-plugin-daemon release: the Dify plugin CLI
DIFY_CLI_SHA256=f5190893643492c3f17f12c9752615a5652e47bbf30e84298634e176611aa108 # dify-plugin-linux-amd64
DIFY_UV_VERSION=0.12.13
OPENAI_AGENTS_UV_VERSION=0.13.0 # the uv openai-agents/tests/package_check.py builds with
OPENAI_AGENTS_LINT="ruff==0.17.1 ty==0.0.90" # pinned: a new ruff or ty alone never turns CI red
OPENAI_AGENTS_FLOOR="openai-agents==0.23.1 httpx==0.27.0" # the oldest versions openai-agents-prized allows

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
note() { echo "::$1::$DIR: $2"; }

# need_sdk python|node: with PRIZED_SDK_DIST, install the SDK from there instead of the registry.
need_sdk() {
  [ -n "${PRIZED_SDK_DIST:-}" ] || return 0
  case $1 in
    python) "$WORK/venv/bin/pip" install -q "$PRIZED_SDK_DIST"/prized-*.whl ;;
    node) SDK_TGZ=$(ls "$PRIZED_SDK_DIST"/prized-sdk-*.tgz) ;;
  esac
}

# scrubbed [NAME=value...] CMD...: run CMD with an empty environment plus PATH, a fresh HOME and the
# NAME=value pairs given, so no key, token, proxy or API endpoint set in this shell (or kept in the
# real HOME) reaches a test.
scrubbed() {
  mkdir -p "$WORK/home"
  env -i PATH="$PATH" HOME="$WORK/home" "$@"
}

# offline [NAME=value...] CMD...: scrubbed, and as this user in a network namespace of its own
# (loopback only), through sudo (CI runners) or user namespaces, whichever works here; else as is,
# with a notice.
via_sudo() {
  sudo -n --preserve-env unshare --net -- sh -c 'ip link set lo up && exec "$@"' sh \
    setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups -- "$@"
}
via_userns() {
  unshare --user --map-root-user --net -- sh -c 'ip link set lo up && exec "$@"' sh \
    unshare --user --map-user="$(id -u)" --map-group="$(id -g)" -- "$@"
}
offline() {
  mkdir -p "$WORK/home"
  local clean=(env -i PATH="$PATH" HOME="$WORK/home")
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
  devin-outposts)
    # Offline: a stand-in for Devin's Outposts service on 127.0.0.1, fake sandboxes that run the
    # in-sandbox scripts with bash, and a fake devin-remote. Live tests skip without PRIZED_LIVE=1.
    py "test" yes "prized==0.1.0 httpx==0.24.1" "pip install -q ruff && ruff check . && ruff format --check ." ;;
  openai-agents)
    # The package's test dependency group (pyproject.toml [dependency-groups]); FLOOR=1 pins the
    # oldest openai-agents and httpx it allows and runs the offline tests only. The latest row
    # adds lint and types, then the build as PyPI gets it: wheel and sdist, twine check, and a
    # clean virtualenv in which the installed wheel runs a scripted agent through Runner against
    # the fake API (tests/package_check.py; it installs from PyPI, so it runs scrubbed but online).
    venv
    if [ -n "$FLOOR" ]; then
      # shellcheck disable=SC2086 # pins are a word list
      pip install -q -e . --group test $OPENAI_AGENTS_FLOOR
    else
      # shellcheck disable=SC2086 # pins are a word list
      pip install -q -e . --group test --group release "uv==$OPENAI_AGENTS_UV_VERSION" $OPENAI_AGENTS_LINT
    fi
    pip list 2>/dev/null | grep -i -E "^(openai-agents|httpx|ruff|ty) " || true
    if [ -z "$FLOOR" ]; then
      ruff check .
      ruff format --check .
      ty check openai_agents_prized
    fi
    offline pytest -rs -p no:cacheprovider --disable-socket --allow-unix-socket
    [ -n "$FLOOR" ] || scrubbed python tests/package_check.py ;;
  hermes)
    # The plugin's tests import Hermes itself, so they run in a pinned hermes-agent checkout's own
    # environment, installed from its lockfile the way Hermes' CI does (uv, Python 3.14).
    venv
    pip install -q "uv==$HERMES_UV_VERSION"
    git -c advice.detachedHead=false clone -q --filter=blob:none https://github.com/NousResearch/hermes-agent "$WORK/hermes-agent"
    git -C "$WORK/hermes-agent" checkout -q "$HERMES_AGENT_SHA"
    (cd "$WORK/hermes-agent" && uv sync -q --locked --python 3.14 --group dev)
    offline HERMES_AGENT_REPO="$WORK/hermes-agent" "$WORK/hermes-agent/.venv/bin/python" -m pytest -rs -p no:cacheprovider tests ;;
  claude-managed-agents)
    # The test drives on-work.sh with the real `ant` poller, checked against its published sha256.
    curl -fsSL -o "$WORK/ant.tgz" \
      "https://github.com/anthropics/anthropic-cli/releases/download/v$ANT_VERSION/ant_${ANT_VERSION}_linux_amd64.tar.gz"
    echo "$ANT_SHA256  $WORK/ant.tgz" | sha256sum -c --quiet -
    tar -xzf "$WORK/ant.tgz" -C "$WORK" ant
    offline ANT="$WORK/ant" bash test/test.sh ;;
  cursor)
    # spawn.sh against a stand-in `prized` and a stub Cursor `agent` (test/): no keys, no network.
    offline bash test/test.sh ;;
  dify)
    # A Dify plugin ships as a .difypkg, not a wheel: requirements.txt is what Dify installs, and
    # pyproject.toml holds only the lint and test settings.
    venv
    pip install -q -r requirements.txt pytest==9.1.1 ruff==0.17.2 "uv==$DIFY_UV_VERSION"
    pip list 2>/dev/null | grep -i -E "^(dify.plugin|httpx) " || true
    ruff check .
    ruff format --check .
    offline pytest -rs -p no:cacheprovider
    # Then inside Dify's own plugin runtime: the pinned Dify plugin CLI, checked against its published
    # sha256, packages the plugin and runs it (`dify plugin run`) against a fake API on 127.0.0.1. The
    # network stays up for this step, for the runtime's own install of requirements.txt; the
    # environment is scrubbed all the same (DIFY_PACKAGE_OUT, where to keep the package, passes).
    curl -fsSL -o "$WORK/dify" \
      "https://github.com/langgenius/dify-plugin-daemon/releases/download/$DIFY_CLI_VERSION/dify-plugin-linux-amd64"
    echo "$DIFY_CLI_SHA256  $WORK/dify" | sha256sum -c --quiet -
    chmod +x "$WORK/dify"
    scrubbed DIFY_CLI_PATH="$WORK/dify" ${DIFY_PACKAGE_OUT:+"DIFY_PACKAGE_OUT=$DIFY_PACKAGE_OUT"} \
      pytest -rs -p no:cacheprovider tests/test_dify_runtime.py ;;
  langchain-js)
    # No lockfile: like the Python folders, the latest row installs the newest versions the package allows.
    need_sdk node
    cp -r "$ROOT/$DIR/." "$WORK/pkg"
    cd "$WORK/pkg"
    [ -z "${SDK_TGZ:-}" ] || npm pkg set "dependencies.@prized/sdk=file:$SDK_TGZ"
    npm install --no-audit --no-fund --no-package-lock --loglevel=error
    npm run typecheck
    npm run build
    offline npm test ;;
  mastra)
    # Same flow as langchain-js; FLOOR=1 pins the oldest @mastra/core the package supports.
    need_sdk node
    cp -r "$ROOT/$DIR/." "$WORK/pkg"
    cd "$WORK/pkg"
    [ -z "${SDK_TGZ:-}" ] || npm pkg set "dependencies.@prized/sdk=file:$SDK_TGZ"
    [ -z "$FLOOR" ] || npm pkg set "devDependencies.@mastra/core=1.79.0"
    npm install --no-audit --no-fund --no-package-lock --loglevel=error
    npm ls --depth=0 @mastra/core @prized/sdk || true
    npm run typecheck
    npm run build
    offline npm test ;;
  *)
    echo "scripts/test.sh doesn't know $DIR yet: add a case for it" >&2; exit 2 ;;
esac
