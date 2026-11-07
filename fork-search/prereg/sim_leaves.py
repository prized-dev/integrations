"""Synthetic leaves.jsonl to exercise ledger.py and analysis.py (no model runs).

python3 -I sim_leaves.py OUT.jsonl EFFECT [--tasks 25] [--n-a 15] [--reps 2] [--seed 7]
    [--d-first-rep-only] [--no-faults] [--plan-out PLAN.jsonl] [--outcomes-out OUTCOMES.jsonl]

EFFECT = per-leaf logit shift for B's children (C and D get none). Deterministic: every
lineage draws from its own stream seeded by (seed, task, rep, attempt, arm, leaf), so
EFFECT and the D flag change nothing else. The plan and the harness outcomes go
through ledger.build_rows, as in a real run. Token timelines: each call costs the whole
context in raw tokens; B and D children inherit the parent's context (cheap cache
reads), C children and A attempts start fresh. Lineages stop at their budget.

Faults (unless --no-faults), by task index:
  t1 rep 0   the fork(count=8) feeding B and C fails (FORK_API_5XX): B and C score 0
  t3 rep 0   D's workdir restore fails (TAR_RESTORE)
  t5 rep 0   one C child could not be made (COPY_UNREACHABLE); the other 3 ran
  t2 rep 0   A infra failure at attempt 0 (SANDBOX_CREATE_5XX); rerun in every arm, ok
  t20 rep 0  C infra failure at every attempt (RUNNER_LOST): dropped after 2 reruns
  t21 last rep  B planned but never recorded (missing; the harness lost it)
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402

BUDGET = 1.50
PRICE = 0.45e-6  # $/raw token, Vals' implied blend ($0.33 / 727.86k)
CACHED = 0.10e-6  # $/token for an inherited prefix (cache read)
CTX0, CTX_MAX, CTX_SUMMARY = 3_000, 150_000, 20_000
OUT_TOKENS = 400


def expit(x):
    return 1 / (1 + math.exp(-x))


def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def lineage(rng, t0, stop_at, ctx, cached, budget):
    """Calls from t0 until stop_at, or until the next call would pass the budget.
    Returns (timeline, usd, end_s, ran_out, episodes, ctx)."""
    t, cum, usd, tl, n = t0, 0, 0.0, [], 0
    while True:
        t_next = t + rng.uniform(10, 30)
        if t_next > stop_at:
            return tl, usd, stop_at, False, n, ctx
        ctx += rng.randint(1500, 4000)
        if ctx > CTX_MAX:  # Terminus 2 summarizes; the cached prefix is gone
            ctx, cached = CTX_SUMMARY, 0
        raw = ctx + OUT_TOKENS
        cost = (raw - cached) * PRICE + cached * CACHED
        if usd + cost > budget:
            return tl, usd, t_next, True, n, ctx
        t, usd, cum, n = t_next, usd + cost, cum + raw, n + 1
        tl.append([round(t, 1), cum])


def leaf(rng, q, t0, deadline, ctx, cached, budget, ep0=0):
    passed = rng.random() < q
    k = rng.random()
    if passed or k < 0.4:  # a real solve, or a false claim
        stop_at = min(deadline, t0 + rng.uniform(60, max(61.0, min(0.9 * deadline - t0, 700))))
    elif k < 0.95:
        stop_at = deadline
    else:
        stop_at = rng.uniform(t0, deadline)
    tl, usd, end, ran_out, n, _ = lineage(rng, t0, stop_at, ctx, cached, budget)
    if ran_out:
        stop, passed = "budget", False
    elif passed or k < 0.4:
        stop = "confirmed"
    elif k < 0.95:
        stop = "deadline"
    else:
        stop = "session_died"
    rec = {"passed": int(passed), "confirmed": stop == "confirmed", "end_s": round(end, 1),
           "episodes": ep0 + n, "stop_reason": stop, "tokens_timeline": tl}
    return rec, usd


def task_rep(seed, ti, rep, attempt, arms, p, deadline, effect, faults):
    """Harness outcomes {arm: outcome or None} for one attempt of one task-rep."""
    task = f"t{ti}"
    R = lambda *k: random.Random("/".join(map(str, (seed, task, rep, attempt) + k)))  # noqa: E731
    base = {"task": task, "rep": rep, "attempt": attempt, "deadline_s": deadline}
    out = {}
    # A: 4 fresh attempts, B/4 each
    recs, usd = [], 0.0
    for lf in range(4):
        rec, u = leaf(R("A", lf), expit(logit(p)), 0.0, deadline, CTX0, 0, BUDGET / 4)
        rec.update(leaf=lf, trigger=R("A", lf, "trig").choice(["claim", "hard", "cutoff"]))
        recs.append(rec)
        usd += u
    out["A"] = dict(base, arm="A", status="ok", usd_arm=round(usd, 4), usd_actual=round(usd, 4),
                    trigger="none", leaves=recs, parent_tokens_timeline=[])
    # the shared parent of B, C and D: runs until its first trigger
    pr = R("parent")
    trig = pr.choice(["claim", "hard", "hard", "cutoff"])
    f_at = 0.5 * deadline if trig == "cutoff" else pr.uniform(0.08, 0.45) * deadline
    ptl, s, f, _, n_p, ctx_f = lineage(pr, 0.0, f_at, CTX0, 0, BUDGET / 4)
    fork_arms = [a for a in "BCD" if a in arms]
    for arm in fork_arms:
        o = dict(base, arm=arm, trigger=trig, parent_tokens_timeline=ptl)
        fault = faults.get((ti, rep, arm))
        if fault and fault[0] == "fork_failed":
            out[arm] = dict(o, status="fork_failed", fail_code=fault[1], end_s=round(f + 30, 1),
                            episodes=n_p, usd_arm=round(s, 4), usd_actual=round(s / len(fork_arms), 4))
            continue
        q = expit(logit(p) + (effect if arm == "B" else 0.0))
        ctx, cached = (CTX0, 0) if arm == "C" else (ctx_f, ctx_f)
        recs, usd = [], 0.0
        for lf in range(4):
            if fault and fault[0] == "child_failed" and fault[2] == lf:
                recs.append({"leaf": lf, "fork_failed": True, "fail_code": fault[1], "end_s": round(f + 30, 1)})
                continue
            rec, u = leaf(R(arm, lf), q, f, deadline, ctx, cached, (BUDGET - s) / 4, ep0=n_p)
            rec["leaf"] = lf
            recs.append(rec)
            usd += u
        out[arm] = dict(o, status="ok", usd_arm=round(s + usd, 4), usd_actual=round(s / len(fork_arms) + usd, 4),
                        leaves=recs)
    for arm in arms:
        fault = faults.get((ti, rep, arm))
        if fault and fault[0] == "infra" and attempt in fault[2]:
            out[arm] = dict(base, arm=arm, status="infra_failed", fail_code=fault[1],
                            parent_tokens_timeline=out[arm]["parent_tokens_timeline"])
        if fault and fault[0] == "missing":
            out[arm] = None
    return out


def simulate(effect, tasks=25, n_a=15, reps=2, seed=7, d_first_rep_only=False, faults=True):
    """(plan, outcomes) for a synthetic run."""
    last = reps - 1
    F = {
        (1, 0, "B"): ("fork_failed", "FORK_API_5XX"),
        (1, 0, "C"): ("fork_failed", "FORK_API_5XX"),
        (3, 0, "D"): ("fork_failed", "TAR_RESTORE"),
        (5, 0, "C"): ("child_failed", "COPY_UNREACHABLE", 2),
        (2, 0, "A"): ("infra", "SANDBOX_CREATE_5XX", {0}),
        (20, 0, "C"): ("infra", "RUNNER_LOST", {0, 1, 2}),
        (21, last, "B"): ("missing",),
    } if faults else {}
    F = {k: v for k, v in F.items() if k[0] < tasks}
    plan, outcomes = [], []
    for ti in range(tasks):
        tr = random.Random(f"{seed}/task/{ti}")
        p = tr.betavariate(0.35, 0.53)
        deadline = float(tr.choice([900, 900, 1800, 3600]))
        stratum = "a" if ti < n_a else "b"
        for rep in range(reps):
            arms = "ABCD" if (rep == 0 or not d_first_rep_only) else "ABC"
            for attempt in range(ledger.MAX_RERUNS + 1):
                pl = [{"task": f"t{ti}", "rep": rep, "arm": a, "stratum": stratum, "attempt": attempt,
                       "deadline_s": deadline} for a in arms]
                res = task_rep(seed, ti, rep, attempt, arms, p, deadline, effect, F)
                plan += pl
                outs = [res[a] for a in arms if res[a] is not None]
                outcomes += outs
                rows = ledger.build_rows(pl, outs)
                lost = any(res[a] is None for a in arms)  # the harness lost track: no rerun
                if lost or all(r["valid"] for r in rows):
                    break
    return plan, outcomes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("effect", type=float)
    ap.add_argument("--tasks", type=int, default=25)
    ap.add_argument("--n-a", type=int, default=15)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--d-first-rep-only", action="store_true", help="D runs in rep 0 only (the pilot)")
    ap.add_argument("--no-faults", action="store_true")
    ap.add_argument("--plan-out")
    ap.add_argument("--outcomes-out")
    a = ap.parse_args(argv)
    plan, outcomes = simulate(a.effect, a.tasks, a.n_a, a.reps, a.seed, a.d_first_rep_only, not a.no_faults)
    rows = ledger.build_rows(plan, outcomes)
    for path, data in ((a.plan_out, plan), (a.outcomes_out, outcomes), (a.out, rows)):
        if path:
            with open(path, "w") as fh:
                fh.writelines(json.dumps(x) + "\n" for x in data)


if __name__ == "__main__":
    main()
