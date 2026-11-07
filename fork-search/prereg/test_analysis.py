"""python3 -I -B test_analysis.py   (stdlib only; synthetic data, no model runs)

Pins ledger.py + analysis.py behaviour. Every `check_*` raises AssertionError when the
property breaks; the `breakage` tests monkeypatch the code to a plausible bug and
require the matching check to fail, so the checks are known to have teeth.
"""
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analysis as an  # noqa: E402
import ledger as lg  # noqa: E402
import sim_leaves as sim  # noqa: E402

FIELDS = {"task", "rep", "arm", "leaf", "stratum", "passed", "valid", "confirmed", "end_s", "episodes",
          "usd_arm", "usd_actual", "raw_tokens", "trigger", "attempt", "fork_failed", "fail_code",
          "stop_reason", "deadline_s", "tokens_timeline", "parent_tokens_timeline", "missing"}
PARENT = [[50.0, 5000]]


# ---------- builders ----------
def lrec(leaf, passed=0, end_s=None, tl=None, episodes=10):
    end_s = 100.0 + 10 * leaf if end_s is None else end_s
    stop = "confirmed" if passed else "deadline"
    return {"leaf": leaf, "passed": passed, "confirmed": stop == "confirmed", "end_s": end_s, "episodes": episodes,
            "stop_reason": stop, "tokens_timeline": [[end_s, 1000]] if tl is None else tl}


def ok(task, arm, passes=(0, 0, 0, 0), rep=0, attempt=0, parent=None, ends=None, tls=None, deadline=900.0, usd=1.0):
    leaves = [lrec(i, passes[i], ends[i] if ends else None, tls[i] if tls else None) for i in range(4)]
    return {"task": task, "rep": rep, "arm": arm, "attempt": attempt, "status": "ok", "deadline_s": deadline,
            "usd_arm": usd, "usd_actual": usd, "trigger": "none" if arm == "A" else "hard",
            "parent_tokens_timeline": [] if arm == "A" else (PARENT if parent is None else parent), "leaves": leaves}


def ff(task, arm, code="FORK_X", rep=0, attempt=0):
    return {"task": task, "rep": rep, "arm": arm, "attempt": attempt, "status": "fork_failed", "fail_code": code,
            "deadline_s": 900.0, "usd_arm": 0.1, "usd_actual": 0.05, "trigger": "hard", "parent_tokens_timeline": PARENT}


def infra(task, arm, code="SANDBOX_5XX", rep=0, attempt=0):
    return {"task": task, "rep": rep, "arm": arm, "attempt": attempt, "status": "infra_failed", "fail_code": code,
            "deadline_s": 900.0}


def P(task, arm, rep=0, stratum="a", attempt=0, deadline=900.0):
    return {"task": task, "rep": rep, "arm": arm, "stratum": stratum, "attempt": attempt, "deadline_s": deadline}


def scenario(spec, stratum=lambda t: "a"):
    """spec {(task, rep): {arm: passes tuple | 'ff' | None (missing)}} -> (units, rows)."""
    plan, outs = [], []
    for (t, rep), arms in spec.items():
        for arm, v in arms.items():
            plan.append(P(t, arm, rep, stratum(t)))
            if v == "ff":
                outs.append(ff(t, arm, rep=rep))
            elif v is not None:
                outs.append(ok(t, arm, v, rep=rep))
    rows = lg.build_rows(plan, outs)
    return an.select_attempts(rows), rows


def raises(fn, exc=lg.LedgerError):
    try:
        fn()
    except exc:
        return True
    raise AssertionError(f"{fn} did not raise {exc.__name__}")


Y, N = (1, 0, 0, 0), (0, 0, 0, 0)


# ---------- checks ----------
def check_four_rows():
    """4 rows per planned unit, a failed fork scored 0 and valid, a missing unit invalid."""
    plan = [P("t0", a) for a in "ABCD"]
    c = ok("t0", "C")
    c["leaves"][2] = {"leaf": 2, "fork_failed": True, "fail_code": "COPY_LOST"}
    rows = lg.build_rows(plan, [ok("t0", "A", Y), ff("t0", "B", "FORK_TIMEOUT"), c])
    assert len(rows) == 16, len(rows)
    by = {a: [r for r in rows if r["arm"] == a] for a in "ABCD"}
    for a in "ABCD":
        assert sorted(r["leaf"] for r in by[a]) == [0, 1, 2, 3], (a, by[a])
    for r in rows:
        assert FIELDS <= set(r), FIELDS - set(r)
    for r in by["B"]:
        assert (r["passed"], r["valid"], r["fork_failed"], r["fail_code"], r["stop_reason"]) == \
            (0, True, True, "FORK_TIMEOUT", "fork_failed"), r
        assert r["parent_tokens_timeline"] == PARENT
    assert [r["fork_failed"] for r in by["C"]] == [False, False, True, False]
    assert by["C"][2]["passed"] == 0 and by["C"][2]["valid"] and by["C"][2]["fail_code"] == "COPY_LOST"
    for r in by["D"]:
        assert (r["valid"], r["missing"], r["fail_code"], r["passed"]) == (False, True, "missing", 0), r
    for r in by["A"]:
        assert r["parent_tokens_timeline"] == [] and not r["fork_failed"]
    u = an.select_attempts(rows)
    sb, sd = an.unit_scores(u[("t0", 0, "B")]), an.unit_scores(u[("t0", 0, "D")])
    assert sb["valid"] and sb["fork_failed"] and sb["pass"] == 0, sb
    assert not sd["valid"] and sd["missing"], sd


