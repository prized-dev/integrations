#!/usr/bin/env bash
# Move a Conductor repo to a Prized box in one command.
#
# Run it on your Mac, in the repo's main checkout (the folder Conductor's workspaces come from):
#
#   curl -fsSL https://raw.githubusercontent.com/prized-dev/integrations/main/conductor/move.sh | bash
#
# It puts the repo on your Prized box with `prized setup --clone`, then does what Conductor did
# for each new workspace: a git worktree on its own branch, the gitignored files Conductor copies
# (.worktreeinclude, file_include_globs, or .env*), and the repo's setup script from
# .conductor/settings.toml or conductor.json, run with Conductor's variables (CONDUCTOR_PORT and
# the rest). Then it starts Claude Code or Codex in each worktree in tmux, so the agents keep
# working with your laptop closed. Safe to re-run. `--help` lists the options.
#
# Everything is inside functions and main runs on the last line, so under `curl | bash` the whole
# script is read before anything runs.
set -euo pipefail

HELPER=.local/share/prized-conductor/move.py

usage() {
  cat <<'EOF'
Usage: move.sh [REPO] [options] [-- PRIZED_SETUP_ARGS...]

REPO is your repo's main checkout (default: this folder), or a GitHub repo
(owner/repo or its URL) to clone on the box without a checkout here.

  -w, --workspace BRANCH  a workspace (git worktree) on BRANCH: checked out if
                          the repo has it, else a new branch from the default
                          branch. Repeat it; default: two new, ws-1 and ws-2
  -n N                    how many new workspaces when no -w is given (default 2)
  --agent claude|codex|none
                          the agent to start in each workspace (default claude)
  --run                   also start each workspace's run script (Conductor's
                          Run button) in a tmux session run-BRANCH
  --setup FILE            run FILE instead of the repo's setup script, such as
                          the repository setup script in Conductor Cloud's
                          settings (it then sees CONDUCTOR_IS_LOCAL=0, as in a
                          cloud workspace)
  --install FILE          run FILE once on the box first, from your home folder,
                          such as your Cloud Computer's install script
  --files, --no-files     copy the gitignored files Conductor copies into each
                          workspace without asking, or never (default: ask)
  --box NAME              the box to use (default: your current box)
  --port-base N           the first workspace's CONDUCTOR_PORT (default 4100);
                          each workspace gets 10 ports, as in Conductor
  --archive BRANCH        archive a workspace: run the archive script, stop its
                          sessions, remove its worktree (the branch stays); with
                          uncommitted changes it stops, unless --force
  --print-helper          print the Python this runs on the box, and stop
  -y, --yes               answer yes to every question, prized setup's too
  -h, --help              this help
  -- ARGS                 passed on to prized setup as they are

Docs: https://prized.dev/docs/moving-from-conductor-cloud?via=conductor
EOF
}

die() { printf 'move: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*" >&2; }
# q WORD: WORD single-quoted for the box's sh.
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
# Every prized call reads /dev/null, never this script's stdin, which under `curl | bash` is the
# script itself.
pz() { prized "$@" </dev/null; }
# box CMD: run one sh command line on the box, from the home folder there.
box() { prized exec --timeout 0 -- "$1" </dev/null; }
tty_ok() { [ "$YES" = 0 ] && (: </dev/tty) 2>/dev/null; }
# ask QUESTION: yes with -y; otherwise the answer from the terminal (Enter is yes); no terminal is no.
ask() {
  [ "$YES" = 1 ] && return 0
  tty_ok || return 1
  local a
  printf '%s [Y/n] ' "$1" >/dev/tty
  IFS= read -r a </dev/tty || return 1
  case $a in '' | y | Y | yes | Yes | YES) return 0 ;; *) return 1 ;; esac
}
# name_for FOLDER: the name prized setup gives the project's folder on the box (~/code/<name>).
name_for() {
  printf '%s' "$1" | LC_ALL=C tr '[:upper:]' '[:lower:]' | LC_ALL=C sed -E 's/[^a-z0-9._-]+/-/g; s/-+/-/g; s/^[._-]+//; s/-+$//'
}
tmpfile() { mktemp "${TMPDIR:-/tmp}/prized-conductor.XXXXXX"; }
put_helper() {
  local t
  t=$(tmpfile)
  helper_py >"$t"
  box "mkdir -p .local/share/prized-conductor" >/dev/null || { rm -f "$t"; die "couldn't reach the box"; }
  pz cp "$t" ":$HELPER" >/dev/null || { rm -f "$t"; die "couldn't copy the helper to the box"; }
  rm -f "$t"
}

