"""Freeze the fork-search task classes from the model-free classification run.

    python3 -I -B forksearch/freeze.py --tb TB21_DIR --shapes classify/shapes.json \\
        --out classify JOB_DIR [JOB_DIR ...]

Stdlib only; reads Harbor job dirs (result.json + classify.json per trial)
written by forksearch.classify_env, and the tasks' task.toml. Writes, in --out:
tasks.csv (one row per task), set_a.txt (sorted names), and a .sha256 next to
each. The rules below are PREREG section 2; they were written before the
classification results were read.

A trial is INFRA-INVALID (and replaced by a rerun) when its environment never
started (no environment_setup.finished_at), Harbor recorded an exception other
than an agent or verifier timeout, classify.json is missing, or
classification hit an infrastructure error: a fork error, a preparation error
(pack, copy, restore, kill), a copy whose sha256 differs, or a replay that
raised. A replay that ran past the task's verifier timeout is a FAIL, not an
infra error. An oracle that fails or times out is a valid result.

Per task, the first two valid trials by start time count:
- eligible: the oracle passes 2/2 (Harbor's reward on the oracle's sandbox).
- control: the identity transplant passes 2/2, and both workdir manifests
  (control and transplant) match the reference, apart from unix sockets, which
  tar cannot carry. Otherwise the task is excluded (transplant suspect).
- T1: the transplant fails 2/2 (passes 2/2: no; 1/2: flaky, excluded).
- T2: the oracle's solve step (agent_execution) has a median >= 180 s.
- P: the verifier fails 2/2 once the trial's processes are killed (1/2: flaky,
  reported, does not exclude).
- set a: eligible, not excluded, T1 or T2. set b: the other eligible tasks.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import tomllib
from datetime import datetime
from pathlib import Path

T2_SEC = 180.0
# Exceptions that are results (the oracle was too slow), not infrastructure.
RESULT_EXCEPTIONS = ("AgentTimeoutError", "VerifierTimeoutError")
FIELDS = [
    "task", "set", "reason", "eligible", "oracle", "control", "transplant", "T1",
    "solve_s_1", "solve_s_2", "solve_s_median", "T2", "kill", "P",
    "agent_timeout_s", "verifier_timeout_s", "cpus", "memory_mb", "vcpu_used", "disk_gb_used",
    "image_mb", "compressed_mb", "workdir", "workdir_tar_mb", "outside_changed", "outside_top",
    "killed_procs", "manifest_note", "valid_trials", "invalid_trials", "trials",
]


def _ts(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _reward(rewards) -> float | None:
    if not isinstance(rewards, dict):
        return None
    value = rewards.get("reward")
    return float(value) if value is not None else None


def _manifest_ok(m: dict | None) -> tuple[bool, str]:
    if not m:
        return False, "no manifest"
    if m.get("same_as_ref"):
        return True, ""
    d = m.get("diff") or {}
    if d and d.get("changed") == 0 and d.get("extra") == 0 and d.get("missing") == d.get("sockets_missing"):
        return True, f"{d.get('sockets_missing')} sockets not carried"
    return False, json.dumps(d)[:200] if d else (m.get("diff_error") or "differs")


def load_trial(trial_dir: Path) -> dict:
    res = json.loads((trial_dir / "result.json").read_text())
    cls_path = trial_dir / "classify.json"
    cls = json.loads(cls_path.read_text()) if cls_path.exists() else None
    task = (res.get("task_name") or "").rsplit("/", 1)[-1] or trial_dir.name.split("__")[0]
    t = {"task": task, "trial": res.get("trial_name") or trial_dir.name, "started_at": res.get("started_at") or "",
         "cls": cls, "invalid": None}
    setup = res.get("environment_setup") or {}
    exc = res.get("exception_info") or {}
    t["exception"] = exc.get("exception_type")
    ae = res.get("agent_execution") or {}
    a, b = _ts(ae.get("started_at")), _ts(ae.get("finished_at"))
    t["solve_s"] = (b - a).total_seconds() if a and b else None
    t["oracle"] = _reward((res.get("verifier_result") or {}).get("rewards"))
    if not setup.get("finished_at"):
        t["invalid"] = f"environment did not start ({t['exception']})"
        return t
    if t["exception"] and t["exception"] not in RESULT_EXCEPTIONS:
        t["invalid"] = f"{t['exception']}: {str(exc.get('exception_message'))[:120]}"
        return t
    if cls is None:
        t["invalid"] = "no classify.json"
        return t
    for key in ("fork_error", "prepare_error", "finish_error"):
        if cls.get(key):
            t["invalid"] = f"{key}: {str(cls[key])[:120]}"
            return t
    if cls.get("prepare_errors"):
        t["invalid"] = f"prepare_errors: {json.dumps(cls['prepare_errors'])[:160]}"
        return t
    replays = cls.get("replays")
    if not cls.get("oracle_verifier_completed") or not isinstance(replays, dict):
        # The oracle's verifier never finished: a valid oracle failure, nothing to compare.
        t["oracle"] = 0.0 if not t["oracle"] else t["oracle"]
        t["roles"] = {}
        return t
    if (cls.get("copy") or {}).get("sha256_ok") is not True:
        t["invalid"] = "copy sha256 mismatch"
        return t
    roles = {}
    for role in ("control", "kill", "transplant"):
        r = replays.get(role)
        if r is None:
            t["invalid"] = f"no {role} replay"
            return t
        if r.get("error"):
            t["invalid"] = f"{role} replay error: {r['error'][:120]}"
            return t
        roles[role] = 1.0 if _reward(r.get("rewards")) == 1.0 else 0.0
    t["roles"] = roles
    return t


def classify(task: str, trials: list[dict], meta: dict, shape: dict) -> dict:
    valid = sorted((t for t in trials if not t["invalid"]), key=lambda t: t["started_at"])[:2]
    row = {k: "" for k in FIELDS}
    row["task"] = task
    row["agent_timeout_s"] = meta.get("agent", {}).get("timeout_sec", "")
    row["verifier_timeout_s"] = meta.get("verifier", {}).get("timeout_sec", "")
    env = meta.get("environment", {})
    row["cpus"], row["memory_mb"] = env.get("cpus", ""), env.get("memory_mb", "")
    row["compressed_mb"] = shape.get("compressed_mb", "")
    row["valid_trials"] = len([t for t in trials if not t["invalid"]])
    row["invalid_trials"] = "; ".join(sorted({t["invalid"] for t in trials if t["invalid"]}))[:300]
    row["trials"] = " ".join(t["trial"] for t in valid)
    if len(valid) < 2:
        row["set"], row["reason"] = "excluded", f"only {len(valid)} valid trials"
        return row
    clss = [t["cls"] or {} for t in valid]
    c0 = clss[0]
    row["vcpu_used"] = (c0.get("shape") or {}).get("vcpu", "")
    row["disk_gb_used"] = (c0.get("shape") or {}).get("disk_gb", "")
    row["image_mb"] = max((c.get("image_mb") or 0) for c in clss) or ""
    row["workdir"] = c0.get("workdir", "")
    tars = [c.get("tar_bytes") for c in clss if c.get("tar_bytes") is not None]
    row["workdir_tar_mb"] = f"{max(tars) / 2**20:.1f}" if tars else ""
    outs = [c.get("outside_workdir") or {} for c in clss]
    changed = [o.get("changed") for o in outs if isinstance(o.get("changed"), int)]
    row["outside_changed"] = int(statistics.median(changed)) if changed else ""
    row["outside_top"] = " ".join(f"{k}:{v}" for k, v in (outs[0].get("top") or [])[:4])
    procs = sorted({p[1] for c in clss for p in ((c.get("kill") or {}).get("killed") or [])})
    row["killed_procs"] = " ".join(procs)[:200]
    solve = [t["solve_s"] for t in valid]
    row["solve_s_1"], row["solve_s_2"] = (f"{s:.1f}" if s is not None else "" for s in solve)
    med = statistics.median(solve) if all(s is not None for s in solve) else None
    row["solve_s_median"] = f"{med:.1f}" if med is not None else ""
    t2 = med is not None and med >= T2_SEC
    row["T2"] = int(t2)
    oracle = [1 if t["oracle"] == 1.0 else 0 for t in valid]
    row["oracle"] = f"{sum(oracle)}/2"
    row["eligible"] = int(sum(oracle) == 2)
    if sum(oracle) < 2:
        row["set"], row["reason"] = "excluded", f"oracle {sum(oracle)}/2"
        return row
    roles = [t.get("roles") or {} for t in valid]
    control = [int(r.get("control") == 1.0) for r in roles]
    transplant = [int(r.get("transplant") == 1.0) for r in roles]
    kill = [int(r.get("kill") == 1.0) for r in roles]
    row["control"], row["transplant"], row["kill"] = (f"{sum(x)}/2" for x in (control, transplant, kill))
    notes = []
    fidelity = True
    for c in clss:
        for role in ("control", "transplant"):
            ok, note = _manifest_ok(c.get(f"manifest_{role}"))
            fidelity &= ok
            if note:
                notes.append(f"{role}: {note}")
    row["manifest_note"] = "; ".join(sorted(set(notes)))[:300]
    row["P"] = {0: 1, 2: 0}.get(sum(kill), "flaky")
    if sum(control) < 2:
        row["set"], row["reason"] = "excluded", f"control {sum(control)}/2"
        return row
    if not fidelity:
        row["set"], row["reason"] = "excluded", "workdir manifest differs after transplant"
        return row
    if sum(transplant) == 1:
        row["T1"] = "flaky"
        row["set"], row["reason"] = "excluded", "T1 flaky (transplant 1/2)"
        return row
    t1 = sum(transplant) == 0
    row["T1"] = int(t1)
    row["set"] = "a" if (t1 or t2) else "b"
    row["reason"] = " ".join(x for x, on in (("T1", t1), ("T2", t2)) if on)
    return row


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tb", required=True, type=Path, help="Terminal-Bench 2.1 clone (tasks/*/task.toml)")
    ap.add_argument("--shapes", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("jobs", nargs="+", type=Path)
    args = ap.parse_args()
    shapes = json.loads(args.shapes.read_text())
    trials: dict[str, list[dict]] = {}
    for job in args.jobs:
        for d in sorted(p for p in job.iterdir() if p.is_dir() and (p / "result.json").exists()):
            t = load_trial(d)
            trials.setdefault(t["task"], []).append(t)
    tasks = sorted(p.name for p in (args.tb / "tasks").iterdir() if (p / "task.toml").exists())
    rows = []
    for task in tasks:
        meta = tomllib.loads((args.tb / "tasks" / task / "task.toml").read_text())
        rows.append(classify(task, trials.get(task, []), meta, shapes.get(task, {})))
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / "tasks.csv"
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    set_a = args.out / "set_a.txt"
    set_a.write_text("".join(f"{r['task']}\n" for r in rows if r["set"] == "a"))
    for p in (out, set_a):
        (p.parent / f"{p.name}.sha256").write_text(f"{sha256(p)}  {p.name}\n")
    n = {s: sum(1 for r in rows if r["set"] == s) for s in ("a", "b", "excluded")}
    print(f"tasks {len(rows)}  set a {n['a']}  set b {n['b']}  excluded {n['excluded']}")
    print("T1", sum(1 for r in rows if r["T1"] == 1), " T2", sum(1 for r in rows if r["T2"] == 1),
          " P", sum(1 for r in rows if r["P"] == 1))
    for r in rows:
        if r["set"] == "excluded":
            print("excluded", r["task"], "-", r["reason"])
    print("tasks.csv sha256", sha256(out))
    print("set_a.txt sha256", sha256(set_a))


if __name__ == "__main__":
    main()
