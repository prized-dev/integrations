# Judge by fork

Grade a running coding agent with hidden tests it never sees, and branch its run at any step.

To grade, `judge.py` forks the agent's live [Prized sandbox](https://prized.dev/docs/sandboxes?via=judge-by-fork), memory and running processes included, and runs the tests in the copy. The agent never sees the tests, and its own run carries on, held up for a fraction of a second per fork. Do that every few seconds and you get a score curve over the whole trajectory, not just one pass or fail at the end.

## Run it

```bash
pip install prized==0.1.2
export PRIZED_API_KEY=dcp_...
python judge.py --try true --try "git revert --no-edit HEAD"
```

Make the key at [prized.dev/dashboard/workspace/api-keys](https://prized.dev/dashboard/workspace/api-keys?via=judge-by-fork) with **Full access**, or **Custom** with **Sandboxes: Write**. Python 3.10 or later. A run takes about 2 minutes and well under a cent of sandbox time. Every sandbox it makes carries a per-run label and is deleted at the end, even after an error.

## What it does

1. **Starts the agent.** One 1 vCPU / 2 GB sandbox runs `agent.py`, a stand-in coding agent with no model in it. It edits `repo/roman.py` every 10 seconds, the way an agent's edit tool does, and commits each step. Step 5 is a refactor that breaks things.
2. **Judges by fork.** Every 5 seconds the judge forks the sandbox, freezes the agent's copy between two of its steps (it takes the agent's step lock, then sends `SIGSTOP` to its process group), pipes `hidden_test.py` into the copy on stdin, records the score in `scores.jsonl`, and deletes the copy. The hidden tests are never written to the agent's sandbox: each judgment sends them to a throwaway copy.
3. **Checks the agent was left alone.** At the end it searches the agent's sandbox for the tests' sentinel string (a sanity check: the judge only ever sends the tests to copies), and reads how long the agent's own 50 ms heartbeat ever stalled.
4. **Branches at the deepest drop** (`--try`, optional). The judge keeps the copy taken where the score fell furthest below its best so far. It forks that copy once per command in one call, runs each command there, resumes the frozen agent (`SIGCONT`) so it carries on from the same step, and scores each copy when the agent finishes. `--try true` is the control: it changes nothing.

## What a run looks like

Real output, Nov 5 2026:

```text
$ python judge.py --try true --try "git revert --no-edit HEAD"
   0.0s   0/10            fork 0.8s  tests 0.1s  start: stubs
   5.0s   0/10            fork 1.4s  tests 0.3s  start: stubs
  10.0s   2/10 ##         fork 1.4s  tests 0.4s  to_roman: repeat I
  15.0s   2/10 ##         fork 1.4s  tests 0.4s  to_roman: repeat I
  20.0s   3/10 ###        fork 1.4s  tests 0.5s  to_roman: greedy over a table
  25.0s   3/10 ###        fork 1.4s  tests 0.5s  to_roman: greedy over a table
  30.0s   6/10 ######     fork 1.4s  tests 0.4s  to_roman: subtractive pairs
  35.0s   6/10 ######     fork 1.4s  tests 0.5s  to_roman: subtractive pairs
  40.0s   8/10 ########   fork 1.4s  tests 0.4s  from_roman: add up the symbols
  45.0s   8/10 ########   fork 1.4s  tests 0.4s  from_roman: add up the symbols
  50.0s   4/10 ####       fork 1.6s  tests 0.4s  refactor: keep NUMERALS sorted
  55.0s   4/10 ####       fork 1.4s  tests 0.4s  refactor: keep NUMERALS sorted
  60.0s   4/10 ####       fork 1.4s  tests 0.4s  docs: docstrings
  65.0s   4/10 ####       fork 1.4s  tests 0.5s  docs: docstrings
  70.0s   6/10 ######     fork 1.4s  tests 0.5s  from_roman: subtractive pairs
agent's own sandbox: 0 files with the tests' sentinel; heartbeat gaps over 0.1 s: 14, longest 0.23s; its run ended at 6/10
branch at 50.0s (refactor: keep NUMERALS sorted, 4/10): 2 copies in 1.4s; each runs one command, then the agent carries on
    try   6/10 ######     true
    try  10/10 ########## git revert --no-edit HEAD
sandbox time: about $0.0029 at list price
```