main() {
  SRC='' AGENT=claude RUN=0 SETUP_FILE='' INSTALL_FILE='' FILES=ask BOX='' PORT_BASE=4100 ARCHIVE='' FORCE='' YES=0 COUNT=2
  BRANCHES=() PASS=()
  while [ $# -gt 0 ]; do
    case $1 in
      -w | --workspace) [ $# -ge 2 ] || die "$1 needs a branch name"; BRANCHES+=("$2"); shift 2 ;;
      -n) [ $# -ge 2 ] || die "-n needs a number"; COUNT=$2; shift 2 ;;
      --agent) [ $# -ge 2 ] || die "--agent needs claude, codex or none"; AGENT=$2; shift 2 ;;
      --run) RUN=1; shift ;;
      --setup) [ $# -ge 2 ] || die "--setup needs a file"; SETUP_FILE=$2; shift 2 ;;
      --install) [ $# -ge 2 ] || die "--install needs a file"; INSTALL_FILE=$2; shift 2 ;;
      --files) FILES=yes; shift ;;
      --no-files) FILES=no; shift ;;
      --box) [ $# -ge 2 ] || die "--box needs a box name"; BOX=$2; shift 2 ;;
      --port-base) [ $# -ge 2 ] || die "--port-base needs a port"; PORT_BASE=$2; shift 2 ;;
      --archive) [ $# -ge 2 ] || die "--archive needs a branch name"; ARCHIVE=$2; shift 2 ;;
      --force) FORCE=" --force"; shift ;;
      --print-helper) helper_py; exit 0 ;;
      -y | --yes) YES=1; shift ;;
      -h | --help) usage; exit 0 ;;
      --) shift; PASS=("$@"); break ;;
      -*) die "unknown option $1 (--help lists them)" ;;
      *) [ -z "$SRC" ] || die "one repo at a time ($SRC, then $1)"; SRC=$1; shift ;;
    esac
  done

  case $AGENT in claude | codex | none) ;; *) die "--agent takes claude, codex or none, not $AGENT" ;; esac
  case $COUNT in '' | *[!0-9]*) die "-n takes a number, not $COUNT" ;; esac
  if [ "$COUNT" -lt 1 ] || [ "$COUNT" -gt 20 ]; then die "-n takes 1 to 20"; fi
  case $PORT_BASE in '' | *[!0-9]*) die "--port-base takes a port number" ;; esac
  if [ "$PORT_BASE" -lt 1024 ] || [ "$PORT_BASE" -gt 65000 ]; then die "--port-base takes 1024 to 65000"; fi
  [ -z "$SETUP_FILE" ] || [ -f "$SETUP_FILE" ] || die "no setup script at $SETUP_FILE"
  [ -z "$INSTALL_FILE" ] || [ -f "$INSTALL_FILE" ] || die "no install script at $INSTALL_FILE"
  if [ ${#BRANCHES[@]} -eq 0 ]; then
    local i=1
    while [ "$i" -le "$COUNT" ]; do BRANCHES+=("ws-$i"); i=$((i + 1)); done
  fi
  local b
  for b in "${BRANCHES[@]}" ${ARCHIVE:+"$ARCHIVE"}; do
    case $b in
      -* | */ | /* | *..* | *//* | *.lock | *[!A-Za-z0-9._/-]*) die "\"$b\" is not a branch name this can use (letters, digits, . _ - /)" ;;
    esac
  done
  [ -z "$BOX" ] || export PRIZED_BOX=$BOX

  command -v prized >/dev/null 2>&1 || die "the Prized CLI isn't installed. Sign up at https://prized.dev/sign-in?via=conductor (the tour installs it and signs this machine in), then run this again"
  command -v git >/dev/null 2>&1 || die "git isn't installed"

  local TOP='' SLUG='' NAME
  if [ -z "$SRC" ] || [ -d "$SRC" ]; then
    TOP=$(git -C "${SRC:-.}" rev-parse --show-toplevel 2>/dev/null) || die "${SRC:-this folder} is not a git checkout. Run this in your repo's main checkout, or pass owner/repo"
    git -C "$TOP" remote get-url origin >/dev/null 2>&1 || die "$TOP has no origin remote. Push it to GitHub first: the box clones it from there"
    NAME=$(name_for "$(basename "$TOP")")
  else
    SLUG=${SRC%/}
    SLUG=${SLUG%.git}
    SLUG=${SLUG#https://github.com/}
    SLUG=${SLUG#http://github.com/}
    SLUG=${SLUG#git@github.com:}
    SLUG=${SLUG#github.com/}
    case $SLUG in
      */*/* | -* | */ | /* | *[!A-Za-z0-9._/-]*) die "$SRC is neither a folder here nor a GitHub repo (owner/repo)" ;;
      */*) ;;
      *) die "$SRC is neither a folder here nor a GitHub repo (owner/repo)" ;;
    esac
    NAME=$(name_for "${SLUG#*/}")
  fi
  [ -n "$NAME" ] || die "can't make a folder name for the box from ${SRC:-$TOP}"
  local ROOT=code/$NAME started=$SECONDS

  if [ -n "$ARCHIVE" ]; then
    put_helper
    box "python3 -I $HELPER archive $(q "$ROOT") --project $(q "$NAME") $(q "$ARCHIVE")$FORCE"
    return
  fi

  # 1. The repo on the box, with your agent and gh signed in there: prized setup.
  say "Moving $NAME to Prized. First the repo, with prized setup:"
  local SETUP_ARGS=(--no-start --no-attach) code=0
  # Naming the agent is setup's grant to sign it in on the box; --no-agents after -- skips that.
  case " ${PASS[*]-} " in *" --no-agents "*) ;; *) [ "$AGENT" = none ] || SETUP_ARGS+=(--agent "$AGENT") ;; esac
  [ "$YES" = 0 ] || SETUP_ARGS+=(-y)
  if [ -n "$TOP" ]; then SETUP_ARGS=("$TOP" --clone "${SETUP_ARGS[@]}"); else SETUP_ARGS=(--no-project "${SETUP_ARGS[@]}"); fi
  if tty_ok; then
    prized setup "${SETUP_ARGS[@]}" ${PASS[@]+"${PASS[@]}"} </dev/tty || code=$?
  else
    prized setup "${SETUP_ARGS[@]}" ${PASS[@]+"${PASS[@]}"} </dev/null || code=$?
  fi
  if [ -n "$SLUG" ]; then
    # No checkout here: clone it on the box, with gh when setup signed gh in there.
    box "if [ -d $ROOT/.git ]; then echo '~/$ROOT is already on the box'; elif gh auth status >/dev/null 2>&1; then gh repo clone $(q "$SLUG") $ROOT; else git clone -- $(q "https://github.com/$SLUG.git") $ROOT; fi" >&2 ||
      die "couldn't clone $SLUG on the box. A private repo needs gh signed in there: run prized setup --gh, then this again"
  fi
  box "test -d $ROOT/.git" >/dev/null 2>&1 || die "prized setup didn't put $NAME at ~/$ROOT on the box (exit $code). Fix what it said above, then run this again"
  [ "$code" = 0 ] || say "prized setup exited $code, but the repo is on the box, so carrying on"
  put_helper

  # 2. The files only this checkout has: Conductor's local settings and the Cloud scripts you saved.
  if [ -n "$TOP" ] && [ -f "$TOP/.conductor/settings.local.toml" ] && ! git -C "$TOP" ls-files --error-unmatch .conductor/settings.local.toml >/dev/null 2>&1; then
    box "mkdir -p $ROOT/.conductor" >/dev/null
    pz cp "$TOP/.conductor/settings.local.toml" ":$ROOT/.conductor/settings.local.toml" >/dev/null && say "Copied .conductor/settings.local.toml"
  fi
  [ -z "$SETUP_FILE" ] || pz cp "$SETUP_FILE" ":.local/share/prized-conductor/$NAME.setup.sh" >/dev/null
  if [ -n "$INSTALL_FILE" ]; then
    pz cp "$INSTALL_FILE" ":.local/share/prized-conductor/install.sh" >/dev/null
    say "Running $(basename "$INSTALL_FILE") once on the box, from your home folder:"
    box "bash -l -c 'set -euo pipefail; . .local/share/prized-conductor/install.sh'" >&2 ||
      die "$(basename "$INSTALL_FILE") failed on the box. The box runs Ubuntu 24.04, so dnf lines become apt-get. Fix it, then run this again"
  fi

  # 3. What the repo's Conductor config holds, and the gitignored files Conductor copies.
  local PATTERNS
  PATTERNS=$(box "python3 -I $HELPER plan $(q "$ROOT")${SETUP_FILE:+ --setup}") || die "couldn't read the Conductor config on the box"
  if [ "$FILES" != no ] && [ -n "$TOP" ] && [ -n "$PATTERNS" ]; then
    local pat list n tgz
    pat=$(tmpfile) list=$(tmpfile)
    printf '%s\n' "$PATTERNS" >"$pat"
    # Conductor copies a file when it matches a pattern and git ignores it.
    git -C "$TOP" ls-files -z --others --ignored --exclude-from="$pat" 2>/dev/null |
      git -C "$TOP" check-ignore -z --stdin >"$list" 2>/dev/null || true
    n=$(tr -cd '\0' <"$list" | wc -c | tr -d ' ')
    if [ "$n" -gt 0 ]; then
      say "Conductor copies $n gitignored file(s) from this checkout into each workspace:"
      tr '\0' '\n' <"$list" | head -n 15 | sed 's/^/  /' >&2
      [ "$n" -le 15 ] || say "  and $((n - 15)) more"
      if [ "$FILES" = yes ] || ask "Copy them to the box too? They can hold secrets."; then
        tgz=$(tmpfile)
        COPYFILE_DISABLE=1 tar -C "$TOP" -czf "$tgz" --null -T "$list"
        pz cp "$tgz" ":.local/share/prized-conductor/files.tgz" >/dev/null
        box "tar -xzpf .local/share/prized-conductor/files.tgz -C $ROOT --warning=no-unknown-keyword; s=\$?; rm -f .local/share/prized-conductor/files.tgz; exit \$s" >&2 ||
          die "couldn't unpack them on the box"
        rm -f "$tgz"
      else
        say "  Not copied: --files copies them, --no-files stops this question."
      fi
    fi
    rm -f "$pat" "$list"
  fi

  # 4. A worktree per workspace, set up the way Conductor sets one up, each with its own tmux session.
  local APPLY OUT
  APPLY="python3 -I $HELPER apply $(q "$ROOT") --project $(q "$NAME") --agent $AGENT --port-base $PORT_BASE"
  [ "$RUN" = 0 ] || APPLY="$APPLY --run"
  [ -z "$SETUP_FILE" ] || APPLY="$APPLY --setup $(q ".local/share/prized-conductor/$NAME.setup.sh")"
  for b in "${BRANCHES[@]}"; do APPLY="$APPLY $(q "$b")"; done
  OUT=$(box "$APPLY") || die "setting up the workspaces failed (above). Fix it and run this again: it picks up where it stopped"

  # 5. An agent in each workspace, through prized agents run.
  local AGENT_ARGS=() ROWS=() kind ws branch dir port setup session first='' r
  case $AGENT in
    claude) AGENT_ARGS=(--permission-mode auto) ;;
    codex) AGENT_ARGS=(--sandbox workspace-write --ask-for-approval never) ;;
  esac
  while IFS="$(printf '\t')" read -r kind ws branch dir port setup session; do
    [ "$kind" = ws ] || continue
    if [ "$session" != - ]; then
      [ -n "$first" ] || first=$session
      pz agents run "$AGENT" --session "$session" --dir "$dir" -- ${AGENT_ARGS[@]+"${AGENT_ARGS[@]}"} >/dev/null 2>&1 ||
        say "$ws: $AGENT didn't start. Start it with: prized agents run $AGENT --session $session --dir $dir"
    fi
    r="  $ws (branch $branch): ~/$dir, CONDUCTOR_PORT $port, setup $setup"
    [ "$session" = - ] || r="$r, prized ssh -s $session"
    ROWS+=("$r")
  done <<EOF
