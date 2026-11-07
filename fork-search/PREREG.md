# Fork search: pre-registration (v2, 2026-11-07)

Nothing with a model has run. This file is frozen once it, `APPENDIX.md` (the detail), `prereg/` and `tasks.csv` are public under `prized-dev/integrations/fork-search`; every result quotes that commit.

**Prior work** (APPENDIX §A). In [Daytona](https://arxiv.org/abs/2610.13841)'s study, fresh retries beat snapshot branching except where state sat outside git.

## 1. Hypotheses
- **H1 (primary).** On tasks whose state lives outside the working directory (set a), arm B beats arm A at equal budget, deadline and concurrency. B forks the live computer at a trigger into 4 children that keep the parent's model context; A is best-of-4 fresh attempts. H1 fails if the full run's δ = pass(B) − pass(A) ≤ 0, or if the one-sided test does not reject at α = 0.05.
- **H2.** The same on all tasks.
- **H3, Daytona's confound.** B beats C on set a. C's children start a fresh context on the forked machine.
- **H4, the computer itself.** B beats D on set a. D keeps the context and the workdir files, not the live machine.
- **Predictions.** B − A is +5 to +10 points on set a and about 0 on set b. B beats C on both sets, and beats D on set a only.
- **Verdicts and post titles** (all end "(pre-registered)").
  - **WIN:** H1 rejects, δ̂ ≥ +5 points, and B spends no more than A on average. "Forking a live computer beat best-of-4 restarts on Terminal-Bench".
  - **NEGATIVE:** the 90% CI's upper bound is below +5 points. "... did not beat ..." if H1 did not reject; "... beat best-of-4 restarts on Terminal-Bench, but by less than 5 points" if it did.
  - Otherwise **INCONCLUSIVE**.

## 2. Tasks, fixed before any agent runs
- **Source.** [Terminal-Bench 2.1](https://github.com/harbor-framework/terminal-bench-2-1), 89 tasks, pinned at `12b7565`.
- **Classification, model-free (11-07).** Reference solutions only, 2 trials per task, on harbor-prized.
  - **Eligible:** the oracle passes 2/2.
  - **T1:** the verifier fails 2/2 after only the workdir of the oracle's final state is copied into a fresh sandbox. 1/2 is flaky and excluded.
  - **Control for T1:** the same tar, restored into a fork of the oracle's sandbox, must pass 2/2 with an identical workdir manifest, or the task is excluded.
  - **T2:** the oracle's solve step takes at least 180 s (median of 2).
  - **Stratum P:** the verifier fails once the trial's processes are killed.
  - **Set a** is T1 or T2. **Set b** is every other eligible task.
- **Result, frozen.** 80 of 89 are eligible. Set a has 14 tasks (T1 12, of them P 5; T2 5), set b 66, and 9 are excluded. Every control passed and no task was flaky.
  - `tasks.csv` sha256 `063fa56ab754c98f8aeda7643fdb0a4d6d4601a92487ab62ec6896a65af66d29`
  - `set_a.txt` sha256 `572bfc2030774baa8011a31a9c872aed572d217ae7a3b284a93bd87171b7cac3`
  - Made by `python3 -I -B forksearch/freeze.py --tb tb21 --shapes classify/shapes.json --out classify runs/classify/classify runs/rerun1/rerun1 runs/rerun2/rerun2 runs/rerun3/rerun3` at `fork-search` commit `578d2f5`. Its rules were committed before any result was read (`87c2b29`). APPENDIX §B has the jobs.
- **Pilot draw.** Seed `063fa56a`, the hash's first 8 hex digits. It takes all 13 set-a tasks with agent timeouts of 1 h or less (we had planned 15), Daytona's 4 runnable tasks (all in set b), and random set-b tasks up to 25.
- **Full run.** Every eligible task, fresh replicates; pilot data are never pooled.

## 3. Arms at matched budget

| Arm | 4 leaves per task-replicate, all graded |
|---|---|
| **A** best-of-4 | 4 fresh attempts in parallel, B/4 each |
| **B** fork + context | One parent, forked at its first trigger. 4 children keep its full context |
| **C** fork + fresh context | Same parent and fork point. Children start a new context (Daytona's design) |
| **D** files + context | Same parent. 4 fresh sandboxes get the workdir files and the parent's context. Every replicate, the pilot's included |

- **Same in every arm.** Claude Haiku 4.5 without extended thinking (temperature logged). Harbor 0.25.0's Terminus 2 with its caching; our subclass only adds the trigger, the nudge and state export. Budget B = $1.50 of tokens at list price. Deadline T = the task's agent timeout from the arm's start, fork included. At most 4 lineages at once, with all arms started together.
- **Trigger** (`prereg/detector.py`; `prereg/atif_adapter.py` defines an episode). It fires after an episode on the first of:
  - **claim:** the first episode in which the model sets task_complete;
  - **hard:** from episode 4, at least 3 of the last 4 episodes printed an error (23 patterns, matched per line; echoed commands, string literals and printed metrics are ignored);
  - **cutoff:** 0.5 T has passed, or $0.375 has been spent.
- **Amendment 11-07, before any pilot outcome.** The hard rule was 2 of the last 3. On public Terminus 2 trajectories, 18 of its 20 hand-checked fires were real errors, but only 4 were a stuck agent. 3 of 4 fires half as often and kept all 4 stuck cases (APPENDIX §C). 2 of 3 is logged as exploratory.
- **Nudge.** At its first trigger every lineage, A's included, gets the same fixed nudge as its next prompt. At a claim the nudge replaces Terminus 2's confirmation question, and the pending completion is cleared, so a lineage then ends only on two consecutive claims. A and B differ only by the fork.
- **Shared parent.** B, C and D share one parent, so their comparisons are paired. Each fork arm is charged the parent's spend s, and each child gets (B − s)/4. Every fork logs its listening ports and processes.
- **A lineage stops** at confirmed completion, its budget, the deadline, or when its session dies.

## 4. Metrics and tests
- **Primary: pass@arm.** A task-replicate is solved if any leaf passes the hidden verifier, run in place. The policy never sees the hidden tests.
- **Test.** A one-sided paired sign-flip permutation test on per-task mean differences, with a 90% task-bootstrap CI. H2-H4 are tested only after H1 rejects, with Holm's correction over the three. H4 uses replicates where all four arms are valid. Everything else is exploratory (`prereg/analysis.py`).
- **Secondary.**
  - **selected@1:** the earliest leaf to confirm completion, else the one with the most episodes.
  - **pass@1** is A's leaf 0: one attempt at a quarter of the budget, not a full-budget attempt.
  - **Raw-token rescoring:** a pass counts only if its leaf ended by the moment the arm's raw tokens (parent included, cache reads and writes included) first passed 2.9M.
  - **Time to the first passing leaf,** with unsolved units at the deadline.
  - Dollars per solve, fork overhead and the trigger mix.
- **Power** (APPENDIX §D). The pilot (13 a-tasks × 2, α = 0.10) has 19% power at +10 points: a screen, not a claim. A full run on the 14 a-tasks has 35% at 5 replicates and 65% at 10.
- **Go rule.** The full run happens only if the pilot's δ̂(a) > 0.
- **Stopping.** Fixed sample size, no efficacy peeks; nobody sees pass rates by arm before the last unit ends. Early stops only for the spend cap, over 3 infra-invalid units among the first 20, or abuse.
- **Failures.** `prereg/ledger.py` writes 4 rows for every planned unit.
  - A tie counts as 0. Backoff after 429s and 5xx comes off the clock.
  - A sandbox or harness failure reruns the task-replicate in every arm, up to twice. After that it is dropped from all arms and listed.
  - A parent that dies before its fork makes the unit invalid. It is rerun once, then scored invalid.
  - A fork failure is ours: one retry, then 0. If only some children fail, those leaves score 0.
  - A verifier crash reruns once. A second crash scores the leaf 0.
  - **Sensitivities** for H1, H3 and H4 drop every task-replicate with a fork failure, and separately every task with an invalid unit in a fork arm. Invalid units are reported by arm.

## 5. Published win or lose
Before any run: this file, `APPENDIX.md`, `prereg/`, `tasks.csv` and the harness commit. Within 7 days of the last unit: every trajectory, the token and cost ledger, every verifier output, fork logs, the analysis output, every exclusion and any dated amendment. Then a prized.dev post and Hudson on X, titled as in §1.

## 6. Cost
The pilot (25 tasks × 2, D in both replicates) is about $237 expected, with a hard stop at $290. The full run is about $1.6k, capped at $2.5k, and is sized after the pilot (APPENDIX §E).

## 7. Threats
Context, not the computer: H3 and H4. Cached dollars flatter shared prefixes: raw-token rescoring and public ledgers. A picked subset: T1 and T2 are model-free and frozen first. A tuned trigger: fixed before any pilot run. pass@4 needs an oracle: selected@1. A weak baseline: A gets the nudge too. One old model: Haiku 5.5 replicates it. Hidden exclusions: fork failures count against B. Fork side effects (new IP, dropped connections, lazy memory) count against B too.
