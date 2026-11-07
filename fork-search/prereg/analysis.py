"""Pre-registered analysis for fork-search (frozen with PREREG.md). Stdlib only.

python3 -I analysis.py LEAVES.jsonl [--pilot] > results.json

LEAVES.jsonl is written by ledger.py: exactly 4 rows (leaf 0-3) per planned unit.
One row per leaf (JSON): task, rep, arm (A|B|C|D), leaf (0-3), stratum (a|b),
attempt (rerun number, default 0), passed (0/1, hidden verifier), valid (bool;
false = infra-invalid or missing unit), confirmed (bool, the leaf confirmed
task_complete), end_s (seconds from arm start), episodes, usd_arm (cost attributed
to the arm, prefix included, same on every leaf row of a unit), usd_actual (cost
actually paid for the unit), raw_tokens, trigger (claim|hard|cutoff|none), and
from ledger.py: fork_failed (bool), fail_code (str|None), missing (bool),
stop_reason (confirmed|budget|deadline|session_died|fork_failed|none), deadline_s,
tokens_timeline ([[s_from_arm_start, cumulative_raw_tokens], ...], the leaf's own),
parent_tokens_timeline (the shared parent's, same on every row of a fork-arm unit).
Rows without the newer fields still load (fork_failed=False, no timelines).

Unit = (task, rep, arm). pass@arm = any leaf passed. selected@1 = the leaf picked
without hidden tests: earliest confirmed leaf by end_s, else most episodes, then
lowest leaf index. pass@1 = arm A's leaf 0.

Reruns: a task-rep is rerun in every arm, so its highest `attempt` wins for every
arm of that task-rep; an arm with no rows at that attempt is absent (and listed).
Units invalid in any arm of a (task, rep) are dropped from all arms (complete-case
pairing); fork failures and parent deaths (fail_code parent_died) are NOT invalid,
they score 0 (the primary analysis), and a sensitivity analysis drops every task-rep
with either in any fork arm.

Tested family: H1 (B vs A, set a) gates H2 (B vs A, all), H3 (B vs C, set a) and
H4 (B vs D, set a), with Holm over {H2, H3, H4}. H4 uses the reps where all of A-D
are valid (D may run in every rep or only rep 0). Everything else is exploratory.

Verdict (decision 2026-11-07): WIN rests on accuracy alone (H1 rejects and the
estimate is at least +5 points). Cost is secondary: B's mean usd_arm on set a is
reported against A's with a +-10% band, and only B > 1.10 x A turns WIN into
WIN_MORE_EXPENSIVE. With --pilot there is no verdict and no Holm family (the pilot is
a screen): the output has pilot_go, and a failed go rule carries the NO_GO title.

Raw-token rescoring: per unit, the parent's timeline (fork arms) and every leaf's
timeline are summed into the arm's cumulative raw tokens; t* = the first time that
sum exceeds RAW_TOKEN_CAP. A leaf counts only if it passed and end_s <= t*.
"""
from __future__ import annotations

import itertools
import json
import math
import random
import statistics
import sys
from collections import defaultdict

SEED = 1106
ALPHA = 0.05
PILOT_ALPHA = 0.10
WIN_POINTS = 0.05
COST_BAND = 0.10  # B's mean spend may exceed A's by up to 10% before WIN becomes WIN_MORE_EXPENSIVE
EPS = 1e-9  # float slack at the registered lines: a delta made of tenths can land at 0.0499999...
RAW_TOKEN_CAP = 2_900_000  # 4 x Vals' 727.86k tokens per test, rounded down


LEAVES_PER_UNIT = 4
LEAF_IDS = frozenset(range(LEAVES_PER_UNIT))
CORE_ARMS = ("A", "B", "C")
ALL_ARMS = ("A", "B", "C", "D")
FORK_ARMS = ("B", "C", "D")
HOLM_FAMILY = ("H2", "H3", "H4")
STRATA = (("a", "a"), ("b", "b"), ("all", None))