$OUT
EOF

  local t=$((SECONDS - started))
  say ""
  say "Moved in $((t / 60))m$((t % 60))s. $NAME is on your box at ~/$ROOT, with a worktree per workspace:"
  for r in ${ROWS[@]+"${ROWS[@]}"}; do say "$r"; done
  say ""
  [ -z "$first" ] || say "Open an agent:  prized ssh -s $first   (leave it running: Ctrl-b, then d)"
  say "All sessions:   prized ssh --ls"
  say "Ports:          a server on its CONDUCTOR_PORT opens at localhost on this machine"
  say "Archive one:    run this again with --archive BRANCH"
  say "Variables and secrets your Cloud Computer set go on the box page's Settings tab."
}

# The Python that runs on the box (python3 -I, standard library only). --print-helper prints it.
helper_py() {
  cat <<'PY'
# Box side of conductor/move.sh (prized-dev/integrations): runs on the Prized box with python3 -I.
# plan ROOT: says what the repo's Conductor config holds (stderr) and prints its Files to copy
#   patterns (stdout).
# apply ROOT --project NAME ... BRANCH...: a worktree, copied files, setup script and tmux session
#   per workspace; prints one tab-separated "ws" line per workspace (stdout).
# archive ROOT --project NAME BRANCH: the archive script, then the worktree removed.
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".local", "share", "prized-conductor")
RESERVED = {"HOME", "PATH", "PORT", "NODE_PATH", "SHELL", "USER", "PWD"}