def check_fork_sensitivity():
    """Primary: a fork failure scores 0 against B. Sensitivity: that task-rep leaves every arm."""
    spec = {(f"t{i}", 0): {"A": N, "B": Y, "C": N, "D": N} for i in range(5)}
    spec[("t5", 0)] = {"A": Y, "B": "ff", "C": N, "D": N}  # B's fork failed: 0 vs A's 1
    spec[("t6", 0)] = {"A": Y, "B": Y, "C": N, "D": "ff"}  # a fork failure in D drops the rep too
    units, rows = scenario(spec)
    assert an.per_task(units, "A", "B", "pass", "a")["t5"] == -1
    out = an.analyse(units, rows)
    h1 = out["H1_B_vs_A_subset_a"]
    assert h1["n_tasks"] == 7 and abs(h1["delta"] - 4 / 7) < 1e-12, h1
    s = out["sensitivity_fork_failures_excluded"]
    assert s["task_reps_dropped"] == ["t5/0", "t6/0"], s
    assert s["H1_B_vs_A_subset_a"]["n_tasks"] == 5 and s["H1_B_vs_A_subset_a"]["delta"] == 1.0, s
    assert s["H3_B_vs_C_subset_a"]["n_tasks"] == 5 and s["H4_B_vs_D_subset_a"]["n_tasks"] == 5, s
    assert out["accounting"]["fork_failures_by_code"] == {"FORK_X": ["t5/0/B", "t6/0/D"]}, out["accounting"]


def check_cap():
    """A passing leaf that ended after the arm crossed RAW_TOKEN_CAP does not count."""
    def unit(end0, top):
        rows = lg.build_rows([P("t0", "A")], [ok("t0", "A", Y, ends=[end0, 400.0, 100.0, 100.0], tls=[
            [[end0, 10]], [[100.0, 1_000_000], [400.0, top]], [[100.0, 0]], [[100.0, 0]]])])
        return an.select_attempts(rows)[("t0", 0, "A")]
    u = unit(500.0, 3_000_000)
    assert an.arm_crossing(u) == (400.0, 3_000_010), an.arm_crossing(u)
    s = an.unit_scores(u)
    assert s["pass"] == 1 and s["pass_raw"] == 0 and s["crossed_cap"], s
    assert an.unit_scores(unit(400.0, 3_000_000))["pass_raw"] == 1  # ended exactly at t*: counts
    assert an.unit_scores(unit(500.0, 2_800_000))["pass_raw"] == 1  # no crossing: counts
    # the rescored H1 sees it, the primary does not
    plan = [P(t, a) for t in ("t0", "t1") for a in "ABC"]
    late = ok("t0", "B", Y, ends=[900.0, 100, 100, 100], tls=[[[900.0, 10]], [[200.0, 3_000_000]], [], []])
    outs = [ok(t, a) for t in ("t0", "t1") for a in "AC"] + [late, ok("t1", "B", Y)]
    rows = lg.build_rows(plan, outs)
    out = an.analyse(an.select_attempts(rows), rows)
    assert out["H1_B_vs_A_subset_a"]["delta"] == 1.0, out["H1_B_vs_A_subset_a"]
    assert out["raw_token_rescoring"]["H1_B_vs_A_subset_a"]["delta"] == 0.5, out["raw_token_rescoring"]


def check_parent_counts():
    """The shared parent's tokens count in every fork arm (once per unit), never in A."""
    parent = [[50.0, 1_000_000]]
    tls = [[[600.0, 10]], [[300.0, 2_000_000]], [[100.0, 0]], [[100.0, 0]]]
    ends = [600.0, 300.0, 100.0, 100.0]
    plan = [P("t0", a) for a in "ABCD"]
    outs = [ok("t0", a, Y, parent=parent, ends=ends, tls=tls) for a in "ABCD"]
    rows = lg.build_rows(plan, outs)
    for r in rows:
        assert r["parent_tokens_timeline"] == ([] if r["arm"] == "A" else parent), r
    u = an.select_attempts(rows)
    for a in "BCD":
        s = an.unit_scores(u[("t0", 0, a)])
        assert s["raw_total"] == 3_000_010 and s["pass"] == 1 and s["pass_raw"] == 0, (a, s)
    s = an.unit_scores(u[("t0", 0, "A")])
    assert s["raw_total"] == 2_000_010 and s["pass_raw"] == 1, s