def read_rows(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _attempt(r):
    return int(r.get("attempt", 0) or 0)


def select_attempts(rows):
    """Units keyed by (task, rep, arm), at the highest `attempt` of their (task, rep).
    A rerun repeats the whole task-rep in every arm, so arms are never paired across
    attempts (rows without `attempt` are attempt 0)."""
    top = {}
    for r in rows:
        k = (r["task"], r["rep"])
        top[k] = max(top.get(k, 0), _attempt(r))
    units = defaultdict(list)
    for r in rows:
        if _attempt(r) == top[(r["task"], r["rep"])]:
            units[(r["task"], r["rep"], r["arm"])].append(r)
    return dict(units)


def load(path):
    return select_attempts(read_rows(path))


def arm_crossing(leaves, cap=None):
    """(t*, total): t* = first time the arm's cumulative raw tokens (the parent's,
    counted once per unit, plus every leaf's) exceed `cap` (default RAW_TOKEN_CAP;
    inf if never); total = the arm's final raw tokens."""
    cap = RAW_TOKEN_CAP if cap is None else cap
    series = [leaves[0].get("parent_tokens_timeline") or []] + [r.get("tokens_timeline") or [] for r in leaves]
    events = sorted((float(t), i, int(c)) for i, s in enumerate(series) for t, c in s)
    last = [0] * len(series)
    total, t_star = 0, math.inf
    for t, i, c in events:
        total += c - last[i]
        last[i] = c
        if total > cap and t_star == math.inf:
            t_star = t
    return t_star, total


def first_pass_time(leaves):
    ts = [r["end_s"] for r in leaves if r["passed"] and r.get("end_s") is not None]
    return min(ts) if ts else None


def unit_scores(leaves):
    any_pass = int(any(r["passed"] for r in leaves))
    conf = sorted((r for r in leaves if r["confirmed"]), key=lambda r: (r["end_s"], r["leaf"]))
    pick = conf[0] if conf else sorted(leaves, key=lambda r: (-r["episodes"], r["leaf"]))[0]
    leaf0 = [r for r in leaves if r["leaf"] == 0]
    t_star, raw_total = arm_crossing(leaves)
    end = lambda r: math.inf if r.get("end_s") is None else r["end_s"]  # noqa: E731
    ttp = first_pass_time(leaves)
    deadline = leaves[0].get("deadline_s")
    if ttp is not None:
        rm_time = min(ttp, deadline) if deadline else ttp
    else:
        rm_time = deadline  # unsolved: censored at the deadline
    return {
        "pass": any_pass,
        "sel": int(pick["passed"]),
        "p1": int(leaf0[0]["passed"]) if len(leaf0) == 1 else None,
        "leaf_mean": sum(r["passed"] for r in leaves) / len(leaves),
        "pass_raw": int(any(r["passed"] and end(r) <= t_star for r in leaves)),
        "crossed_cap": t_star != math.inf,
        "raw_total": raw_total,
        "has_timeline": any(r.get("tokens_timeline") for r in leaves) or bool(leaves[0].get("parent_tokens_timeline")),
        "ttp": ttp,
        "rm_time": rm_time,
        "rm_frac": (rm_time / deadline) if (deadline and rm_time is not None) else None,
        "usd_arm": leaves[0]["usd_arm"],
        "usd_actual": leaves[0]["usd_actual"],
        # a unit without exactly leaves 0-3 is invalid, never silently scored
        "valid": all(r.get("valid", True) for r in leaves)
        and len(leaves) == LEAVES_PER_UNIT and {r["leaf"] for r in leaves} == LEAF_IDS,
        "fork_failed": any(r.get("fork_failed") for r in leaves),
        "missing": any(r.get("missing") for r in leaves),
        "fail_codes": sorted({r["fail_code"] for r in leaves if r.get("fail_code")}),
        "stratum": leaves[0]["stratum"],
    }


def complete_reps(units, arms, exclude=frozenset()):
    """(task, rep) pairs where every arm in `arms` has a valid unit: complete-case
    across all those arms, so every comparison in a family uses the same reps.
    `exclude` drops further task-reps (the fork-failure sensitivity)."""
    reps = sorted({(t, r) for (t, r, a) in units})
    ok = []
    for t, r in reps:
        if (t, r) in exclude:
            continue
        us = [units.get((t, r, a)) for a in arms]
        if all(us) and all(unit_scores(u)["valid"] for u in us):
            ok.append((t, r))
    return ok


def fork_failed_reps(units):
    """Task-reps with a fork failure in any fork arm."""
    return frozenset((t, r) for (t, r, a), u in units.items() if a in FORK_ARMS and unit_scores(u)["fork_failed"])


def per_task(units, arm_x, arm_y, metric, stratum=None, exclude=frozenset(), metric_x=None):
    """Per-task mean over complete-case reps of metric(Y) - metric_x(X); metric_x
    defaults to metric."""
    family = ALL_ARMS if "D" in (arm_x, arm_y) else CORE_ARMS
    mx = metric_x or metric
    by = defaultdict(list)
    for t, r in complete_reps(units, family, exclude):
        sx, sy = unit_scores(units[(t, r, arm_x)]), unit_scores(units[(t, r, arm_y)])
        if stratum and sx["stratum"] != stratum:
            continue
        if sx[mx] is None or sy[metric] is None:
            continue
        by[t].append(sy[metric] - sx[mx])
    return {t: sum(v) / len(v) for t, v in by.items()}


def signflip_p(diffs, rng):
    d = [x for x in diffs if x != 0]
    obs = sum(diffs)
    if not d:
        return 1.0
    if len(d) <= 20:
        ge = tot = 0
        for signs in itertools.product((1, -1), repeat=len(d)):
            tot += 1
            ge += sum(s * x for s, x in zip(signs, d)) >= obs - 1e-12
        return ge / tot
    n, ge = 100_000, 0
    for _ in range(n):
        ge += sum(x if rng.random() < 0.5 else -x for x in d) >= obs - 1e-12
    return (ge + 1) / (n + 1)


def boot_ci(diffs, rng, level=0.90, n=10_000):
    if not diffs:
        return (float("nan"), float("nan"))
    k = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(k)) / k for _ in range(n))
    lo = means[int((1 - level) / 2 * n)]
    hi = means[int((1 + level) / 2 * n) - 1]
    return lo, hi


