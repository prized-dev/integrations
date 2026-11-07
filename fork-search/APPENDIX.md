# Fork search: appendix to PREREG.md (v2, 2026-11-07)

The detail behind `PREREG.md`. Where the two differ, `PREREG.md` rules.

## A. Prior work
- **[Daytona](https://arxiv.org/abs/2610.13841)** ran Claude Haiku 4.5 on Terminal-Bench 2.0 through Harbor: 5 tasks × 5 seeds, 1M tokens a seed.
  - Retrying from scratch (3 attempts) solved 17/25.
  - Branching from snapshots solved 6/25. That arm had 1 root and up to 2 children, and every child started with a fresh model context.
  - Branching recovered 67% of failed roots on extract-elf, where retrying recovered 38%.
  - On a custom task whose progress sat in a SQLite database outside git, started from a planted checkpoint, it solved 4/5 from the snapshot, 0/5 from a git diff and 0/5 from a clean start.
  - Threats to it: 1 model, 5 tasks, and no best-of-N judging.
- **[ParallelEnv](https://dl.acm.org/doi/10.1145/3786335.3813189)** reports 48% on SWE-bench Pro at $0.50, +15 points at matched compute, but against a single path, not against best-of-k.
- **[Vals](https://www.vals.ai/benchmarks/terminal-bench-2)** measured Haiku 4.5 (Thinking) with Terminus 2 on TB 2.0 at 38.20% single-attempt, $0.33 and 728k tokens per test. We run without thinking, so our cost per attempt will differ. 2.9M raw tokens is 4 × that mean.

## B. Classification run (11-07, model-free, $2.92)
- **Code.** `prized-dev/integrations`, branch `agent/fork-search` (local only, not yet pushed), directory `fork-search/`.
  - `forksearch/classify_env.py` is a harbor-prized environment for Harbor's oracle agent. The oracle runs in sandbox A, and Harbor's reward there is the oracle's own verdict.
  - When the verifier starts, A is forked twice: C (control) and K (kill). A fresh sandbox B of the same task was started alongside A.
  - In C, the workdir is listed, packed with tar and restored in place. B gets that tar on an emptied workdir, and in K every process the trial left running is killed.
  - The verifier's exact steps are replayed on C, K and B.
  - It ran on Harbor 0.25.0 and harbor-prized 0.4.0 at `1b3ec83`, inside a keyless Prized sandbox; the key stayed on agent-xp, behind a broker.
- **Jobs.** `-p /home/prized/tb21/tasks` is the TB 2.1 clone at `12b7565`. `(same)` means the classify job's agent, environment, shapes and labels.

  ```
  broker.py run classify -- -p /home/prized/tb21/tasks -a oracle -e forksearch.classify_env:ClassifyEnvironment -k 2 -n 4 \
    --ek shapes=/home/prized/fork-search/classify/shapes.json --ek labels=agent=agent-xp,task=fork-search-1107,run=classify -y
  broker.py run rerun2 -- (same) -i <classify/need2.txt> -k 2 -n 3 --ek mirror=mirror.gcr.io
  broker.py run rerun1 -- (same) -i <classify/need1.txt> -k 1 -n 2 --ek mirror=mirror.gcr.io
  broker.py run rerun3 -- (same) -i mteb-leaderboard -k 2 -n 2 --ek mirror=mirror.gcr.io
  python3 -I -B forksearch/freeze.py --tb tb21 --shapes classify/shapes.json --out classify \
    runs/classify/classify runs/rerun1/rerun1 runs/rerun2/rerun2 runs/rerun3/rerun3
  ```

  | Job | Code (classify_env.py, shapes.json sha256) | Trials | Note |
  |---|---|---|---|
  | classify | `87c2b29` (d556c48a, cc8339e8) | 178 | Docker Hub by tag |
  | rerun1, rerun2 | `fad8cae` (514a4c87, 4abdec02) | 46 + 86 | mirror.gcr.io by digest |
  | rerun3 | `e140040` (514a4c87, b3b1eb58) | 2 | 20 GB headroom |
  | freeze | `e140040` (freeze.py d10a30a1), outputs at `578d2f5` | | |

- **Deviations, all fixed before freezing.**
  - Docker Hub's anonymous limit (100 pulls an hour per IP) stopped the first job after about 45 tasks: every sandbox start makes 2 manifest GETs. All 89 images are on mirror.gcr.io under the digest their Docker Hub tag had, and the reruns pulled from there by that digest. 42 tasks had one trial from each source, and all 42 agreed.
  - mteb-leaderboard's 3.2 GB workdir tar filled its copies' disks (an infra-invalid trial). It was rerun with 20 GB of headroom.
- **Rules (`freeze.py`, committed before any result was read).** The first two valid trials of each task by start time count. A trial is invalid if its environment never started, Harbor recorded an exception other than an agent or verifier timeout, or classification hit an infrastructure error. Agent and verifier timeouts are results. A replay that ran past the task's verifier timeout counts as a fail.
- **Result.**
  - **Excluded (oracle 0/2):** build-cython-ext, mcmc-sampling-stan, protein-assembly, qemu-alpine-ssh and qemu-startup (task rot), configure-git-webserver, git-multibranch and mailman (they need a real user switch), and torch-tensor-parallelism (its verifier timed out at 900 s even with 4 vCPUs). These are the same 9 that failed in the single trial of 11-03.
  - **T1 and P (a live process the tests talk to):** hf-model-inference (Flask with a model loaded), install-windows-3.11 (QEMU), kv-store-grpc, nginx-request-logging and pypi-server.
  - **T1 only (installed state outside the workdir):** adaptive-rejection-sampler, build-pmars, build-pov-ray, caffe-cifar-10, compile-compcert, largest-eigenval and sqlite-with-gcov. caffe-cifar-10 and compile-compcert are T2 too, and install-windows-3.11 also has T2.
  - **T2 only:** crack-7z-hash (a 1,444 s solve) and rstan-to-pystan (208 s).
  - Daytona's 4 runnable tasks (extract-elf, custom-memory-heap-crash, code-from-image, large-scale-text-editing) are all in set b.
  - Every control passed 2/2, and every workdir manifest (type, mode, size, sha256, link target) matched in every trial.
  - We had expected 15-30. A static preview had flagged 15 tasks; 8 hold T1, and 4 tasks it missed hold T1 too.
- **The per-task table** (`tasks.csv`) has oracle, control, transplant and kill results, solve times, measured unpacked image MB, image digest, workdir tar MB, the top directories changed outside the workdir, and the processes killed.

## C. Trigger: replay and amendment (11-07, before any pilot run)
- **Data.** 5,981 public Terminus 2 trajectories from 14 models, covering passed and failed trials on all 89 tasks:
  - TB 2.1 leaderboard trajectories from the Harbor Hub;
  - TB 2.0 leaderboard trajectories from Hugging Face `harborframework/terminal-bench-2-leaderboard`;
  - 258 Haiku 4.5 trials from `DCAgent/eval-terminal-bench-2.0-claude-haiku-4-5-20251001-20260115_165217`.
- **Detector changes:** per-line matching, benign-line filters (printed metrics, broken pipes, code on screen, printf templates, string literals) and 7 missed error forms. Each has a regression test from a real snippet.
- **First trigger:**

  | Hard rule | All: claim / hard / cutoff | Haiku 4.5: claim / hard / cutoff |
  |---|---|---|
  | 2 of the last 3 (registered first) | 46 / 33 / 21% | 43 / 41 / 16% |
  | 3 of the last 4 (primary from 11-07) | 53 / 17 / 29% | 53 / 25 / 22% |

  The 2-of-3 rule fired at a median of episode 5 (about 11% of T), and the 3-of-4 rule at episode 6.
- **Why the rule changed.** In a hand-check of 20 random 2-of-3 fires, 18 were real errors, but only 4 were an agent stuck on a step. The rest were routine setup errors that the agent fixed within 1-2 episodes. The 3-of-4 rule kept all 4 stuck cases and dropped 8 of the 14 routine fires and both false positives. That sample is small and was also used to label the cases; the rule was chosen before any pilot outcome.
- **Claim semantics.** Terminus 2 answers a first `task_complete` with a confirmation question and ends the run on the next claim (`terminus_2.py:716-732, 1619-1631, 1745-1748`).
  - Without a change, a nudged lineage could confirm and stop at once. So at every first trigger, in every arm, the harness sends `detector.after_trigger()`: the terminal state plus the nudge, never the question. It also clears the pending completion.
  - Arm C's children get `FRESH_NOTE` with Terminus 2's initial prompt before it, and arm D's get `FILES_NOTE`.
- **Hashes** are in `SHA256SUMS`.

## D. Power and operating characteristics
- Model (`scripts/power.py`, `scripts/power_seta.py`): per-task single-attempt rates ~ Beta(0.35, 0.53) with mean 0.40; A is pass@4, and B is a logit shift tuned to the target δ.

  | Design | α | δ +0.05 | +0.10 | +0.15 |
  |---|---|---|---|---|
  | Pilot, 13 a-tasks × 2 | 0.10 | 7% | 19% | 36% |
  | Full, 14 a-tasks × 5 | 0.05 | 10% | 35% | 64% |
  | Full, 14 a-tasks × 10 | 0.05 | 23% | 65% | 87% |
  | Full, 20 a-tasks × 5 | 0.05 | 20% | 60% | 88% |
  | Full, 25 a-tasks × 5 | 0.05 | 26% | 71% | 95% |
  | All 80 tasks × 3 | 0.05 | 54% | 98% | 100% |

- Operating characteristics at the planned 25 a-tasks × 5 (reviewer's simulation): at a true effect of 0, NEGATIVE 50% and INCONCLUSIVE 48%; at +5 points, WIN 24%; at +10, WIN 73%.
- The go rule (pilot δ̂(a) > 0) passes 42% of the time at a true effect of 0 and 88% at +10 points.
- H4 in the pilot has 10-18% power with D in one replicate; D now runs in both.

## E. Harness, shapes, cost
- **Reused:** harbor-prized 0.4 and its keyless broker; the fork fixups from `harbor-fork-mode` `f4e776d` and `agent/harbor-fork-verifier` `1b3ec83`; Harbor 0.25.0's Terminus 2 and its verifier; and `fork(count=8)`. On 16 django copies, every test had passed 3.1 s after the fork call.
- **Shapes:** `prized-base-v1`, with each task's vCPU and memory. Disk is 10 GB plus the measured unpacked image (median 335 MB, max 2.7 GB; mteb-leaderboard needs 20 GB). Images come from mirror.gcr.io, pinned by digest (`classify/shapes.json`).
- **Expected cost per task-replicate,** with caps in brackets: A $1.20 ($1.50), the parent $0.10, B $0.90, C $1.10 and D $1.00 ($1.50 each), and about $0.25 of sandboxes.
- **Pilot:** 25 tasks × 2 with A-D in both replicates, 200 units.
  - About $215 of tokens and $22 of sandboxes expected.
  - If every arm hit its cap, tokens would reach $300. The token spend stop at $265 and the hard stop at $290 then end the run early.
- **Full run options, decided after the pilot:**
  - 14 a-tasks × 10 replicates;
  - or add Docker-mode tasks (from 11-10) to set a: the 3 user-switch TB 2.1 tasks, and Terminal-Bench 4.0's 11 Docker Compose tasks with services. Each goes through the same classifier;
  - plus set b × 3, and a Haiku 5.5 replication.
  - About $1.6k, capped at $2.5k.
- **Side effects of a fork,** measured: it gets a new hostname and IP, outbound TCP connections drop, servers bound to 0.0.0.0 or 127.0.0.1 keep working, and memory restores lazily (a 1.5 GB re-read took 4.07 s against 0.011 s in the parent).