def check_pass1():
    """pass@1 is A's leaf 0, whatever the row order."""
    plan = [P(t, a) for t in ("t0", "t1") for a in "ABC"]
    outs = [ok("t0", "A", (0, 0, 0, 1)), ok("t1", "A", Y)] + [ok(t, "B", Y) for t in ("t0", "t1")] + \
           [ok(t, "C") for t in ("t0", "t1")]
    rows = lg.build_rows(plan, outs)[::-1]
    u = an.select_attempts(rows)
    s0, s1 = an.unit_scores(u[("t0", 0, "A")]), an.unit_scores(u[("t1", 0, "A")])
    assert (s0["p1"], s0["pass"], s1["p1"]) == (0, 1, 1), (s0, s1)
    out = an.analyse(u, rows)
    assert out["pass_at_1"]["A_leaf0"]["a"] == {"units": 2, "rate": 0.5}, out["pass_at_1"]
    assert out["pass_at_1"]["A_mean_over_leaves"]["a"]["rate"] == 0.25, out["pass_at_1"]
    assert out["explore"]["B_pass_arm_vs_A_pass1_subset_a"]["delta"] == 0.5, out["explore"]


def check_ttp():
    """Time to first pass: min end_s of passing leaves; unsolved units sit at the deadline."""
    plan = [P(t, a) for t in ("t0", "t1") for a in "ABC"]
    outs = [ok("t0", "A", Y, ends=[100.0, 50, 50, 50]),
            ok("t0", "B", (0, 1, 1, 0), ends=[50.0, 300, 200, 50]),
            ok("t1", "A", N), ok("t1", "B", Y, ends=[450.0, 50, 50, 50])] + [ok(t, "C") for t in ("t0", "t1")]
    rows = lg.build_rows(plan, outs)
    u = an.select_attempts(rows)
    assert an.unit_scores(u[("t0", 0, "B")])["ttp"] == 200.0
    assert an.unit_scores(u[("t1", 0, "A")])["rm_time"] == 900.0  # censored at the deadline
    out = an.analyse(u, rows)["time_to_first_pass"]
    a, b = out["A"]["a"], out["B"]["a"]
    assert (a["share_solved"], a["median_solved_s"], a["restricted_mean_s"]) == (0.5, 100.0, 500.0), a
    assert abs(a["restricted_mean_frac_of_deadline"] - (100 / 900 + 1) / 2) < 1e-12, a
    assert (b["share_solved"], b["median_solved_s"], b["restricted_mean_s"]) == (1.0, 325.0, 325.0), b
    assert out["B_minus_A_subset_a"]["seconds"]["delta_B_minus_A"] == -175.0, out["B_minus_A_subset_a"]


def check_holm_family():
    """H4 is in the Holm family with H2 and H3. Data where H2 and H3 have p = 10/512
    (between alpha/3 and alpha/2) and H4 has p = 1: with H4 in the family nothing in it
    rejects; a two-member family would reject both."""
    spec = {(f"t{i}", 0): {"A": N, "B": Y, "C": N, "D": Y} for i in range(8)}
    spec[("t8", 0)] = {"A": Y, "B": N, "C": Y, "D": N}
    units, rows = scenario(spec)
    out = an.analyse(units, rows)
    p2, p3, p4 = (out[k]["p_one_sided"] for k in ("H2_B_vs_A_all", "H3_B_vs_C_subset_a", "H4_B_vs_D_subset_a"))
    assert (p2, p3) == (10 / 512, 10 / 512) and p4 == 1.0, (p2, p3, p4)
    assert out["H1_B_vs_A_subset_a"]["p_one_sided"] <= an.ALPHA
    assert an.holm({"H2": p2, "H3": p3}, an.ALPHA) == {"H2": True, "H3": True}  # the data discriminate
    assert out["holm_rejected"] == {"H2": False, "H3": False, "H4": False}, out["holm_rejected"]


def check_h4_gated():
    """H4 is tested only after H1 rejects: B ties A but beats D everywhere."""
    units, rows = scenario({(f"t{i}", 0): {"A": Y, "B": Y, "C": N, "D": N} for i in range(6)})
    out = an.analyse(units, rows)
    assert out["H4_B_vs_D_subset_a"]["p_one_sided"] == 1 / 64, out["H4_B_vs_D_subset_a"]
    assert out["holm_rejected"] == "not tested (H1 not rejected)", out["holm_rejected"]
    units, rows = scenario({(f"t{i}", 0): {"A": N, "B": Y, "C": N, "D": N} for i in range(6)})
    assert an.analyse(units, rows)["holm_rejected"] == {"H2": True, "H3": True, "H4": True}


def check_h4_d_reps():
    """H4 uses the reps where all of A-D are valid: D in every rep, or rep 0 only."""
    every, first = {}, {}
    for i in range(4):
        every[(f"t{i}", 0)] = first[(f"t{i}", 0)] = {"A": N, "B": Y, "C": N, "D": N}
        every[(f"t{i}", 1)] = {"A": N, "B": N, "C": N, "D": Y}
        first[(f"t{i}", 1)] = {"A": N, "B": N, "C": N}
    ue, re_ = scenario(every)
    uf, rf = scenario(first)
    oe, of = an.analyse(ue, re_), an.analyse(uf, rf)
    assert oe["H4_B_vs_D_subset_a"]["delta"] == 0.0 and of["H4_B_vs_D_subset_a"]["delta"] == 1.0
    assert oe["accounting"]["complete_with_D"] == 8 and of["accounting"]["complete_with_D"] == 4
    assert of["accounting"]["missing_planned_units"] == [] and of["accounting"]["complete_core"] == 8
    assert of["H1_B_vs_A_subset_a"]["delta"] == 0.5  # H1 still uses both reps


