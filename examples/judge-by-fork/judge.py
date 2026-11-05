"""Judge by fork: score a running agent with hidden tests, without giving it the tests.

Every --every seconds: fork the agent's live sandbox, freeze the agent's copy,
send the hidden tests into the copy on stdin, record the score, delete the copy.
The agent's own sandbox never gets the tests and keeps running (each fork holds
it up for a fraction of a second). With --try, branch the run where the score
fell furthest below its best: one copy per command; each runs its command, lets
the frozen agent carry on, and is scored when the agent finishes.
"""
import argparse
import json
import pathlib
import time
import uuid

from prized import Sandbox

HERE, AG = pathlib.Path(__file__).parent, "/home/prized/agent"  # the agent's own files, outside the repo
ap = argparse.ArgumentParser()
ap.add_argument("--every", type=float, default=5, help="seconds between judgments")
ap.add_argument("--step", type=float, default=10, help="seconds per agent step")
ap.add_argument("--try", dest="tries", action="append", default=[],
                help="a shell command to run in the repo of a copy taken at the deepest drop")
ap.add_argument("--label", action="append", default=[], help="an extra sandbox label, key=value")
a = ap.parse_args()
LABELS = {**dict(kv.split("=", 1) for kv in a.label), "example": "judge-by-fork", "run": uuid.uuid4().hex[:8]}
EOF = "HIDDEN_" + uuid.uuid4().hex
PROBE = f"""flock -w 30 {AG}/lock kill -STOP -- -$(cat {AG}/pid) 2>/dev/null || echo @gone
echo "@step $(git log -1 --format=%s)"; git diff --quiet HEAD || echo @edit; test -e {AG}/done && echo @done
python3 -B - <<'{EOF}'\n{(HERE / "hidden_test.py").read_text()}\n{EOF}"""
RATE = (0.04 + 2 * 0.0125 + 10 * 0.0001) / 3600  # list price of 1 vCPU, 2 GB and 10 GB, in $ per second
born, spent = {}, 0.0


def score(sb):  # freeze the copy's agent between steps (its whole process group), then run the hidden tests
    out = sb.exec(PROBE, cwd="repo").stdout.splitlines()
    tag = {line.split(" ", 1)[0]: line.partition(" ")[2] for line in out if line.startswith("@")}
    s = json.loads(tag.get("@score") or '{"passed": 0, "total": 0}')
    return tag.get("@step", "?") + (" (+ uncommitted edit)" if "@edit" in tag else ""), tag, s


def drop(sb):
    global spent
    spent += (time.monotonic() - born.pop(sb.id)) * RATE
    sb.delete()


try:
    agent = Sandbox.create(vcpu=1, memory_gb=2, labels=LABELS, max_runtime_min=int(30 + 8 * a.step / 60))
    born[agent.id] = time.monotonic()
    agent.files.write(f"{AG}/agent.py", (HERE / "agent.py").read_text())
    agent.exec(f"python3 {AG}/agent.py {a.step} > {AG}/log 2>&1", background=True)
    agent.exec(f"until [ -e {AG}/ready ]; do sleep 0.05; done", timeout=30)  # the repo has its first commit
    t0, best, drawdown, kept, log = time.monotonic(), 0, 0, None, open("scores.jsonl", "w", buffering=1)
    while True:
        tick = time.monotonic()
        copy = agent.fork(labels=LABELS)[0]
        born[copy.id], forked = tick, time.monotonic()
        step, tag, s = score(copy)
        row = dict(t=round(tick - t0, 1), step=step, passed=s["passed"], total=s["total"],
                   fork_s=round(forked - tick, 2), test_s=round(time.monotonic() - forked, 2))
        log.write(json.dumps(row) + "\n")
        best = max(best, s["passed"])
        print(f"{row['t']:6.1f}s {s['passed']:3}/{s['total']} {'#' * s['passed']:<10} "
              f"fork {row['fork_s']:.1f}s  tests {row['test_s']:.1f}s  {step}", flush=True)
        if a.tries and best - s["passed"] > drawdown and "@edit" not in tag:  # deepest drop so far: keep it
            drawdown, old, kept, at = best - s["passed"], kept, copy, row
            if old:
                drop(old)
        else:
            drop(copy)
        if "@done" in tag or "@gone" in tag:
            break
        time.sleep(max(0.0, a.every - (time.monotonic() - tick)))
    found, info = agent.exec(f"grep -rIl 'HIDDEN-7f3[a]' /home /tmp /etc | wc -l; cat {AG}/done").stdout.split("\n", 1)
    stalls = json.loads(info or '{"stalls": []}')["stalls"]
    print(f"agent's own sandbox: {found} files with the tests' sentinel; heartbeat gaps over 0.1 s: {len(stalls)}, "
          f"longest {max(stalls, default=0)}s; its run ended at {row['passed']}/{row['total']}")
    drop(agent)
    if a.tries and not kept:
        print("no drop in the score, so nothing to branch from")
    elif a.tries:
        t = time.monotonic()
        copies = kept.fork(count=len(a.tries), labels=LABELS)
        print(f"branch at {at['t']}s ({at['step']}, {at['passed']}/{at['total']}): {len(copies)} copies in "
              f"{time.monotonic() - t:.1f}s; each runs one command, then the agent carries on")
        drop(kept)
        for c, cmd in zip(copies, a.tries, strict=True):
            born[c.id] = t
            r = c.exec(cmd, cwd="repo", timeout=120)
            err = " ".join((r.stderr or r.stdout).split())[-70:]
            note = "" if r.exit_code == 0 else f"  (exit {r.exit_code}: {err})"
            c.exec(f"kill -CONT -- -$(cat {AG}/pid)")
            c.exec(f"while [ ! -e {AG}/done ] && kill -0 $(cat {AG}/pid) 2>/dev/null; do sleep 1; done",
                   timeout=10 * a.step + 60)
            s = score(c)[2]
            drop(c)
            result = dict(branch_at=at["t"], cmd=cmd, exit=r.exit_code, passed=s["passed"], total=s["total"])
            log.write(json.dumps(result) + "\n")
            print(f"{'try':>7} {s['passed']:3}/{s['total']} {'#' * s['passed']:<10} {cmd}{note}")
finally:
    spent += sum(time.monotonic() - b for b in born.values()) * RATE
    try:
        Sandbox.delete_by_labels(LABELS)  # everything this run made, even after an error
    finally:
        print(f"sandbox time: about ${spent:.4f} at list price")