def say(msg=""):
    print(msg, file=sys.stderr, flush=True)


def die(msg):
    say("move: " + msg)
    sys.exit(1)


def tilde(path):
    return "~" + path[len(HOME) :] if path == HOME or path.startswith(HOME + "/") else path


def load_toml(path):
    try:
        import tomllib
    except ImportError:
        die("reading %s needs Python 3.11 or later on the box" % path)
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:
        die("can't read %s: %s" % (path, e))


def merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def text(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def read_config(root, mode):
    """The repo's Conductor config, read as Conductor reads it: .conductor/settings.toml when it
    exists (Conductor then ignores conductor.json), else the legacy conductor.json, with
    .conductor/settings.local.toml over either."""
    cfg, sources = {}, []
    shared = os.path.join(root, ".conductor", "settings.toml")
    legacy = os.path.join(root, "conductor.json")
    local = os.path.join(root, ".conductor", "settings.local.toml")
    if os.path.isfile(shared):
        cfg, sources = load_toml(shared), [".conductor/settings.toml"]
    elif os.path.isfile(legacy):
        try:
            with open(legacy, encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError) as e:
            die("can't read conductor.json: %s" % e)
        if not isinstance(j, dict):
            die("conductor.json is not a JSON object")
        s = j.get("scripts") if isinstance(j.get("scripts"), dict) else {}
        scripts = {}
        for k in ("setup", "run", "archive"):
            v = s.get(k, j.get(k))  # a few repos put them at the top level
            if isinstance(v, str) and v.strip():
                scripts[k] = v
        if isinstance(j.get("runScriptMode"), str):
            scripts["run_mode"] = j["runScriptMode"]
        cfg, sources = {"scripts": scripts}, ["conductor.json"]
    if os.path.isfile(local):
        cfg = merge(cfg, load_toml(local))
        sources.append(".conductor/settings.local.toml")

    scripts = cfg.get("scripts") if isinstance(cfg.get("scripts"), dict) else {}
    setup = scripts.get("setup") if isinstance(scripts.get("setup"), str) else ""
    archive = scripts.get("archive") if isinstance(scripts.get("archive"), str) else ""
    actions = []
    run = scripts.get("run")
    if isinstance(run, str) and run.strip():
        actions.append({"id": "run", "command": run, "cwd": "", "default": True})
    elif isinstance(run, dict):
        for aid, a in run.items():
            if not isinstance(a, dict) or a.get("hide") is True:
                continue
            cmd = a.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            if isinstance(a.get("args"), list) and a["args"]:
                cmd += " " + " ".join(shlex.quote(text(x)) for x in a["args"])
            opts = a.get("options") if isinstance(a.get("options"), dict) else {}
            cwd = opts.get("cwd") if isinstance(opts.get("cwd"), str) else ""
            actions.append({"id": str(aid), "command": cmd, "cwd": cwd, "default": a.get("default") is True})
    if actions and not any(a["default"] for a in actions):
        actions[0]["default"] = True

    env, skipped = {}, []
    ev = cfg.get("environment_variables") if isinstance(cfg.get("environment_variables"), dict) else {}
    layered = {k: v for k, v in ev.items() if not isinstance(v, dict)}
    if isinstance(ev.get(mode), dict):
        layered.update(ev[mode])
    for k, v in layered.items():
        if isinstance(v, (dict, list)):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or k in RESERVED or k.startswith("CONDUCTOR_"):
            skipped.append(k)
            continue
        env[k] = text(v)

    wti = os.path.join(root, ".worktreeinclude")
    globs = cfg.get("file_include_globs")
    if os.path.isfile(wti):
        with open(wti, encoding="utf-8", errors="replace") as f:
            patterns, psrc = f.read(), ".worktreeinclude"
    elif isinstance(globs, str):
        patterns, psrc = globs, "file_include_globs"
    elif isinstance(globs, list):
        patterns, psrc = "\n".join(text(g) for g in globs), "file_include_globs"
    else:
        patterns, psrc = ".env*", "Conductor's default"
    prompts = cfg.get("prompts") if isinstance(cfg.get("prompts"), dict) else {}
    general = prompts.get("general") if isinstance(prompts.get("general"), str) else ""
    return {
        "sources": sources,
        "setup": setup,
        "archive": archive,
        "actions": actions,
        "run_mode": scripts.get("run_mode") if isinstance(scripts.get("run_mode"), str) else "concurrent",
        "env": env,
        "env_skipped": skipped,
        "patterns": patterns.strip(),
        "patterns_from": psrc,
        "general_prompt": general.strip(),
    }


def first_line(s, width=70):
    lines = [x for x in s.strip().splitlines() if x.strip()]
    one = lines[0].strip() if lines else ""
    more = " (+%d lines)" % (len(lines) - 1) if len(lines) > 1 else ""
    return (one[: width - 1] + "…" if len(one) > width else one) + more


def describe(c, setup_override):
    say("Conductor config: " + (", ".join(c["sources"]) or "none in the repo"))
    if setup_override:
        say("  setup    your --setup script")
    elif c["setup"]:
        say("  setup    " + first_line(c["setup"]))
    else:
        say("  setup    none (no setup script in the repo)")
    for a in c["actions"]:
        say("  run      %s%s: %s" % (a["id"], " (default)" if a["default"] and len(c["actions"]) > 1 else "", first_line(a["command"], 60)))
    if c["archive"]:
        say("  archive  " + first_line(c["archive"]))
    if c["env"]:
        say("  env      " + ", ".join(sorted(c["env"])))
    if c["env_skipped"]:
        say("  env      not set (reserved or not a variable name): " + ", ".join(sorted(c["env_skipped"])))
    if c["patterns"]:
        say("  copies   %s (%s)" % (", ".join(p for p in c["patterns"].splitlines() if p.strip() and not p.lstrip().startswith("#")), c["patterns_from"]))
    if c["general_prompt"]:
        say("  prompt   Conductor added prompts.general to every chat; put it in CLAUDE.md or AGENTS.md for the agents here")


def git(cwd, *args, check=True, inp=None):
    r = subprocess.run(["git", "-C", cwd] + list(args), input=inp, capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "git %s failed" % args[0])
    return r


def default_branch(root):
    r = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    if r.returncode == 0 and r.stdout.strip().startswith("origin/"):
        return r.stdout.strip()[len("origin/") :]
    for b in ("main", "master"):
        if git(root, "show-ref", "--verify", "--quiet", "refs/remotes/origin/" + b, check=False).returncode == 0:
            return b
    return git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def files_to_copy(root, patterns):
    """Untracked files under root that match the patterns and that git ignores: what Conductor's
    Files to copy copies into a new workspace."""
    if not patterns:
        return []
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(patterns + "\n")
        pf = f.name
    try:
        cands = [p for p in git(root, "ls-files", "-z", "--others", "--ignored", "--exclude-from=" + pf).stdout.split("\0") if p]
    finally:
        os.unlink(pf)
    if not cands:
        return []
    r = git(root, "check-ignore", "-z", "--stdin", check=False, inp="\0".join(cands) + "\0")
    ignored = {p for p in r.stdout.split("\0") if p}
    return [p for p in cands if p in ignored]


def slug_for(branch):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", branch).strip("-") or "ws"


def sh_env_file(path, env):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k in sorted(env):
            f.write("export %s=%s\n" % (k, shlex.quote(env[k])))


def tmux(*args):
    return subprocess.run(["tmux"] + list(args), capture_output=True, text=True)


def has_session(name):
    return tmux("has-session", "-t", "=" + name).returncode == 0


def new_session(name, cwd, env):
    args = ["new-session", "-d", "-s", name, "-c", cwd]
    for k in sorted(env):
        args += ["-e", "%s=%s" % (k, env[k])]
    r = tmux(*args)
    if r.returncode != 0:
        raise RuntimeError("tmux: " + (r.stderr.strip() or "new-session failed"))


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, st):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)


