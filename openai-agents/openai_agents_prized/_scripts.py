"""The bash scripts the session runs in the sandbox.

Each one takes its inputs as positional arguments (never interpolated into
the script) and runs as `bash -c SCRIPT bash ARG...`. The file scripts judge
paths with the SDK's own resolver (`RESOLVE_WORKSPACE_PATH_HELPER`, its
arguments `ROOT PATH MODE [GRANT READ_ONLY]...`), on what the shell actually
holds open, so a link swapped in after the SDK's own check cannot redirect a
read or a write.
"""

from __future__ import annotations

EXIT_NOT_FOUND = 44
"""The read script's code for a path that does not exist."""

EXIT_NOT_FILE = 45
"""The read script's code for a path that is not a regular file."""

EXIT_MOVED = 46
"""The read script's code for a file that was not where it was judged, while it was judged."""

EXIT_ROOT_MOVED = 47
"""The snapshot scripts' code for a workspace root that is a link, or under one."""

EXIT_STAGE_CHANGED = 48
"""The write script's code for staged parts whose bytes are not the ones uploaded."""

EXIT_ESCAPE = 111
"""The resolver's code for a path outside the workspace and every grant."""

EXIT_READ_ONLY_GRANT = 114
"""The resolver's code for a write under a read-only grant."""

READ_STAGED = r"""
set -euo pipefail
helper=$1 target=$2 stage=$3 part=$4 limit=$5 root=$6
shift 6
[ -e "$target" ] || exit 44
[ -f "$target" ] || exit 45
exec 3< "$target"
# Where the opened file is now, as the kernel names it (no links in it).
# The `x` keeps a name's trailing newlines from being cut.
held() { at=$(readlink -- "/proc/$$/fd/3"; printf x); at=${at%?x}; }
# Judge what was opened, where it really is: a link swapped in after the
# SDK's own check is caught here, since the descriptor cannot be redirected.
held
real=$at
# The resolver must find that very path, through no link: a link swapped
# into it for the check would approve another place.
judged=$("$helper" "$root" "$real" 0 "$@" && printf x) || exit $?
judged=${judged%?x}
[ "$judged" = "$real" ] || { echo "moved while checked: $target" >&2; exit 46; }
# And the file must still be there once judged: one moved into an allowed
# place for the check and back out after it is refused.
held
[ "$at" = "$real" ] || { echo "moved while checked: $target" >&2; exit 46; }
[ -f /dev/fd/3 ] || exit 45
mkdir -p -- "$stage"
if [ -n "$limit" ]; then head -c "$limit" <&3; else cat <&3; fi \
  | split -b "$part" -d -a 4 - "$stage/part-"
cd -- "$stage"
ls -1
""".strip()