def rng_for(name):
    """One seeded stream per comparison, so a result never depends on what ran before it."""
    return random.Random(f"{SEED}/{name}")


def compare(units, x, y, metric, stratum, rng, exclude=frozenset(), metric_x=None):
    pt = per_task(units, x, y, metric, stratum, exclude, metric_x)
    diffs = list(pt.values())
    est = sum(diffs) / len(diffs) if diffs else float("nan")
    return {"n_tasks": len(diffs), "delta": est, "p_one_sided": signflip_p(diffs, rng), "ci90": boot_ci(diffs, rng)}


def _arm_units(units, arm, stratum=None, exclude=frozenset()):
    family = ALL_ARMS if arm == "D" else CORE_ARMS
    v = [unit_scores(units[(t, r, arm)]) for t, r in complete_reps(units, family, exclude)]
    return [s for s in v if stratum is None or s["stratum"] == stratum]


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def spend(units, arm, stratum=None, exclude=frozenset()):
    v = _arm_units(units, arm, stratum, exclude)
    if not v:
        return {}
    solved = sum(s["pass"] for s in v)
    arm_usd = [s["usd_arm"] for s in v if s["usd_arm"] is not None]
    actual_usd = [s["usd_actual"] for s in v if s["usd_actual"] is not None]
    return {
        "units": len(v),
        "pass_rate": solved / len(v),
        # units without a cost record (e.g. a parent that died before any call) are left out
        # of the cost means and counted here; they still count in every pass rate
        "units_without_cost": len(v) - len(arm_usd),
        "usd_arm_mean": _mean(arm_usd),
        "usd_actual_mean": _mean(actual_usd),
        "usd_per_solve": (sum(arm_usd) / solved) if solved and arm_usd else None,
        "selected1_rate": sum(s["sel"] for s in v) / len(v),
        "pass_rate_raw_tokens": sum(s["pass_raw"] for s in v) / len(v),
        "share_crossing_cap": sum(s["crossed_cap"] for s in v) / len(v),
        "raw_tokens_mean": sum(s["raw_total"] for s in v) / len(v),
        "fork_failed_units": sum(s["fork_failed"] for s in v),
    }