def state_dir(project):
    sdir = os.path.join(STATE, project)
    os.makedirs(sdir, exist_ok=True)
    return sdir


FNM_HOOK = 'export PATH="$HOME/.local/share/fnm:$PATH"; command -v fnm >/dev/null 2>&1 && eval "$(fnm env --use-on-cd --shell bash)"'
BUN_HOOK = 'export BUN_INSTALL="$HOME/.bun"; export PATH="$BUN_INSTALL/bin:$PATH"'


def have(cmd):
    return subprocess.run(["bash", "-l", "-c", "command -v %s" % cmd], capture_output=True, stdin=subprocess.DEVNULL).returncode == 0


def add_profile_hook(line):
    """One line in ~/.profile, which login shells (setup scripts, tmux sessions, agents) read."""
    prof = os.path.join(HOME, ".profile")
    try:
        with open(prof, encoding="utf-8", errors="replace") as f:
            if line in f.read():
                return
    except FileNotFoundError:
        pass
    with open(prof, "a", encoding="utf-8") as f:
        f.write("\n# added by prized-dev/integrations conductor/move.sh\n" + line + "\n")


def package_json(root):
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return None


def install(what, script, log):
    say("box: installing %s (log %s)" % (what, tilde(log)))
    with open(log, "a") as lf:
        r = subprocess.run(["bash", "-c", "set -e; " + script], stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, cwd=HOME)
    if r.returncode != 0:
        say("box: installing %s failed (exit %d); setup scripts that need it will fail too" % (what, r.returncode))
    return r.returncode == 0


