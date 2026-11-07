"""forksearch/freeze.py on synthetic job dirs. Stdlib only:
    python3 -I -B tests/test_freeze.py
"""
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FREEZE = HERE.parent / "forksearch" / "freeze.py"


def trial(job: Path, task: str, n: int, *, oracle=1.0, control=1.0, transplant=1.0, kill=1.0,
          solve=30.0, start="2026-11-07T01:00:00", setup_ok=True, exc=None, cls_extra=None,
          manifest=None):
    d = job / f"{task}__{n:04d}"
    d.mkdir(parents=True)
    t0 = f"{start}.000000Z"
    t1 = f"2026-11-07T01:{int(solve // 60):02d}:{int(solve % 60):02d}.000000Z"
    res = {
        "task_name": f"terminal-bench/{task}", "trial_name": d.name, "started_at": t0,
        "environment_setup": {"started_at": t0, "finished_at": t0 if setup_ok else None},
        "agent_execution": {"started_at": "2026-11-07T01:00:00.000000Z", "finished_at": t1},
        "verifier_result": {"rewards": {"reward": oracle}} if oracle is not None else None,
        "exception_info": {"exception_type": exc, "exception_message": "x"} if exc else None,
    }
    (d / "result.json").write_text(json.dumps(res))
    if not setup_ok:
        return
    m = manifest or {"same_as_ref": True}
    cls = {
        "oracle_verifier_completed": True, "shape": {"vcpu": 1, "disk_gb": 14}, "image_mb": 300,
        "workdir": "/app", "tar_bytes": 2 ** 20, "copy": {"sha256_ok": True},
        "outside_workdir": {"changed": 5, "top": [["/usr/local", 5]]},
        "kill": {"killed": [[1, "nginx"]] if kill == 0.0 else []},
        "manifest_control": dict(m), "manifest_transplant": dict(m),
        "replays": {"control": {"rewards": {"reward": control}}, "kill": {"rewards": {"reward": kill}},
                    "transplant": {"rewards": {"reward": transplant}}},
    }
    cls.update(cls_extra or {})
    (d / "classify.json").write_text(json.dumps(cls))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tb = tmp / "tb"
        names = ["live", "plain", "flaky", "badctl", "rot", "slow", "infra", "sock", "badman", "never"]
        for name in names:
            (tb / "tasks" / name).mkdir(parents=True)
            (tb / "tasks" / name / "task.toml").write_text(
                '[agent]\ntimeout_sec = 900.0\n[verifier]\ntimeout_sec = 900.0\n[environment]\ncpus = 1\nmemory_mb = 2048\n')
        job = tmp / "job"
        for i in (1, 2):
            trial(job, "live", i, transplant=0.0, kill=0.0)
            trial(job, "plain", i)
            trial(job, "flaky", i, transplant=0.0 if i == 1 else 1.0)
            trial(job, "badctl", i, control=0.0 if i == 2 else 1.0)
            trial(job, "rot", i, oracle=0.0 if i == 1 else 1.0)
            trial(job, "slow", i, solve=200.0 if i == 1 else 160.0)  # median exactly 180
            trial(job, "sock", i, transplant=0.0,
                  manifest={"same_as_ref": False, "diff": {"missing": 1, "sockets_missing": 1, "changed": 0, "extra": 0}})
            trial(job, "badman", i, manifest={"same_as_ref": False, "diff": {"missing": 0, "sockets_missing": 0, "changed": 2, "extra": 0}})
        # infra: one start failure, one fork error, then two good trials started later
        trial(job, "infra", 1, setup_ok=False, start="2026-11-07T00:59:00")
        trial(job, "infra", 2, cls_extra={"fork_error": "boom"}, start="2026-11-07T00:59:30")
        trial(job, "infra", 3, transplant=0.0, start="2026-11-07T01:10:00")
        trial(job, "infra", 4, transplant=0.0, start="2026-11-07T01:11:00")
        trial(job, "infra", 5, transplant=1.0, start="2026-11-07T01:12:00")  # third valid: ignored
        trial(job, "never", 1, exc="RuntimeError")
        out = tmp / "out"
        (tmp / "shapes.json").write_text("{}")
        subprocess.run([sys.executable, "-I", "-B", str(FREEZE), "--tb", str(tb), "--shapes",
                        str(tmp / "shapes.json"), "--out", str(out), str(job)],
                       capture_output=True, text=True, check=True)
        rows = {row["task"]: row for row in csv.DictReader((out / "tasks.csv").open())}
        got = {k: (v["set"], v["reason"]) for k, v in rows.items()}
        expect = {
            "live": ("a", "T1"), "plain": ("b", ""), "flaky": ("excluded", "T1 flaky (transplant 1/2)"),
            "badctl": ("excluded", "control 1/2"), "rot": ("excluded", "oracle 1/2"), "slow": ("a", "T2"),
            "infra": ("a", "T1"), "sock": ("a", "T1"),
            "badman": ("excluded", "workdir manifest differs after transplant"),
            "never": ("excluded", "only 0 valid trials"),
        }
        assert got == expect, json.dumps({k: (got[k], expect[k]) for k in got if got[k] != expect[k]})
        assert rows["live"]["P"] == "1" and rows["plain"]["P"] == "0" and rows["live"]["killed_procs"] == "nginx"
        assert rows["infra"]["trials"] == "infra__0003 infra__0004" and rows["infra"]["valid_trials"] == "3"
        assert "environment did not start" in rows["infra"]["invalid_trials"]
        assert rows["slow"]["solve_s_median"] == "180.0" and rows["plain"]["T2"] == "0"
        assert (out / "set_a.txt").read_text() == "infra\nlive\nslow\nsock\n"
        line = (out / "tasks.csv.sha256").read_text().split()
        import hashlib
        assert line[0] == hashlib.sha256((out / "tasks.csv").read_bytes()).hexdigest() and line[1] == "tasks.csv"
        # deterministic: a second freeze gives the same bytes
        out2 = tmp / "out2"
        subprocess.run([sys.executable, "-I", "-B", str(FREEZE), "--tb", str(tb), "--shapes",
                        str(tmp / "shapes.json"), "--out", str(out2), str(job)], capture_output=True, check=True)
        assert (out2 / "tasks.csv").read_bytes() == (out / "tasks.csv").read_bytes()
    print("freeze: all checks pass")


if __name__ == "__main__":
    main()