def check_missing_listed():
    """A planned unit with no outcome is listed and its task-rep is not scored."""
    spec = {(t, 0): {"A": Y, "B": N, "C": N} for t in ("t0", "t1", "t2")}
    spec[("t1", 0)] = {"A": Y, "B": None, "C": N, "D": None}
    units, rows = scenario(spec)
    out = an.analyse(units, rows)
    acc = out["accounting"]
    assert acc["missing_planned_units"] == ["t1/0/B", "t1/0/D"], acc
    assert acc["invalid_units_by_arm"] == {"A": 0, "B": 1, "C": 0, "D": 1}, acc
    assert "t1/0" in acc["dropped_task_reps_core"]
    assert out["H1_B_vs_A_subset_a"]["n_tasks"] == 2 and out["arms"]["A"]["a"]["units"] == 2
    plan = [P(t, a) for (t, _), arms in spec.items() for a in arms]
    summ = lg.summarize(plan, rows)
    assert summ["missing_units"] == ["t1/0/B@0", "t1/0/D@0"], summ
    assert [e["task"] for e in summ["rerun_owed"]] == ["t1"], summ


def check_reruns():
    """Infra failure: rows invalid, rerun owed in every arm, highest attempt of the task-rep wins,
    dropped after MAX_RERUNS reruns."""
    plan = [P("t0", a) for a in "ABC"]
    outs = [infra("t0", "A"), ok("t0", "B"), ok("t0", "C")]
    rows = lg.build_rows(plan, outs)
    assert all(not r["valid"] for r in rows if r["arm"] == "A") and len(rows) == 12
    assert lg.rerun_status(plan, rows)["rerun_owed"][0]["invalid"] == ["A:SANDBOX_5XX"]
    plan += [P("t0", a, attempt=1) for a in "ABC"]
    outs += [ok("t0", "A", attempt=1), ok("t0", "B", Y, attempt=1), ok("t0", "C", attempt=1)]
    rows = lg.build_rows(plan, outs)
    assert lg.rerun_status(plan, rows) == {"rerun_owed": [], "dropped": []}
    u = an.select_attempts(rows)
    assert an.unit_scores(u[("t0", 0, "B")])["pass"] == 1 and {r["attempt"] for r in u[("t0", 0, "B")]} == {1}
    acc = an.accounting(u, rows)
    assert acc["reruns"] == {"t0/0": {"final_attempt": 1, "earlier_invalid": ["A@0:SANDBOX_5XX"]}}, acc
    # still invalid at attempt 2 -> dropped and listed
    plan2 = [P("t1", a, attempt=k) for k in range(3) for a in "AB"]
    outs2 = [o for k in range(3) for o in (ok("t1", "A", attempt=k), infra("t1", "B", "RUNNER_LOST", attempt=k))]
    rows2 = lg.build_rows(plan2, outs2)
    st = lg.rerun_status(plan2, rows2)
    assert st["rerun_owed"] == [] and st["dropped"][0]["invalid"] == ["B:RUNNER_LOST"], st
    # a rerun that skipped an arm: never paired across attempts, and listed
    rows3 = [r for r in rows if not (r["attempt"] == 1 and r["arm"] == "B")]
    u3 = an.select_attempts(rows3)
    acc3 = an.accounting(u3, rows3)
    assert ("t0", 0, "B") not in u3 and acc3["arms_absent_at_final_attempt"] == ["t0/0/B"], acc3
    assert acc3["complete_core"] == 0 and acc3["missing_core_units"] == ["t0/0/B"]