def ensure_tools(root, setup, sdir):
    """Conductor Cloud's machines came with Node 24, npm and pnpm; a fresh box has no Node. Bring
    the JavaScript and Python tools this repo's lockfiles or setup script name, when the box lacks
    them, the way prized setup brings them (fnm for Node, corepack for pnpm and yarn)."""
    log = os.path.join(sdir, "tools.log")
    pj = package_json(root)
    words = set(re.findall(r"[A-Za-z0-9_.-]+", setup))

    def exists(f):
        return os.path.exists(os.path.join(root, f))

    pm_field = (pj or {}).get("packageManager") if isinstance((pj or {}).get("packageManager"), str) else ""
    if (pj is not None or words & {"npm", "npx", "pnpm", "yarn", "node", "corepack"}) and not have("node"):
        ver = "24"
        for f in (".nvmrc", ".node-version"):
            try:
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    v = fh.read().strip().lstrip("v")
                if re.fullmatch(r"\d+(\.\d+){0,2}", v):
                    ver = v
                    break
            except OSError:
                pass
        fnm = '"$HOME/.local/share/fnm/fnm"'
        if install(
            "Node %s with fnm" % ver,
            "[ -x %s ] || curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell; %s install %s; %s default %s" % (fnm, fnm, shlex.quote(ver), fnm, shlex.quote(ver)),
            log,
        ):
            add_profile_hook(FNM_HOOK)
    for pm, lock in (("pnpm", "pnpm-lock.yaml"), ("yarn", "yarn.lock")):
        if (pm_field.startswith(pm + "@") or exists(lock) or pm in words) and have("node") and not have(pm):
            spec = pm_field.split("+")[0] if pm_field.startswith(pm + "@") else pm
            install(pm + " with corepack", "bash -l -c %s" % shlex.quote("corepack enable %s 2>/dev/null || npm i -g %s" % (pm, shlex.quote(spec))), log)
    if (pm_field.startswith("bun@") or exists("bun.lock") or exists("bun.lockb") or "bun" in words) and not have("bun"):
        tag = " -s bun-v" + shlex.quote(pm_field[4:].split("+")[0]) if pm_field.startswith("bun@") else ""
        if install("Bun", "curl -fsSL https://bun.sh/install | bash" + tag, log):
            add_profile_hook(BUN_HOOK)
    if (exists("uv.lock") or "uv" in words) and not have("uv"):
        install("uv", "curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh", log)
    try:
        with open(os.path.join(root, ".gitattributes"), encoding="utf-8", errors="replace") as f:
            lfs = "filter=lfs" in f.read()
    except OSError:
        lfs = False
    if lfs and not have("git-lfs"):
        say("box: this repo uses Git LFS, which a fresh box lacks: sudo apt-get install -y git-lfs, then git lfs pull in each worktree")