def time_to_pass(units, arm, stratum=None):
    """Time to the first passing leaf, unsolved units censored at deadline_s."""
    v = _arm_units(units, arm, stratum)
    if not v:
        return {}
    solved = [s["ttp"] for s in v if s["ttp"] is not None]
    rm = [s["rm_time"] for s in v if s["rm_time"] is not None]
    rmf = [s["rm_frac"] for s in v if s["rm_frac"] is not None]
    return {
        "units": len(v),
        "share_solved": len(solved) / len(v),
        "median_solved_s": statistics.median(solved) if solved else None,
        "restricted_mean_s": _mean(rm),
        "restricted_mean_frac_of_deadline": _mean(rmf),
        "units_without_deadline": len(v) - len(rmf),
    }


def paired_time(units, metric, stratum, rng):
    """Paired B - A difference in the restricted mean (negative = B faster)."""
    diffs = list(per_task(units, "A", "B", metric, stratum).values())
    return {
        "n_tasks": len(diffs),
        "delta_B_minus_A": _mean(diffs),
        "ci90": boot_ci(diffs, rng),
        "p_one_sided_B_faster": signflip_p([-d for d in diffs], rng),
    }


def trigger_mix(units):
    """First-trigger counts over valid units: per lineage in A (each attempt has its own
    trigger), per unit in the fork arms (the shared parent's trigger)."""
    mix = defaultdict(lambda: defaultdict(int))
    for (t, r, a), u in units.items():
        if not unit_scores(u)["valid"]:
            continue
        for row in (u if a == "A" else u[:1]):
            mix[a][row.get("trigger") or "none"] += 1
    return {a: dict(sorted(c.items())) for a, c in sorted(mix.items())}


def holm(pvals, alpha):
    """Holm step-down over {name: p}; returns {name: rejected}."""
    ps = sorted(pvals.items(), key=lambda x: x[1])
    rej, stop = {}, False
    for i, (name, p) in enumerate(ps):
        ok = (not stop) and p <= alpha / (len(ps) - i)
        rej[name] = ok
        stop = stop or not ok
    return rej