def check_ledger_rejects():
    p = [P("t0", a) for a in "AB"]
    raises(lambda: lg.build_rows(p, [ok("t9", "A")]))  # unplanned
    raises(lambda: lg.build_rows(p, [ok("t0", "A"), ok("t0", "A")]))  # duplicate
    raises(lambda: lg.build_rows(p + [P("t0", "A")], []))  # planned twice
    raises(lambda: lg.build_rows(p, [ff("t0", "A")]))  # A has no fork
    raises(lambda: lg.build_rows(p, [dict(infra("t0", "A"), fail_code=None)]))  # no code
    bad = ok("t0", "B")
    bad["leaves"][1] = {"leaf": 1, "fork_failed": True, "fail_code": "X", "passed": 1}
    raises(lambda: lg.build_rows(p, [bad]))  # failed fork carrying a verifier pass
    dec = ok("t0", "A", tls=[[[10.0, 50], [20.0, 40]], [], [], []])
    raises(lambda: lg.build_rows(p, [dec]))  # decreasing cumulative tokens
    mis = ok("t0", "A")
    mis["leaves"][0]["confirmed"] = True
    raises(lambda: lg.build_rows(p, [mis]))  # confirmed but stop_reason deadline
    noend = ok("t0", "A")
    noend["leaves"][2]["end_s"] = None
    raises(lambda: lg.build_rows(p, [noend]))  # a leaf that ran needs its end time
    raises(lambda: lg.build_rows(p, [dict(ok("t0", "A"), parent_tokens_timeline=PARENT)]))  # A with a parent
    pc = [P("t0", a) for a in "BC"]
    raises(lambda: lg.build_rows(pc, [ok("t0", "B"), ok("t0", "C", parent=[[60.0, 7000]])]))  # two parents
    raises(lambda: lg.build_rows(pc[:1], [ok("t0", "B", parent=[])]))  # a fork arm without its parent's tokens
    raises(lambda: lg.build_rows(p[:1], [dict(ok("t0", "A"), deadline_s=1800.0)]))  # deadline differs from plan
    # incomplete leaf records -> invalid unit; a verifier crash -> 0, listed, valid
    inc = ok("t0", "A")
    inc["leaves"] = inc["leaves"][:3]
    rows = lg.build_rows(p[:1], [inc])
    assert len(rows) == 4 and all(not r["valid"] and r["fail_code"] == lg.INCOMPLETE for r in rows)
    vc = ok("t0", "A", Y)
    vc["leaves"][0]["passed"] = None
    rows = lg.build_rows(p[:1], [vc])
    assert rows[0]["passed"] == 0 and rows[0]["valid"] and rows[0]["fail_code"] == lg.VERIFIER_CRASH
    assert lg.summarize(p[:1], rows)["verifier_crash_leaves"] == ["t0/0/A@0#0"]


def check_unit_validity():
    """Exactly leaves 0-3, once each, or the unit is invalid."""
    base = [dict(r) for r in lg.build_rows([P("t0", "A")], [ok("t0", "A")])]
    assert an.unit_scores(base)["valid"]
    assert not an.unit_scores(base[:3])["valid"]
    assert not an.unit_scores(base + base)["valid"]  # a rerun merged into one unit
    assert not an.unit_scores(base[:3] + [dict(base[3], leaf=2)])["valid"]
    assert not an.unit_scores(base[:3] + [dict(base[3], leaf=5)])["valid"]
    assert not an.unit_scores(base[:3] + [dict(base[3], valid=False)])["valid"]


def check_verdict_and_pairing():
    c = lambda p, d, hi, n=10: {"n_tasks": n, "delta": d, "p_one_sided": p, "ci90": (d - 0.1, hi)}  # noqa: E731
    sp = lambda x: {"usd_arm_mean": x}  # noqa: E731
    assert an.verdict(c(0.01, 0.08, 0.15), sp(1.0), sp(1.2), 0.05) == "WIN"
    assert an.verdict(c(0.01, 0.08, 0.15), sp(1.3), sp(1.2), 0.05) == "INCONCLUSIVE"  # B costs more
    assert an.verdict(c(0.30, 0.00, 0.04), sp(1.0), sp(1.2), 0.05) == "NEGATIVE"
    assert an.verdict(c(0.20, 0.03, 0.12), sp(1.0), sp(1.2), 0.05) == "INCONCLUSIVE"
    assert an.verdict(c(1.0, float("nan"), float("nan"), 0), {}, {}, 0.05) == "no data"
    # complete-case: an invalid C drops the rep from H1; an invalid D does not
    plan = [P(t, a) for t in ("t0", "t1") for a in "ABCD"]
    outs = [ok(t, a, Y if a == "B" else N) for t in ("t0", "t1") for a in "ABD"] + [infra("t0", "C"), ok("t1", "C")]
    rows = lg.build_rows(plan, outs)
    u = an.select_attempts(rows)
    assert an.per_task(u, "A", "B", "pass", "a") == {"t1": 1.0}
    outs = [ok(t, a, Y if a == "B" else N) for t in ("t0", "t1") for a in "ABC"] + [infra("t0", "D"), ok("t1", "D")]
    u = an.select_attempts(lg.build_rows(plan, outs))
    assert an.per_task(u, "A", "B", "pass", "a") == {"t0": 1.0, "t1": 1.0}
    assert an.per_task(u, "D", "B", "pass", "a") == {"t1": 1.0}
    # a comparison does not depend on what ran before it
    units, rows = scenario({(f"t{i}", 0): {"A": N, "B": Y if i % 12 else N, "C": N} for i in range(25)})
    out = an.analyse(units, rows)
    assert out["H2_B_vs_A_all"] == an.compare(units, "A", "B", "pass", None, an.rng_for("H2"))


def check_backcompat():
    """Rows in the old schema (no ledger fields) still analyse."""
    rows = []
    for t in range(4):
        for arm in "ABCD":
            for lf in range(4):
                rows.append({"task": f"t{t}", "rep": 0, "arm": arm, "leaf": lf, "stratum": "a",
                             "passed": int(arm == "B" and lf == 0), "valid": True, "confirmed": False,
                             "end_s": 100.0, "episodes": 5, "usd_arm": 1.0, "usd_actual": 1.0,
                             "raw_tokens": 700000, "trigger": "none"})
    out = an.analyse(an.select_attempts(rows), rows)
    assert out["H1_B_vs_A_subset_a"]["delta"] == 1.0
    assert out["raw_token_rescoring"]["H1_B_vs_A_subset_a"]["delta"] == 1.0
    assert out["accounting"]["valid_units_without_token_timeline"] == 16
    assert out["time_to_first_pass"]["A"]["a"]["units_without_deadline"] == 4