def cmd_plan(a):
    c = read_config(os.path.join(HOME, a.root), "cloud" if a.setup else "local")
    describe(c, a.setup)
    print(c["patterns"])


def cmd_apply(a):
    root = os.path.join(HOME, a.root)
    mode = "cloud" if a.setup else "local"
    c = read_config(root, mode)
    setup = c["setup"]
    if a.setup:
        with open(os.path.join(HOME, a.setup), encoding="utf-8", errors="replace") as f:
            setup = f.read()
    sdir = state_dir(a.project)
    slugs = {}
    for b in a.branches:
        s = slug_for(b)
        if s in slugs and slugs[s] != b:
            die("%s and %s would share the folder %s-%s; pick names that differ in more than punctuation" % (slugs[s], b, a.project, s))
        slugs[s] = b
        if git(root, "check-ref-format", "--branch", b, check=False).returncode != 0:
            die("%s is not a valid branch name" % b)

    if git(root, "fetch", "--prune", "--quiet", "origin", check=False).returncode != 0:
        say("  (couldn't fetch from origin; new branches start from what the box has)")
    base = default_branch(root)
    start = "origin/" + base if git(root, "show-ref", "--verify", "--quiet", "refs/remotes/origin/" + base, check=False).returncode == 0 else base
    copy = files_to_copy(root, c["patterns"])
    ensure_tools(root, setup, sdir)

    used = set()  # every workspace's port on this box, across repos, so two never share one
    for proj in os.listdir(STATE):
        pdir = os.path.join(STATE, proj)
        if os.path.isdir(pdir):
            for name in os.listdir(pdir):
                p = load_state(os.path.join(pdir, name)).get("port") if name.endswith(".json") else None
                if isinstance(p, int):
                    used.add(p)
    run_started = 0
    for b in a.branches:
        s = slug_for(b)
        wdir = os.path.join(os.path.dirname(root), "%s-%s" % (a.project, s))
        stp = os.path.join(sdir, s + ".json")
        st = load_state(stp)
        status, session = "", "-"
        try:
            if os.path.exists(wdir):
                common = git(wdir, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()
                if os.path.realpath(common) != os.path.realpath(os.path.join(root, ".git")):
                    raise RuntimeError("%s is already there and is not a worktree of %s" % (tilde(wdir), tilde(root)))
                say("%s: worktree %s is already there" % (s, tilde(wdir)))
            else:
                if git(root, "show-ref", "--verify", "--quiet", "refs/heads/" + b, check=False).returncode == 0:
                    git(root, "worktree", "add", "--quiet", wdir, b)
                    how = "branch %s" % b
                elif git(root, "show-ref", "--verify", "--quiet", "refs/remotes/origin/" + b, check=False).returncode == 0:
                    git(root, "worktree", "add", "--quiet", "--track", "-b", b, wdir, "origin/" + b)
                    how = "branch %s from origin" % b
                else:
                    git(root, "worktree", "add", "--quiet", "--no-track", "-b", b, wdir, start)
                    how = "new branch %s from %s" % (b, start)
                say("%s: worktree %s on %s" % (s, tilde(wdir), how))
            n = 0
            for rel in copy:
                dst = os.path.join(wdir, rel)
                if os.path.lexists(dst):
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(root, rel), dst, follow_symlinks=False)
                n += 1
            if n:
                say("%s: copied %d gitignored file(s) from %s" % (s, n, tilde(root)))

            port = st.get("port")
            if not isinstance(port, int):
                port = a.port_base
                while any(abs(port - u) < 10 for u in used):  # each workspace has 10 ports
                    port += 10
            used.add(port)
            env = dict(c["env"])
            env.update(
                {
                    "CONDUCTOR_WORKSPACE_NAME": s,
                    "CONDUCTOR_WORKSPACE_PATH": wdir,
                    "CONDUCTOR_ROOT_PATH": root if mode == "local" else wdir,
                    "CONDUCTOR_DEFAULT_BRANCH": base,
                    "CONDUCTOR_PORT": str(port),
                    "CONDUCTOR_IS_LOCAL": "1" if mode == "local" else "0",
                }
            )
            if mode == "cloud":
                env["CONDUCTOR_BASE_DIR"] = os.path.dirname(wdir)
            st.update({"branch": b, "dir": wdir, "port": port})
            save_state(stp, st)
            sh_env_file(os.path.join(sdir, s + ".env"), env)

            if not setup:
                status = "none"
            elif st.get("setup") == "ok":
                status = "ok"
                say("%s: setup ran here before" % s)
            else:
                log = os.path.join(sdir, s + "-setup.log")
                say("%s: running the setup script (log %s)" % (s, tilde(log)))
                with open(log, "w") as lf:
                    r = subprocess.run(["bash", "-l", "-c", setup], cwd=wdir, env={**os.environ, "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0", **env}, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT)
                if r.returncode == 0:
                    status = "ok"
                else:
                    status = "failed(exit %d)" % r.returncode
                    with open(log, errors="replace") as lf:
                        tail = lf.read().splitlines()[-12:]
                    say("%s: setup failed (exit %d); the end of its log:" % (s, r.returncode))
                    for line in tail:
                        say("    " + line)
                st["setup"] = status
                save_state(stp, st)

            if a.agent != "none":
                session = "%s-%s" % (a.agent, s)
                if not has_session(session):
                    new_session(session, wdir, env)
            if a.run and c["actions"]:
                act = next(x for x in c["actions"] if x["default"])
                rs = "run-" + s
                if c["run_mode"] == "nonconcurrent" and run_started:
                    say("%s: run script not started: run_mode is nonconcurrent, and one runs already" % s)
                elif has_session(rs):
                    say("%s: session %s is already there" % (s, rs))
                    run_started += 1
                else:
                    script = os.path.join(sdir, s + "-run.sh")
                    with open(script, "w") as f:
                        f.write(act["command"] + "\n")
                    new_session(rs, os.path.join(wdir, act["cwd"]) if act["cwd"] else wdir, env)
                    tmux("send-keys", "-t", "=" + rs + ":", "-l", "bash " + shlex.quote(script))
                    tmux("send-keys", "-t", "=" + rs + ":", "Enter")
                    say("%s: run script started on port %d: prized ssh -s %s" % (s, port, rs))
                    run_started += 1
        except RuntimeError as e:
            say("%s: %s" % (s, e))
            status = status or "error"
            print("\t".join(["ws", s, b, os.path.relpath(wdir, HOME), "-", status, "-"]), flush=True)
            continue
        print("\t".join(["ws", s, b, os.path.relpath(wdir, HOME), str(port), status, session]), flush=True)