def accounting(units, rows=None):
    reps = sorted({(t, r) for (t, r, a) in units})
    sc = {k: unit_scores(u) for k, u in units.items()}
    name = lambda k: f"{k[0]}/{k[1]}/{k[2]}"  # noqa: E731
    bad = sorted(name(k) for k, s in sc.items() if not s["valid"])
    missing = sorted(f"{t}/{r}/{a}" for t, r in reps for a in CORE_ARMS if (t, r, a) not in units)
    ff = defaultdict(list)
    for k, s in sc.items():
        if s["fork_failed"]:
            for c in sorted({r["fail_code"] for r in units[k] if r.get("fork_failed")}):
                ff[c].append(name(k))
    core = set(complete_reps(units, CORE_ARMS))
    out = {
        "task_reps_seen": len(reps),
        "complete_core": len(core),
        "complete_with_D": len(complete_reps(units, ALL_ARMS)),
        "invalid_units": bad,
        "invalid_unit_codes": {name(k): s["fail_codes"] for k, s in sorted(sc.items()) if not s["valid"]},
        "missing_planned_units": sorted(name(k) for k, s in sc.items() if s["missing"]),
        "missing_core_units": missing,
        "dropped_task_reps_core": sorted(f"{t}/{r}" for t, r in reps if (t, r) not in core),
        "fork_failures_by_code": {c: sorted(v) for c, v in sorted(ff.items())},
        "fork_failed_units_by_arm": {a: sum(1 for k, s in sc.items() if k[2] == a and s["fork_failed"]) for a in FORK_ARMS},
        # invalid or missing units piling up in the fork arms would hide fork trouble
        "invalid_units_by_arm": {a: sum(1 for k, s in sc.items() if k[2] == a and not s["valid"]) for a in ALL_ARMS},
        "task_reps_with_fork_failure": len(fork_failed_reps(units)),
        "verifier_crash_leaves": sorted(f"{name(k)}#{r['leaf']}" for k, u in units.items() for r in u
                                        if r.get("fail_code") == "verifier_crash"),
        "valid_units_without_token_timeline": sum(1 for s in sc.values() if s["valid"] and not s["has_timeline"]),
    }
    if rows is not None:
        top = {}
        for r in rows:
            top[(r["task"], r["rep"])] = max(top.get((r["task"], r["rep"]), 0), _attempt(r))
        earlier = defaultdict(set)
        earlier_ff = defaultdict(set)
        arms_at = defaultdict(set)
        for r in rows:
            k = (r["task"], r["rep"])
            arms_at[(k, _attempt(r))].add(r["arm"])
            if _attempt(r) < top[k] and not r.get("valid", True):
                earlier[k].add(f"{r['arm']}@{_attempt(r)}:{r.get('fail_code')}")
            if _attempt(r) < top[k] and r.get("fork_failed"):
                earlier_ff[k].add(f"{r['arm']}@{_attempt(r)}:{r.get('fail_code')}")
        out["reruns"] = {f"{t}/{r}": {"final_attempt": top[(t, r)], "earlier_invalid": sorted(earlier[(t, r)]),
                                      # fork failures and parent deaths a rerun of the task-rep replaced
                                      "earlier_fork_failures": sorted(earlier_ff[(t, r)])}
                         for (t, r) in sorted(top) if top[(t, r)] > 0}
        stale = []
        for (t, r), a_top in sorted(top.items()):
            seen = set().union(*(arms_at[((t, r), k)] for k in range(a_top + 1)))
            stale += [f"{t}/{r}/{a}" for a in sorted(seen - arms_at[((t, r), a_top)])]
        out["arms_absent_at_final_attempt"] = stale
    return out


# Post titles, fixed in advance (PREREG sections 1 and 5). NEGATIVE splits on whether H1
# rejected: a significant gain whose 90% CI stays under +5 points is "beat it, but by less
# than 5". NO_GO is the pilot's title when the go rule fails (there is then no full run).
TITLES = {
    "WIN": "Forking a live computer beat best-of-4 restarts on Terminal-Bench (pre-registered)",
    "WIN_MORE_EXPENSIVE": ("Forking a live computer beat best-of-4 restarts on Terminal-Bench, "
                           "but cost more (pre-registered)"),
    "NO_GO": "Forking a live computer did not show a benefit at pilot scale on Terminal-Bench (pre-registered)",
    "NEGATIVE": "Forking a live computer did not beat best-of-4 restarts on Terminal-Bench (pre-registered)",
    "NEGATIVE_SMALL": ("Forking a live computer beat best-of-4 restarts on Terminal-Bench, "
                       "but by less than 5 points (pre-registered)"),
    "INCONCLUSIVE": "Forking a live computer vs best-of-4 restarts on Terminal-Bench: inconclusive (pre-registered)",
}


def verdict_title(v, c, alpha):
    if v == "no data":
        return None
    if v == "NEGATIVE" and c["p_one_sided"] <= alpha:
        return TITLES["NEGATIVE_SMALL"]
    return TITLES.get(v, v)


def invalid_fork_tasks(units):
    """Tasks with a unit still invalid (at its final attempt) in any fork arm."""
    return frozenset(t for (t, r, a), u in units.items() if a in FORK_ARMS and not unit_scores(u)["valid"])


def reps_of_tasks(units, tasks):
    return frozenset((t, r) for (t, r, a) in units if t in tasks)


def cost_compare(sp_b, sp_a):
    """B's mean spend against A's (usd_arm, list price, parent included), with the +-10% band."""
    b, a = sp_b.get("usd_arm_mean"), sp_a.get("usd_arm_mean")
    if b is None or a is None:
        return {"usd_arm_mean_B": b, "usd_arm_mean_A": a, "ratio_B_to_A": None, "band": "unknown", "dearer": False}
    dearer = b > (1 + COST_BAND) * a + EPS
    cheaper = b < (1 - COST_BAND) * a - EPS
    r = b / a if a else (math.inf if b else 1.0)
    band = "B more than 10% dearer" if dearer else "B more than 10% cheaper" if cheaper else "within +-10%"
    return {"usd_arm_mean_B": b, "usd_arm_mean_A": a, "ratio_B_to_A": r, "band": band, "dearer": dearer}