def check_negative_titles():
    """A significant gain whose 90% CI stays under +5 points is NEGATIVE, titled "beat it, but by
    less than 5 points"; a NEGATIVE without H1 rejecting keeps "did not beat"."""
    c = lambda p, d, hi: {"n_tasks": 400, "delta": d, "p_one_sided": p, "ci90": (d - 0.01, hi)}  # noqa: E731
    sp = lambda x: {"usd_arm_mean": x}  # noqa: E731
    small = c(0.03, 0.0125, 0.03)
    assert an.verdict(small, sp(1.0), sp(1.2), 0.05) == "NEGATIVE"
    assert an.verdict_title("NEGATIVE", small, 0.05) == an.TITLES["NEGATIVE_SMALL"]
    assert "but by less than 5 points" in an.TITLES["NEGATIVE_SMALL"]
    assert an.verdict_title("NEGATIVE", c(0.40, 0.0, 0.03), 0.05) == an.TITLES["NEGATIVE"]
    assert "did not beat" in an.TITLES["NEGATIVE"]
    assert an.verdict_title("WIN", c(0.01, 0.08, 0.15), 0.05) == an.TITLES["WIN"]
    # end to end: 400 a-tasks, B wins 5 more units than A and nothing else differs
    spec = {(f"t{i}", 0): {"A": N, "B": Y if i < 5 else N, "C": N} for i in range(400)}
    units, rows = scenario(spec)
    out = an.analyse(units, rows)
    h1 = out["H1_B_vs_A_subset_a"]
    assert h1["p_one_sided"] <= an.ALPHA and h1["ci90"][1] < an.WIN_POINTS, h1
    assert out["H1_verdict"] == "NEGATIVE" and out["H1_title"] == an.TITLES["NEGATIVE_SMALL"], out["H1_title"]


def check_invalid_fork_sensitivity():
    """Invalid units in fork arms are counted per arm, and a sensitivity repeats H1, H3 and H4
    without every task that has one (all its reps, all arms)."""
    spec = {(f"t{i}", r): {"A": N, "B": Y, "C": N, "D": N} for i in range(6) for r in range(2)}
    units0, _ = scenario(spec)
    plan = [P(t, a, r) for (t, r), arms in spec.items() for a in arms]
    outs = []
    for (t, r), arms in spec.items():
        for a, v in arms.items():
            outs.append(infra(t, a, rep=r) if (t, r, a) == ("t0", 1, "B") else ok(t, a, v, rep=r))
    rows = lg.build_rows(plan, outs)
    units = an.select_attempts(rows)
    out = an.analyse(units, rows)
    sens = out["sensitivity_invalid_fork_units_excluded"]
    assert sens["tasks_dropped"] == ["t0"] and sens["invalid_units_by_arm"]["B"] == 1, sens
    assert out["H1_B_vs_A_subset_a"]["n_tasks"] == 6          # primary: only t0/rep1 is dropped
    assert sens["H1_B_vs_A_subset_a"]["n_tasks"] == 5          # sensitivity: all of t0 is dropped
    assert out["sensitivity_invalid_fork_units_excluded"]["H4_B_vs_D_subset_a"]["n_tasks"] == 5
    out0 = an.analyse(units0, None)
    assert out0["sensitivity_invalid_fork_units_excluded"]["tasks_dropped"] == []


def check_parent_died_rerun_once():
    """A parent that dies before its fork: invalid, rerun once, then dropped and listed."""
    plan = [P("t0", a) for a in "ABCD"]
    outs = [ok("t0", "A")] + [infra("t0", a, lg.PARENT_DIED) for a in "BCD"]
    rows = lg.build_rows(plan, outs)
    assert all(not r["valid"] for r in rows if r["arm"] != "A")
    assert len(lg.rerun_status(plan, rows)["rerun_owed"]) == 1
    plan += [P("t0", a, attempt=1) for a in "ABCD"]
    outs += [ok("t0", "A", attempt=1)] + [infra("t0", a, lg.PARENT_DIED, attempt=1) for a in "BCD"]
    rows = lg.build_rows(plan, outs)
    st = lg.rerun_status(plan, rows)
    assert st["rerun_owed"] == [] and st["dropped"][0]["invalid"] == ["B:parent_died", "C:parent_died", "D:parent_died"], st
    # other infra failures still get two reruns
    plan2 = [P("t1", a, attempt=k) for k in range(2) for a in "AB"]
    outs2 = [o for k in range(2) for o in (ok("t1", "A", attempt=k), infra("t1", "B", "SANDBOX_5XX", attempt=k))]
    assert len(lg.rerun_status(plan2, lg.build_rows(plan2, outs2))["rerun_owed"]) == 1
    raises(lambda: lg.build_rows([P("t2", "A")], [infra("t2", "A", lg.PARENT_DIED)]))  # A has no parent
    raises(lambda: lg.build_rows([P("t2", "B")], [ff("t2", "B", lg.PARENT_DIED)]))     # not a fork failure