def cmd_archive(a):
    root = os.path.join(HOME, a.root)
    s = slug_for(a.branch)
    sdir = state_dir(a.project)
    wdir = os.path.join(os.path.dirname(root), "%s-%s" % (a.project, s))
    if not os.path.isdir(wdir):
        die("no workspace %s at %s" % (a.branch, tilde(wdir)))
    dirty = git(wdir, "status", "--porcelain", check=False).stdout.splitlines()
    if dirty and not a.force:
        die("%s has %d uncommitted change(s); commit and push them, or add --force to throw them away" % (tilde(wdir), len(dirty)))
    c = read_config(root, "local")
    env = {}
    envf = os.path.join(sdir, s + ".env")
    if os.path.isfile(envf):
        with open(envf) as f:
            for line in f:
                k, _, v = line[len("export ") :].rstrip("\n").partition("=")
                env[k] = (shlex.split(v) or [""])[0]
    if c["archive"]:
        say("%s: running the archive script" % s)
        r = subprocess.run(["bash", "-l", "-c", c["archive"]], cwd=wdir, env={**os.environ, **env}, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            die("the archive script failed (exit %d); the worktree stays" % r.returncode)
    for prefix in ("claude", "codex", "cursor", "opencode", "pi", "run"):
        name = "%s-%s" % (prefix, s)
        if has_session(name):
            tmux("kill-session", "-t", "=" + name)
            say("%s: stopped session %s" % (s, name))
    r = git(root, "worktree", "remove", *(["--force"] if a.force else []), wdir, check=False)
    if r.returncode != 0:
        die(r.stderr.strip() or "git worktree remove failed")
    for ext in (".json", ".env", "-setup.log", "-run.sh"):
        try:
            os.unlink(os.path.join(sdir, s + ext))
        except FileNotFoundError:
            pass
    say("%s: archived; the branch %s stays (git branch -D %s deletes it)" % (s, a.branch, shlex.quote(a.branch)))


def main():
    p = argparse.ArgumentParser(prog="move.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("root")
    pl.add_argument("--setup", action="store_true")
    ap = sub.add_parser("apply")
    ap.add_argument("root")
    ap.add_argument("--project", required=True)
    ap.add_argument("--agent", default="claude", choices=["claude", "codex", "none"])
    ap.add_argument("--port-base", type=int, default=4100)
    ap.add_argument("--setup", default="")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("branches", nargs="+")
    ar = sub.add_parser("archive")
    ar.add_argument("root")
    ar.add_argument("--project", required=True)
    ar.add_argument("branch")
    ar.add_argument("--force", action="store_true")
    a = p.parse_args()
    if not os.path.isdir(os.path.join(HOME, a.root, ".git")):
        die("no git repo at ~/%s on the box" % a.root)
    {"plan": cmd_plan, "apply": cmd_apply, "archive": cmd_archive}[a.cmd](a)


main()
PY
}

main "$@"
