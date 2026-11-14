#!/usr/bin/env bash
# reap.sh: delete the sandboxes of Cursor workers that have stayed paused longer than you want.
#
#   ./reap.sh --older-than 1d [--pool prized] [--dry-run]
#
# Each worker's sandbox already deletes itself once it has been paused for CURSOR_HIBERNATE_TTL (3d
# unless set), so run this to clear them sooner: after lowering the TTL, to free paused storage, or
# from cron. It deletes only sandboxes spawn.sh made (labelled cursor-worker=...) that are paused or in
# error and were last used before the cutoff; a running worker is never touched. Needs `prized`
# (signed in, or PRIZED_API_KEY with Sandboxes: Write) and jq.
set -euo pipefail

AGE='' POOL='' DRY=0
while [ $# -gt 0 ]; do
  case $1 in
    --older-than) AGE=${2:-}; shift 2 || shift ;;
    --pool) POOL=${2:-}; shift 2 || shift ;;
    --dry-run) DRY=1; shift ;;
    -h | --help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "reap: unknown argument $1 (see --help)" >&2; exit 2 ;;
  esac
done
[[ $AGE =~ ^([0-9]+)([mhd]?)$ ]] || { echo "reap: --older-than takes a duration such as 90m, 12h or 3d" >&2; exit 2; }
n=$((10#${BASH_REMATCH[1]}))
case ${BASH_REMATCH[2]} in d) secs=$((n * 86400)) ;; h) secs=$((n * 3600)) ;; *) secs=$((n * 60)) ;; esac
[ -z "$POOL" ] || [[ $POOL =~ ^[A-Za-z0-9._-]+$ ]] || { echo "reap: unexpected pool name" >&2; exit 2; }
command -v jq >/dev/null || { echo "reap: needs jq" >&2; exit 1; }
LOGS=${PRIZED_CURSOR_LOGS:-${XDG_STATE_HOME:-$HOME/.local/state}/prized-cursor}  # spawn.sh's, for its locks
cutoff=$(($(date +%s) - secs))

select=(--json)
[ -z "$POOL" ] || select+=(--label "cursor-pool=$POOL")
list=$(prized sandbox ls "${select[@]}")
# "id worker state" for each candidate, oldest first.
rows=$(jq -r --argjson cutoff "$cutoff" '[.sandboxes[]
    | select((.labels["cursor-worker"] // "") != "" and (.state == "paused" or .state == "error"))
    | . + {used: ((.lastActiveAt // .createdAt) | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601)}
    | select(.used < $cutoff)] | sort_by(.used) | .[] | "\(.id) \(.labels["cursor-worker"]) \(.state)"' <<< "$list")

HELD=''
trap '[ -z "$HELD" ] || rmdir "$LOGS/$HELD.lock" 2>/dev/null || true' EXIT
mkdir -p "$LOGS"
n=0
while read -r sbx worker state; do
  [ -n "$sbx" ] || continue
  [[ $sbx =~ ^sbx_[A-Za-z0-9]+$ && $worker =~ ^[A-Za-z0-9._-]+$ ]] || continue
  if [ "$DRY" = 1 ]; then echo "would delete $sbx (worker $worker, $state)"; n=$((n + 1)); continue; fi
  # A wake spawn.sh is handling on this machine holds the worker's lock: leave that one. A lock
  # older than 15 minutes was left by a process that died.
  lock=$LOGS/$worker.lock
  if [ -n "$(find "$lock" -maxdepth 0 -mmin +15 2>/dev/null)" ] && mv "$lock" "$lock.stale.$$" 2>/dev/null; then
    rmdir "$lock.stale.$$" 2>/dev/null || true
  fi
  mkdir "$lock" 2>/dev/null || { echo "skipped $sbx: worker $worker is being woken"; continue; }
  HELD=$worker
  now=$(prized sandbox get "$sbx" --json 2>/dev/null | jq -r '.sandbox.state // empty') || now=
  if [ "$now" = paused ] || [ "$now" = error ]; then
    if prized sandbox rm "$sbx" > /dev/null; then echo "deleted $sbx (worker $worker, $state)"; n=$((n + 1)); fi
  else
    echo "skipped $sbx: now ${now:-gone}"
  fi
  rmdir "$lock" 2>/dev/null || true
  HELD=''
done <<< "$rows"
if [ "$DRY" = 1 ]; then echo "$n to delete"; else echo "$n deleted"; fi