WRITE_STAGED = r"""
set -euo pipefail
helper=$1 stage=$2 target=$3 root=$4 want=$5
shift 5
grants=("$@")
made=
cleanup() {
  rm -rf -- "$stage"
  if [ -n "$made" ]; then rm -f -- "$made"; fi
}
trap cleanup EXIT
# The resolver must approve that very path: it cuts a directory name's
# trailing newlines (`$(pwd -P)`), so a path it answers for under another
# name is outside what it judged.
allowed() {
  local judged
  judged=$("$helper" "$root" "$1" 1 "${grants[@]}" && printf x) || return $?
  judged=${judged%?x}
  [ "$judged" = "$1" ] || { printf 'outside the workspace: %s\n' "$1" >&2; return 111; }
}

# Walk to the target's directory one component at a time, making a missing
# one only where a write is allowed. The shell then holds that directory:
# swapping a component for a link afterwards changes nothing here.
dir=${target%/*}
leaf=${target##*/}
cd -P /
rest=${dir#/}
while [ -n "$rest" ]; do
  part=${rest%%/*}
  case $rest in */*) rest=${rest#*/} ;; *) rest= ;; esac
  [ -n "$part" ] || continue
  if [ ! -e "$part" ] && [ ! -L "$part" ]; then
    here=$(pwd -P; printf x)
    here=${here%?x}
    allowed "${here%/}/$part"
    # Another write may make it first (the SDK writes a manifest's files at
    # once): one that exists now is walked into like any other.
    mkdir -- "$part" 2>/dev/null || [ -d "$part" ] || mkdir -- "$part"
  fi
  cd -P -- "$part"
done

# A link at the target is written through, one hop at a time, each hop held.
hops=0
while [ -L "$leaf" ]; do
  hops=$((hops + 1))
  if [ "$hops" -gt 40 ]; then echo "too many levels of symbolic links" >&2; exit 40; fi
  link=$(readlink -- "$leaf"; printf x)
  link=${link%?x}
  case $link in
    */*) cd -P -- "${link%/*}/"; leaf=${link##*/} ;;
    *) leaf=$link ;;
  esac
done
case $leaf in ""|.|..) echo "not a file: $target" >&2; exit 21 ;; esac
# The `x` keeps a directory name's trailing newlines from being cut, so the
# path judged is the one the shell holds.
here=$(pwd -P; printf x)
here=${here%?x}
allowed "${here%/}/$leaf"

# A new file beside the target, created with O_EXCL (which never follows a
# link) under a name no other write uses, then renamed over the target: the
# bytes land in this directory, whatever the target's name points at by then.
# (A process in the sandbox can still race the rename: FAILURES.md.)
tmp=".openai-agents-prized-$$-${SRANDOM:-$RANDOM}${SRANDOM:-$RANDOM}"
set -C
exec 3> "$tmp"
set +C
made=$tmp
[ /dev/fd/3 -ef "$tmp" ] || exit 1
cat -- "$stage"/part-* >&3
# The bytes written must be the ones uploaded: a part swapped for a link (or
# rewritten) after its upload would publish another file's bytes here.
got=$(sha256sum < /dev/fd/3) || exit 1
if [ "${got%% *}" != "$want" ]; then
  echo "staged upload changed before the write: $target" >&2
  exit 48
fi
# An existing file keeps its mode, as a write on a local disk does.
if [ -f "$leaf" ] && [ ! -L "$leaf" ]; then chmod --reference="$leaf" -- /dev/fd/3; fi
exec 3>&-
mv -fT -- "$tmp" "$leaf"
made=
""".strip()

# Both snapshot scripts work in the workspace root held as the shell's
# directory, and only when its path names it through no link: a workload
# that swapped the root (or a directory above it) for a link to somewhere
# else is refused, and one that swaps it after the check moves nothing, since
# tar works on the directory already held. The `x` keeps the held path's
# trailing newlines, so a link to a sibling named the root plus a newline is
# refused too.
PIN_ROOT = r"""cd -P -- "$root"
pinned=$(pwd -P; printf x)
pinned=${pinned%?x}
if [ "$pinned" != "$root" ]; then
  printf 'workspace root %s is a link to %s\n' "$root" "$pinned" >&2
  exit 47
fi
"""

# Hard links are stored as the files they name (`--hard-dereference`): the
# SDK's tar policy, which the restore checks, has no hard-link members.
PERSIST = (
    r"""
set -euo pipefail
root=$1 stage=$2 part=$3
shift 3
mkdir -p -- "$stage"
"""
    + PIN_ROOT
    + r"""
tar --no-wildcards --hard-dereference "$@" -cf - . \
  | split -b "$part" -d -a 4 - "$stage/part-"
cd -- "$stage"
ls -1
""".strip()
)

# GNU tar (1.35 in Prized's templates) extracts beneath -C with
# openat2(RESOLVE_BENEATH), so a link already in the workspace cannot carry a
# member out of it, and it creates the archive's own links last. The archive
# was checked before upload: no member under one of its links, no `..`, no
# absolute names, no hard links.
HYDRATE = (
    r"""
set -euo pipefail
root=$1 stage=$2
mkdir -p -- "$root"
"""
    + PIN_ROOT
    + r"""
cat -- "$stage"/part-* | tar -C . -xf -
""".strip()
)

