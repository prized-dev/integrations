"""Ledger for fork-search (frozen with PREREG.md). Stdlib only.

python3 -I ledger.py PLAN.jsonl OUTCOMES.jsonl > leaves.jsonl   (summary JSON on stderr)

Turns the PLANNED units plus whatever the harness recorded into leaves.jsonl, in
analysis.py's schema. Every planned unit yields exactly 4 rows (leaf 0-3), so no
unit can disappear silently.

PLAN row, one per planned unit (reruns are new planned units with attempt k+1, in
every arm planned for that task-rep):
  task, rep, arm (A|B|C|D), stratum (a|b), attempt (default 0), deadline_s (optional)

OUTCOME row, one per unit the harness finished or gave up on:
  task, rep, arm, attempt (default 0)
  status      ok | fork_failed | infra_failed
  fail_code   harness error code; required unless status is ok. The harness picks
              it from the error, in code, before any verifier output is read.
  deadline_s, usd_arm, usd_actual, trigger (claim|hard|cutoff|none), episodes
  end_s       for fork_failed: when the fork was given up (optional)
  parent_tokens_timeline  [[s_from_arm_start, cumulative_raw_tokens], ...] of the
              shared parent; fork arms only (B, C and D share one parent)
  leaves      for ok: 4 records {leaf, passed, confirmed, end_s, episodes,
              stop_reason, tokens_timeline, raw_tokens?, trigger?, fork_failed?,
              fail_code?}. Leaf numbers are fixed at launch, never by finish order
              (pass@1 is A's leaf 0).
              A leaf record with fork_failed=true is a child the fork could not make.
              passed=None means the verifier crashed twice: scored 0, listed.

Raw tokens = all input (cache reads and cache writes included) plus output. A leaf's
tokens_timeline is its own tokens only, from its first call; the parent's are in
parent_tokens_timeline, which is copied onto every row of a fork-arm unit. Every time
(end_s, timelines, deadline_s) is on the arm's clock: seconds from the arm's start,
fork included, 429/5xx backoff excluded.

Rules (PREREG section 4):
  - fork failure (status fork_failed, after its one retry): 4 rows, passed=0,
    valid=True, fork_failed=True, stop_reason=fork_failed. It counts against the arm.
  - non-fork infra failure: 4 rows, valid=False. The task-rep is rerun in every arm,
    up to MAX_RERUNS times, then dropped from all arms and listed.
  - a parent that dies before its fork (fail_code parent_died, fork arms only): an
    infra failure that is rerun once, then the task-rep is dropped and listed
    (amendment 2026-11-07).
  - planned unit with no outcome: 4 rows, valid=False, missing=True, fail_code=missing.
  - an ok outcome whose leaf records are not exactly leaves 0-3: 4 rows, valid=False,
    fail_code=incomplete_leaves (treated like an infra failure).
Outcomes for unplanned or duplicate units are errors, as are inconsistent records.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

ARMS = ("A", "B", "C", "D")
FORK_ARMS = ("B", "C", "D")
LEAVES = (0, 1, 2, 3)
STRATA = ("a", "b")
STATUSES = ("ok", "fork_failed", "infra_failed")
STOP_REASONS = ("confirmed", "budget", "deadline", "session_died", "fork_failed", "none")
TRIGGERS = ("claim", "hard", "cutoff", "none")
MAX_RERUNS = 2
PARENT_DIED = "parent_died"
# fewer reruns for some codes (amendment 2026-11-07): a parent that dies before its fork
MAX_RERUNS_BY_CODE = {PARENT_DIED: 1}
MISSING = "missing"
INCOMPLETE = "incomplete_leaves"
VERIFIER_CRASH = "verifier_crash"


class LedgerError(ValueError):
    pass


def _key(r):
    return (r["task"], int(r["rep"]), r["arm"], int(r.get("attempt", 0) or 0))


def _timeline(tl, what):
    """Validate a cumulative timeline: [[t, cum], ...], t and cum non-decreasing, >= 0."""
    if tl is None:
        return []
    out, last_t, last_c = [], 0.0, 0
    for p in tl:
        if len(p) != 2:
            raise LedgerError(f"{what}: timeline point {p!r} is not [t, cum]")
        t, c = float(p[0]), int(p[1])
        if t < last_t or c < last_c or t < 0 or c < 0:
            raise LedgerError(f"{what}: timeline must be non-decreasing and >= 0, got {p!r} after {[last_t, last_c]}")
        out.append([t, c])
        last_t, last_c = t, c
    return out


def _base(plan_row, outcome, leaf):
    o = outcome or {}
    return {
        "task": plan_row["task"], "rep": int(plan_row["rep"]), "arm": plan_row["arm"], "leaf": leaf,
        "stratum": plan_row["stratum"], "attempt": int(plan_row.get("attempt", 0) or 0),
        "deadline_s": o.get("deadline_s", plan_row.get("deadline_s")),
        "usd_arm": o.get("usd_arm"), "usd_actual": o.get("usd_actual"),
        "trigger": o.get("trigger", "none"),
        "parent_tokens_timeline": _timeline(o.get("parent_tokens_timeline"), f"{_key(plan_row)} parent"),
        "missing": False,
    }


def _dead_rows(plan_row, outcome, *, valid, fork_failed, fail_code, stop_reason, missing=False):
    """4 rows for a unit with no usable leaf records (fork failed, infra failed, missing)."""
    rows = []
    for leaf in LEAVES:
        r = _base(plan_row, outcome, leaf)
        r.update(passed=0, valid=valid, confirmed=False, end_s=(outcome or {}).get("end_s"),
                 episodes=int((outcome or {}).get("episodes", 0) or 0), raw_tokens=0, tokens_timeline=[],
                 fork_failed=fork_failed, fail_code=fail_code, stop_reason=stop_reason, missing=missing)
        rows.append(r)
    return rows


def _fork_failed_rows(plan_row, outcome):
    return _dead_rows(plan_row, outcome, valid=True, fork_failed=True,
                      fail_code=outcome["fail_code"], stop_reason="fork_failed")


def _leaf_row(plan_row, outcome, rec):
    k = _key(plan_row)
    r = _base(plan_row, outcome, int(rec["leaf"]))
    if "trigger" in rec:  # each A lineage fires its own trigger (the nudge)
        if rec["trigger"] not in TRIGGERS:
            raise LedgerError(f"{k} leaf {rec['leaf']}: bad trigger {rec['trigger']!r}")
        r["trigger"] = rec["trigger"]
    tl = _timeline(rec.get("tokens_timeline"), f"{k} leaf {rec['leaf']}")
    if rec.get("fork_failed"):
        if plan_row["arm"] not in FORK_ARMS:
            raise LedgerError(f"{k}: arm A cannot have a fork failure")
        if not rec.get("fail_code"):
            raise LedgerError(f"{k} leaf {rec['leaf']}: fork failure without a fail_code")
        if rec.get("passed"):
            raise LedgerError(f"{k} leaf {rec['leaf']}: a failed fork cannot carry a verifier pass")
        r.update(passed=0, valid=True, confirmed=False, end_s=rec.get("end_s"),
                 episodes=int(rec.get("episodes", 0) or 0), raw_tokens=tl[-1][1] if tl else 0,
                 tokens_timeline=tl, fork_failed=True, fail_code=rec["fail_code"], stop_reason="fork_failed")
        return r
    stop = rec.get("stop_reason", "none")
    if stop not in STOP_REASONS or stop == "fork_failed":
        raise LedgerError(f"{k} leaf {rec['leaf']}: bad stop_reason {stop!r}")
    confirmed = bool(rec.get("confirmed", False))
    if confirmed != (stop == "confirmed"):
        raise LedgerError(f"{k} leaf {rec['leaf']}: confirmed={confirmed} but stop_reason={stop!r}")
    end_s = rec.get("end_s")
    if not isinstance(end_s, (int, float)) or isinstance(end_s, bool) or end_s < 0:
        raise LedgerError(f"{k} leaf {rec['leaf']}: end_s must be seconds >= 0, got {end_s!r}")
    raw = rec.get("raw_tokens")
    if tl and raw is not None and int(raw) != tl[-1][1]:
        raise LedgerError(f"{k} leaf {rec['leaf']}: raw_tokens {raw} != timeline end {tl[-1][1]}")
    passed, fail_code = rec.get("passed"), rec.get("fail_code")
    if passed is None:  # verifier crashed on its rerun too: scored 0 and listed
        passed, fail_code = 0, fail_code or VERIFIER_CRASH
    if int(passed) not in (0, 1):
        raise LedgerError(f"{k} leaf {rec['leaf']}: passed must be 0, 1 or None")
    r.update(passed=int(passed), valid=True, confirmed=confirmed, end_s=rec.get("end_s"),
             episodes=int(rec.get("episodes", 0) or 0), raw_tokens=tl[-1][1] if tl else int(raw or 0),
             tokens_timeline=tl, fork_failed=False, fail_code=fail_code, stop_reason=stop)
    return r


def unit_rows(plan_row, outcome):
    """Exactly 4 rows (leaf 0-3) for one planned unit."""
    k = _key(plan_row)
    if outcome is None:
        return _dead_rows(plan_row, None, valid=False, fork_failed=False, fail_code=MISSING,
                          stop_reason="none", missing=True)
    status = outcome.get("status")
    if status not in STATUSES:
        raise LedgerError(f"{k}: bad status {status!r}")
    if status != "ok" and not outcome.get("fail_code"):
        raise LedgerError(f"{k}: status {status} needs a fail_code")
    if outcome.get("fail_code") == PARENT_DIED and (plan_row["arm"] not in FORK_ARMS or status != "infra_failed"):
        raise LedgerError(f"{k}: parent_died is an infra failure of a fork arm")
    if outcome.get("trigger", "none") not in TRIGGERS:
        raise LedgerError(f"{k}: bad trigger {outcome.get('trigger')!r}")
    if plan_row["arm"] == "A" and outcome.get("parent_tokens_timeline"):
        raise LedgerError(f"{k}: arm A has no parent")
    if plan_row["arm"] in FORK_ARMS and status != "infra_failed" and not outcome.get("parent_tokens_timeline"):
        raise LedgerError(f"{k}: a fork arm needs its parent's token timeline (the rescoring counts it)")
    pd, od = plan_row.get("deadline_s"), outcome.get("deadline_s")
    if pd is not None and od is not None and float(pd) != float(od):
        raise LedgerError(f"{k}: deadline_s {od} differs from the plan's {pd}")
    if status == "fork_failed":
        if plan_row["arm"] not in FORK_ARMS:
            raise LedgerError(f"{k}: arm A cannot have a fork failure")
        if any(rec.get("passed") for rec in outcome.get("leaves") or []):
            raise LedgerError(f"{k}: a failed fork cannot carry a verifier pass")
        return _fork_failed_rows(plan_row, outcome)
    if status == "infra_failed":
        return _dead_rows(plan_row, outcome, valid=False, fork_failed=False,
                          fail_code=outcome["fail_code"], stop_reason="none")
    recs = outcome.get("leaves") or []
    if sorted(int(rec["leaf"]) for rec in recs) != list(LEAVES):
        return _dead_rows(plan_row, outcome, valid=False, fork_failed=False, fail_code=INCOMPLETE,
                          stop_reason="none")
    return [_leaf_row(plan_row, outcome, rec) for rec in sorted(recs, key=lambda x: int(x["leaf"]))]


def _check_plan(plan):
    seen = set()
    for p in plan:
        k = _key(p)
        if p["arm"] not in ARMS or p["stratum"] not in STRATA:
            raise LedgerError(f"{k}: bad arm or stratum")
        if not 0 <= k[3] <= MAX_RERUNS:
            raise LedgerError(f"{k}: attempt must be 0..{MAX_RERUNS}")
        if k in seen:
            raise LedgerError(f"{k}: planned twice")
        seen.add(k)
    return seen


def build_rows(plan, outcomes):
    """All leaf rows for `plan` (list of plan dicts) given `outcomes` (list of outcome dicts)."""
    planned = _check_plan(plan)
    by_key = {}
    for o in outcomes:
        k = _key(o)
        if k not in planned:
            raise LedgerError(f"{k}: outcome for a unit that was never planned")
        if k in by_key:
            raise LedgerError(f"{k}: two outcomes for one unit")
        by_key[k] = o
    rows = []
    for p in plan:
        rows.extend(unit_rows(p, by_key.get(_key(p))))
    _check_parents(rows)
    return rows


def _check_parents(rows):
    """B, C and D share one parent: their non-empty parent timelines must be identical."""
    par = defaultdict(set)
    for r in rows:
        if r["arm"] in FORK_ARMS and r["parent_tokens_timeline"]:
            par[(r["task"], r["rep"], r["attempt"])].add(json.dumps(r["parent_tokens_timeline"]))
    bad = [k for k, v in par.items() if len(v) > 1]
    if bad:
        raise LedgerError(f"parent timelines differ across fork arms of {bad}")


def rerun_status(plan, rows):
    """Per task-rep at its highest planned attempt: ok, rerun_owed (an invalid unit and
    attempts left) or dropped (still invalid after MAX_RERUNS reruns)."""
    top = defaultdict(int)
    for p in plan:
        k = _key(p)
        top[k[:2]] = max(top[k[:2]], k[3])
    bad = defaultdict(list)
    limit = {}
    for r in rows:
        k = (r["task"], r["rep"])
        if r["attempt"] == top[k] and not r["valid"] and r["leaf"] == 0:
            bad[k].append(f"{r['arm']}:{r['fail_code']}")
            limit[k] = min(limit.get(k, MAX_RERUNS), MAX_RERUNS_BY_CODE.get(r["fail_code"], MAX_RERUNS))
    out = {"rerun_owed": [], "dropped": []}
    for k in sorted(top):
        if bad.get(k):
            entry = {"task": k[0], "rep": k[1], "attempt": top[k], "invalid": sorted(bad[k])}
            out["dropped" if top[k] >= limit[k] else "rerun_owed"].append(entry)
    return out


def summarize(plan, rows):
    units = {(r["task"], r["rep"], r["arm"], r["attempt"]): r for r in rows if r["leaf"] == 0}
    ff = defaultdict(list)
    for r in rows:
        if r["fork_failed"]:
            ff[r["fail_code"]].append(f"{r['task']}/{r['rep']}/{r['arm']}@{r['attempt']}#{r['leaf']}")
    return {
        "planned_units": len(plan),
        "rows": len(rows),
        "missing_units": sorted(f"{t}/{rp}/{a}@{at}" for (t, rp, a, at), r in units.items() if r["missing"]),
        "invalid_units": sorted(f"{t}/{rp}/{a}@{at}:{r['fail_code']}" for (t, rp, a, at), r in units.items()
                                if not r["valid"] and not r["missing"]),
        "fork_failed_leaves_by_code": {c: sorted(v) for c, v in sorted(ff.items())},
        "verifier_crash_leaves": sorted(f"{r['task']}/{r['rep']}/{r['arm']}@{r['attempt']}#{r['leaf']}"
                                        for r in rows if r["fail_code"] == VERIFIER_CRASH),
        **rerun_status(plan, rows),
    }


def read_jsonl(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main(argv):
    plan, outcomes = read_jsonl(argv[1]), read_jsonl(argv[2])
    rows = build_rows(plan, outcomes)
    for r in rows:
        sys.stdout.write(json.dumps(r) + "\n")
    sys.stderr.write(json.dumps(summarize(plan, rows), indent=1) + "\n")


if __name__ == "__main__":
    main(sys.argv)