def verdict(c, sp_b, sp_a, alpha):
    """Accuracy decides; cost can only turn WIN into WIN_MORE_EXPENSIVE (B > 1.10 x A)."""
    if c["n_tasks"] == 0:
        return "no data"
    if c["p_one_sided"] <= alpha and c["delta"] >= WIN_POINTS - EPS:
        return "WIN_MORE_EXPENSIVE" if cost_compare(sp_b, sp_a)["dearer"] else "WIN"
    if c["ci90"][1] < WIN_POINTS - EPS:
        return "NEGATIVE"
    return "INCONCLUSIVE"


PILOT_NO_VERDICT = "none (pilot: a screen, not a claim)"


def analyse(units, rows=None, pilot=False):
    alpha = PILOT_ALPHA if pilot else ALPHA
    arms = sorted({a for (_, _, a) in units})
    out = {"alpha": alpha, "pilot": pilot, "accounting": accounting(units, rows),
           "arms": {a: {s: spend(units, a, st) for s, st in STRATA} for a in arms}}
    h1 = compare(units, "A", "B", "pass", "a", rng_for("H1"))
    out["H1_B_vs_A_subset_a"] = h1
    out["H1_cost_B_vs_A_subset_a"] = cost_compare(spend(units, "B", "a"), spend(units, "A", "a"))
    if pilot:
        # a screen, not a claim: no verdict, no Holm family; a failed go rule is the NO_GO post
        out["pilot_go"] = bool(h1["n_tasks"]) and h1["delta"] > 0
        # no set-a data at all (everything invalid) is an infrastructure problem, not a NO-GO
        out["H1_verdict"] = PILOT_NO_VERDICT if h1["n_tasks"] else "no data"
        out["H1_title"] = None if (out["pilot_go"] or not h1["n_tasks"]) else TITLES["NO_GO"]
    else:
        out["H1_verdict"] = verdict(h1, spend(units, "B", "a"), spend(units, "A", "a"), alpha)
        out["H1_title"] = verdict_title(out["H1_verdict"], h1, alpha)
    sens_verdict = (lambda c, sb, sa: PILOT_NO_VERDICT) if pilot else (lambda c, sb, sa: verdict(c, sb, sa, alpha))
    # gatekept secondaries (Holm over H2, H3, H4) only if H1 rejected
    fam = {
        "H2": compare(units, "A", "B", "pass", None, rng_for("H2")),
        "H3": compare(units, "C", "B", "pass", "a", rng_for("H3")),
        "H4": compare(units, "D", "B", "pass", "a", rng_for("H4")),
    }
    out["H2_B_vs_A_all"], out["H3_B_vs_C_subset_a"], out["H4_B_vs_D_subset_a"] = fam["H2"], fam["H3"], fam["H4"]
    if pilot:
        out["holm_rejected"] = "not tested (pilot)"
    elif h1["p_one_sided"] <= alpha:
        out["holm_rejected"] = holm({h: fam[h]["p_one_sided"] for h in HOLM_FAMILY}, alpha)
    else:
        out["holm_rejected"] = "not tested (H1 not rejected)"
    # sensitivity: drop every task-rep with a fork failure or a parent death (fail_code
    # parent_died) in any fork arm; the primary keeps them as 0
    ex = fork_failed_reps(units)
    s1 = compare(units, "A", "B", "pass", "a", rng_for("S-H1"), exclude=ex)
    out["sensitivity_fork_failures_excluded"] = {
        "task_reps_dropped": sorted(f"{t}/{r}" for t, r in ex),
        "H1_B_vs_A_subset_a": s1,
        "H1_verdict_if_excluded": sens_verdict(s1, spend(units, "B", "a", ex), spend(units, "A", "a", ex)),
        "H3_B_vs_C_subset_a": compare(units, "C", "B", "pass", "a", rng_for("S-H3"), exclude=ex),
        "H4_B_vs_D_subset_a": compare(units, "D", "B", "pass", "a", rng_for("S-H4"), exclude=ex),
    }
    # sensitivity: invalid units clustering in the fork arms (they are dropped from all arms in
    # the primary, which could flatter B): repeat without every task that has one, all its reps
    bad_tasks = invalid_fork_tasks(units)
    ex2 = reps_of_tasks(units, bad_tasks)
    s2 = compare(units, "A", "B", "pass", "a", rng_for("I-H1"), exclude=ex2)
    out["sensitivity_invalid_fork_units_excluded"] = {
        "tasks_dropped": sorted(bad_tasks),
        "invalid_units_by_arm": out["accounting"]["invalid_units_by_arm"],
        "H1_B_vs_A_subset_a": s2,
        "H1_verdict_if_excluded": sens_verdict(s2, spend(units, "B", "a", ex2), spend(units, "A", "a", ex2)),
        "H3_B_vs_C_subset_a": compare(units, "C", "B", "pass", "a", rng_for("I-H3"), exclude=ex2),
        "H4_B_vs_D_subset_a": compare(units, "D", "B", "pass", "a", rng_for("I-H4"), exclude=ex2),
    }
    # secondary, no gate: raw-token rescoring
    out["raw_token_rescoring"] = {
        "cap_raw_tokens": RAW_TOKEN_CAP,
        "H1_B_vs_A_subset_a": compare(units, "A", "B", "pass_raw", "a", rng_for("R-H1")),
        "pass_rate_by_arm": {a: {s: spend(units, a, st).get("pass_rate_raw_tokens") for s, st in STRATA} for a in arms},
        "share_crossing_cap_by_arm": {a: spend(units, a).get("share_crossing_cap") for a in arms},
    }

    def rate(arm, metric, st):
        v = _arm_units(units, arm, st)
        return {"units": len(v), "rate": _mean([s[metric] for s in v if s[metric] is not None])}

    out["pass_at_1"] = {
        "A_leaf0": {s: rate("A", "p1", st) for s, st in STRATA},
        "A_mean_over_leaves": {s: rate("A", "leaf_mean", st) for s, st in STRATA},
    }
    out["trigger_mix"] = trigger_mix(units)
    out["time_to_first_pass"] = {a: {s: time_to_pass(units, a, st) for s, st in STRATA} for a in arms}
    out["time_to_first_pass"]["B_minus_A_subset_a"] = {
        "seconds": paired_time(units, "rm_time", "a", rng_for("T-s")),
        "frac_of_deadline": paired_time(units, "rm_frac", "a", rng_for("T-f")),
    }
    # exploratory, reported with no test claims
    out["explore"] = {
        "B_vs_D_subset_b": compare(units, "D", "B", "pass", "b", rng_for("E-BD-b")),
        "B_vs_A_subset_b": compare(units, "A", "B", "pass", "b", rng_for("E-BA-b")),
        "C_vs_A_subset_a": compare(units, "A", "C", "pass", "a", rng_for("E-CA-a")),
        "selected1_B_vs_A_subset_a": compare(units, "A", "B", "sel", "a", rng_for("E-sel-a")),
        "selected1_B_vs_A_all": compare(units, "A", "B", "sel", None, rng_for("E-sel-all")),
        "B_pass_arm_vs_A_pass1_subset_a": compare(units, "A", "B", "pass", "a", rng_for("E-p1-a"), metric_x="p1"),
        "B_pass_arm_vs_A_pass1_subset_b": compare(units, "A", "B", "pass", "b", rng_for("E-p1-b"), metric_x="p1"),
    }
    return out


def main():
    path = sys.argv[1]
    pilot = "--pilot" in sys.argv
    rows = read_rows(path)
    print(json.dumps(analyse(select_attempts(rows), rows, pilot), indent=1, default=str))


if __name__ == "__main__":
    main()