## Numbers

From 6 runs on Nov 5, 2026 (90 judgments):

| | Result |
| --- | --- |
| Fork, until the copy is running and takes commands | 0.85-1.03 s for each run's first fork; 1.38 s p50 and 1.58 s p95 after that (84 forks, 1 vCPU / 2 GB) |
| Hidden tests in the copy, freeze included | 0.42 s p50, 0.53 s max |
| Agent held up by a fork (gaps over 0.1 s in its 50 ms heartbeat) | gaps of 0.10-0.31 s, so held up about 0.05-0.26 s |
| Hidden-test files found in the agent's own sandbox | 0 in every run |
| Branch: 2-4 copies of one moment in one `fork(count=N)` call | 1.4-2.0 s |
| Control branch (`--try true`) | 6/10 in every run, the same as the original run |
| `git revert --no-edit HEAD` at the deepest drop, then the agent carries on | 10/10 in 3 runs. A 4th, before the agent took a lock around each step, froze git mid-commit and the revert failed |
| Sandbox time per run (from Prized's records, list price) | $0.003-0.004 |

The score curve was the same in every run: 0, 2, 3, 6, 8, then 4 after the refactor, ending at 6. With `--try "rm roman.py"` the copy's agent crashes, the judge notices it's gone and scores that copy 0/10. A failing command is reported with its exit code.

## Use it with your own agent

- Replace `agent.py` with your agent. The judge needs three things from it: a pid file for a process group, so the copy's agent can be frozen; a lock it holds while it changes files, so it's frozen between steps and never halfway through one; and a working directory to test.
- Replace `hidden_test.py` with your tests. They run in the copy, so they see exactly the files, processes and packages the agent had at that moment. The judge reads the line they print that starts with `@score`, JSON with `passed` and `total`.
- Judge on a timer, as here, or on events: after each tool call, before each commit, when the agent says it's done.
- `--try` takes any shell command: a hint file, a revert, a different model's next step. Each runs in its own copy of the same moment.

## Limits

- A fork holds the agent up briefly: about 0.05-0.26 s per fork here, for a 2 GB sandbox. In our load test on a 4 GB sandbox rewriting 32 MB of memory a second, the longest pause was 0.24 to 1.04 s at any number of concurrent forks.
- Copies start on the same host as the original, so how many can be live at once depends on that host's free room. In our load test, one `fork(count=35)` call of a 2 vCPU sandbox had all 35 running in about 13 s. One `fork(count=N)` call is much faster than N separate calls, which queue: 25 separate forks at once took 23 s at p95, one call of 25 took 6-6.5 s.
- A copy holds everything the agent had, including any key in its environment or memory. The judge runs only your tests there and deletes it, but keep that in mind before you hand a copy to anyone else.
- The stand-in agent is deterministic, so the control branch reproduces the original run exactly. With a real model, each branch is one sample.
- The heartbeat gaps are measured with the sandbox's own clock.
- The copy's agent runs on for a moment after the fork, until the freeze reaches it (about one command's round trip), so the state scored is a fraction of a second later than the fork. A row's `t` is when the judge asked for the fork.
- `fork_s` is the SDK call until the copy is running and takes commands, which includes the SDK polling for it, not the fork alone.
- The judge freezes the agent's process group in the copy (`agent.py` makes itself a group leader). An agent that starts processes in other groups or sessions needs those frozen too, for example with a cgroup freezer.

## Files

- `judge.py`: the judge, about 85 lines of code.
- `agent.py`: the stand-in agent. It runs inside the sandbox.
- `hidden_test.py`: the hidden tests. The judge sends them only to copies.
- `test_offline.py`: checks, with no key and no network, that the stand-in agent's steps give the score curve above (`python test_offline.py`).