# ---------- tests ----------
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


for chk in (check_four_rows, check_fork_sensitivity, check_cap, check_parent_counts, check_pass1, check_ttp,
            check_holm_family, check_h4_gated, check_h4_d_reps, check_missing_listed, check_reruns,
            check_ledger_rejects, check_unit_validity, check_verdict_and_pairing, check_backcompat,
            check_negative_titles, check_invalid_fork_sensitivity, check_parent_died_rerun_once):
    test(chk)


def breaks(check, patches):
    """Apply (module, attr, value) patches; `check` must fail; restore."""
    saved = [(m, a, getattr(m, a)) for m, a, _ in patches]
    try:
        for m, a, v in patches:
            setattr(m, a, v)
        try:
            check()
        except (AssertionError, lg.LedgerError, KeyError, TypeError):
            return
        raise AssertionError(f"{check.__name__} still passed under {[(m.__name__, a) for m, a, _ in patches]}")
    finally:
        for m, a, v in saved:
            setattr(m, a, v)


@test
def breakage_title_ignores_h1():
    breaks(check_negative_titles, [(an, "verdict_title", lambda v, c, alpha: an.TITLES.get(v, v))])


@test
def breakage_invalid_sensitivity_per_rep():
    breaks(check_invalid_fork_sensitivity, [(an, "reps_of_tasks", lambda units, tasks: frozenset())])


@test
def breakage_parent_died_two_reruns():
    breaks(check_parent_died_rerun_once, [(lg, "MAX_RERUNS_BY_CODE", {})])


@test
def breakage_ledger_drops_failed_fork_rows():
    breaks(check_four_rows, [(lg, "_fork_failed_rows", lambda p, o: [])])


@test
def breakage_ledger_marks_failed_forks_invalid():
    orig = lg._dead_rows
    bad = lambda p, o: orig(p, o, valid=False, fork_failed=True, fail_code=o["fail_code"], stop_reason="fork_failed")  # noqa: E731
    breaks(check_four_rows, [(lg, "_fork_failed_rows", bad)])
    breaks(check_fork_sensitivity, [(lg, "_fork_failed_rows", bad)])


@test
def breakage_sensitivity_keeps_fork_failures():
    breaks(check_fork_sensitivity, [(an, "fork_failed_reps", lambda units: frozenset())])


@test
def breakage_rescoring_ignores_parent():
    orig = an.arm_crossing
    no_parent = lambda leaves, cap=None: orig([dict(r, parent_tokens_timeline=[]) for r in leaves], cap)  # noqa: E731
    breaks(check_parent_counts, [(an, "arm_crossing", no_parent)])


@test
def breakage_rescoring_ignores_cap():
    breaks(check_cap, [(an, "RAW_TOKEN_CAP", 10**12)])


@test
def breakage_pass1_any_leaf():
    orig = an.unit_scores
    breaks(check_pass1, [(an, "unit_scores", lambda leaves: dict(orig(leaves), p1=orig(leaves)["pass"]))])


@test
def breakage_ttp_without_censoring():
    orig = an.unit_scores
    breaks(check_ttp, [(an, "unit_scores", lambda leaves: dict(orig(leaves), rm_time=orig(leaves)["ttp"]))])


@test
def breakage_holm_without_h4():
    breaks(check_holm_family, [(an, "HOLM_FAMILY", ("H2", "H3"))])


@test
def breakage_ledger_skips_missing_units():
    orig = lg.unit_rows
    breaks(check_missing_listed, [(lg, "unit_rows", lambda p, o: [] if o is None else orig(p, o))])


@test
def breakage_attempt_per_unit():
    """The old per-unit 'highest attempt' would pair A@1 with B@0 when B's rerun is absent."""
    def per_unit(rows):
        top = {}
        for r in rows:
            k = (r["task"], r["rep"], r["arm"])
            top[k] = max(top.get(k, 0), an._attempt(r))
        out = {}
        for r in rows:
            k = (r["task"], r["rep"], r["arm"])
            if an._attempt(r) == top[k]:
                out.setdefault(k, []).append(r)
        return out
    breaks(check_reruns, [(an, "select_attempts", per_unit)])


@test
def sim_deterministic_and_isolated():
    a1, b1 = sim.simulate(0.0), sim.simulate(0.0)
    assert a1 == b1
    rows0 = lg.build_rows(*sim.simulate(0.0))
    rows2 = lg.build_rows(*sim.simulate(2.5))
    rowsf = lg.build_rows(*sim.simulate(0.0, d_first_rep_only=True))
    pick = lambda rows, arms: [r for r in rows if r["arm"] in arms]  # noqa: E731
    assert pick(rows0, "ACD") == pick(rows2, "ACD")  # EFFECT touches only B
    assert pick(rows0, "A") == pick(rowsf, "A")  # the D flag touches no A draw
    assert not [r for r in rowsf if r["arm"] == "D" and r["rep"] > 0]
    assert len(rows0) == 4 * len(sim.simulate(0.0)[0])
    s = lg.summarize(sim.simulate(0.0)[0], rows0)
    assert s["missing_units"] == ["t21/1/B@0"] and s["dropped"][0]["task"] == "t20"
    assert set(s["fork_failed_leaves_by_code"]) == {"FORK_API_5XX", "TAR_RESTORE", "COPY_UNREACHABLE"}