# Starts a command in its own session, detached from the exec that starts it,
# so it outlives that exec (the edge ends an exec's process group, not this).
# With a TTY it runs under `script`, whose stdin is a FIFO that `write_stdin`
# writes to; without one, stdin is /dev/null. The process directory is the
# start's claim: a kill that came first (after a lost answer) made it, and
# then nothing starts.
PTY_START = r"""
set -eu
dir=$1
mkdir -p -- "${dir%/*}"
mkdir -- "$dir"
mkfifo -- "$dir/in"
: > "$dir/out"
# Job control: a background job of a shell without it starts with SIGINT
# ignored, and then Ctrl-C through the TTY could never stop it.
set -m
setsid bash -c '
  dir=$1; cmd=$2; tty=$3
  echo $$ > "$dir/pid"
  # A kill that found no pid marked the directory, or removed it: run nothing,
  # and take the pid back so no later kill aims at a reused one.
  if [ -e "$dir/cancelled" ] || [ ! -p "$dir/in" ]; then rm -f -- "$dir/pid"; exit 0; fi
  exec 3<>"$dir/in"
  if [ "$tty" = 1 ]; then
    SHELL=/bin/bash script -qfec "$cmd" /dev/null <&3 >"$dir/out" 2>&1
  else
    bash -c "$cmd" </dev/null >"$dir/out" 2>&1
  fi
  echo $? > "$dir/exit.tmp"
  mv -f -- "$dir/exit.tmp" "$dir/exit"
' bash "$dir" "$2" "$3" </dev/null >/dev/null 2>&1 &
""".strip()

# Waits up to $3 ms for the process to exit, then prints `<done> <exit|-> <offset>`
# and, base64-encoded, the output from byte $2 on (at most $4 bytes). Output
# before $2 was delivered by an earlier answer, so its disk is given back
# (the writer's offset is unchanged; the start of the file reads as zeros).
PTY_POLL = r"""
dir=$1; off=$2; wait_ms=$3; max=$4
if [ "$off" -gt 0 ]; then fallocate -p -o 0 -l "$off" -- "$dir/out" 2>/dev/null || true; fi
now() { date +%s%3N; }
end=$(( $(now) + wait_ms ))
while [ ! -e "$dir/exit" ] && [ "$(now)" -lt "$end" ]; do sleep 0.05; done
done=0; code=-
if [ -e "$dir/exit" ]; then done=1; code=$(cat -- "$dir/exit"); fi
size=$(stat -c %s -- "$dir/out" 2>/dev/null || echo "$off")
n=$(( size - off ))
if [ "$n" -gt "$max" ]; then n=$max; done=0; fi
if [ "$n" -lt 0 ]; then n=0; fi
printf '%s %s %s\n' "$done" "$code" "$(( off + n ))"
tail -c +"$(( off + 1 ))" -- "$dir/out" | head -c "$n" | base64 -w0
""".strip()

# Kills each process's whole tree (a TTY's shell runs in a session of its own
# under `script`), then its process group, and removes its files. It marks
# the directory first, and the process checks for the mark after writing its
# pid, so one of the two always sees the other: a process whose pid is not
# written yet runs nothing, and its marked directory stays as the claim that
# keeps a late start from running.
PTY_KILL = r"""
tree() {
  local child
  for child in $(cat /proc/"$1"/task/*/children 2>/dev/null); do tree "$child"; done
  echo "$1"
}
for dir in "$@"; do
  mkdir -p -- "$dir" 2>/dev/null || true
  : > "$dir/cancelled" 2>/dev/null || true
  if [ -s "$dir/pid" ]; then
    leader=$(cat -- "$dir/pid")
    # Its tree only while it runs: once its exit file is there, its pid may
    # be another process's. Its group cannot be, while a member is left.
    if [ ! -e "$dir/exit" ]; then kill -KILL $(tree "$leader") 2>/dev/null; fi
    kill -KILL -- "-$leader" 2>/dev/null
    rm -rf -- "$dir"
  fi
done
true
""".strip()

# The input travels in the environment, not in argv: /proc/<pid>/environ is
# readable only by its owner, while every user can read a command line.
# `timeout`: a FIFO whose reader has exited would block the write.
PTY_STDIN_ENV = "OPENAI_AGENTS_PRIZED_INPUT"
PTY_STDIN = 'timeout 5 sh -c \'printf "%s" "$' + PTY_STDIN_ENV + '" > "$1/in"\' sh "$1"'