def headline(out):
    h1, r1 = out["H1_B_vs_A_subset_a"], out["raw_token_rescoring"]["H1_B_vs_A_subset_a"]
    t = out["time_to_first_pass"]
    return {
        "H1_delta": h1["delta"], "H1_p": h1["p_one_sided"], "verdict": out["H1_verdict"],
        "H4_delta": out["H4_B_vs_D_subset_a"]["delta"], "H4_n": out["H4_B_vs_D_subset_a"]["n_tasks"],
        "sens_H1_delta": out["sensitivity_fork_failures_excluded"]["H1_B_vs_A_subset_a"]["delta"],
        "raw_H1_delta": r1["delta"],
        "B_pass_a": out["arms"]["B"]["a"]["pass_rate"], "A_pass_a": out["arms"]["A"]["a"]["pass_rate"],
        "A_pass1_a": out["pass_at_1"]["A_leaf0"]["a"]["rate"],
        "B_vs_A_pass1_delta": out["explore"]["B_pass_arm_vs_A_pass1_subset_a"]["delta"],
        "B_solved_a": t["B"]["a"]["share_solved"], "A_solved_a": t["A"]["a"]["share_solved"],
        "B_minus_A_rm_frac": t["B_minus_A_subset_a"]["frac_of_deadline"]["delta_B_minus_A"],
    }


@test
def end_to_end_effect_moves_outputs():
    print("    effect  D-reps  " + "  ".join(f"{k}" for k in headline(an.analyse(*_sim(0.0)))))
    for d_first in (False, True):
        lo, hi = (headline(an.analyse(*_sim(e, d_first))) for e in (0.0, 2.5))
        for e, h in ((0.0, lo), (2.5, hi)):
            print(f"    {e:>5}  {'rep0' if d_first else 'all ':>5}  " +
                  "  ".join(f"{v:.3g}" if isinstance(v, float) else str(v) for v in h.values()))
        assert hi["H1_delta"] > lo["H1_delta"] + 0.15 and hi["H1_p"] < lo["H1_p"]
        assert hi["H1_p"] <= an.ALPHA and lo["verdict"] != "WIN"
        assert hi["H4_delta"] > lo["H4_delta"] + 0.15 and hi["sens_H1_delta"] > lo["sens_H1_delta"] + 0.15
        assert hi["raw_H1_delta"] > lo["raw_H1_delta"] + 0.1
        assert hi["B_pass_a"] > lo["B_pass_a"] and hi["A_pass_a"] == lo["A_pass_a"]
        assert hi["A_pass1_a"] == lo["A_pass1_a"] and hi["B_vs_A_pass1_delta"] > lo["B_vs_A_pass1_delta"]
        assert hi["B_solved_a"] > lo["B_solved_a"] and hi["B_minus_A_rm_frac"] < lo["B_minus_A_rm_frac"]
        assert lo["H4_n"] == 15 and hi["H4_n"] == 15


def _sim(effect, d_first=False):
    rows = lg.build_rows(*sim.simulate(effect, d_first_rep_only=d_first))
    return an.select_attempts(rows), rows


@test
def cli_round_trip():
    """sim_leaves.py -> ledger.py -> analysis.py as subprocesses, same answer as in-process."""
    py = [sys.executable, "-I", "-B"]
    with tempfile.TemporaryDirectory() as d:
        leaves, plan, outs, led = (str(Path(d) / n) for n in ("leaves.jsonl", "plan.jsonl", "outs.jsonl", "led.jsonl"))
        subprocess.run(py + [str(HERE / "sim_leaves.py"), leaves, "1.0", "--d-first-rep-only",
                             "--plan-out", plan, "--outcomes-out", outs], check=True)
        with open(led, "w") as fh:
            subprocess.run(py + [str(HERE / "ledger.py"), plan, outs], check=True, stdout=fh, stderr=subprocess.DEVNULL)
        assert Path(led).read_text() == Path(leaves).read_text()
        res = subprocess.run(py + [str(HERE / "analysis.py"), leaves, "--pilot"], check=True, capture_output=True, text=True)
        out = json.loads(res.stdout)
        rows = an.read_rows(leaves)
        mine = json.loads(json.dumps(an.analyse(an.select_attempts(rows), rows, pilot=True), default=str))
        assert json.dumps(out, sort_keys=True) == json.dumps(mine, sort_keys=True)
        assert out["alpha"] == an.PILOT_ALPHA and isinstance(out["pilot_go"], bool)


def main():
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"ok    {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"analysis: {len(TESTS) - failed}/{len(TESTS)} pass")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
