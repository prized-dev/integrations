"""Power of the pre-registered primary test by simulation (no model runs).

Model (stated in PREREG.md):
- task t has a fresh single-attempt success prob p_t ~ Beta(A_, B_) (mean 0.40,
  U-shaped, like Haiku 4.5 on TB2: 38-42% pass@1, most tasks near 0 or 1)
- best-of-4 arm solves a task-replicate w.p. a_t = 1-(1-p_t)^4 (pass@4, oracle)
- fork arm: logit(b_t) = logit(a_t) + beta, beta tuned so mean(b-a) = delta
- outcomes independent given t; R replicates per task per arm
- test: one-sided paired sign-flip permutation test on per-task mean
  differences (B - A), alpha as given
Usage: python3 -I power.py
"""
import math
import random

A_, B_ = 0.35, 0.53
SIMS = 2000
FLIPS = 2000


def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def expit(x):
    return 1 / (1 + math.exp(-x))


def draw_tasks(n, rng):
    return [1 - (1 - rng.betavariate(A_, B_)) ** 4 for _ in range(n)]


def beta_for(delta, rng):
    # tune beta on a large task sample so the population mean difference = delta
    tasks = draw_tasks(20000, rng)
    lo, hi = -5.0, 5.0
    for _ in range(50):
        mid = (lo + hi) / 2
        d = sum(expit(logit(a) + mid) - a for a in tasks) / len(tasks)
        if d < delta:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def signflip_p(d, rng):
    obs = sum(d)
    if all(x == 0 for x in d):
        return 1.0
    nz = [x for x in d if x != 0]
    ge = 0
    for _ in range(FLIPS):
        s = sum(x if rng.random() < 0.5 else -x for x in nz)
        if s >= obs - 1e-12:
            ge += 1
    return (ge + 1) / (FLIPS + 1)


def power(n, reps, delta, alpha, rng):
    beta = beta_for(delta, rng) if delta != 0 else 0.0
    hits = 0
    for _ in range(SIMS):
        tasks = draw_tasks(n, rng)
        d = []
        for a in tasks:
            b = expit(logit(a) + beta) if beta else a
            ya = sum(rng.random() < a for _ in range(reps)) / reps
            yb = sum(rng.random() < b for _ in range(reps)) / reps
            d.append(yb - ya)
        if signflip_p(d, rng) <= alpha:
            hits += 1
    return hits / SIMS


if __name__ == "__main__":
    rng = random.Random(1106)
    m = A_ / (A_ + B_)
    tasks = draw_tasks(20000, rng)
    print(f"p1 mean={m:.3f}  pass@4 mean={sum(tasks)/len(tasks):.3f}  "
          f"share of tasks with 0.1<a<0.9: {sum(0.1 < a < 0.9 for a in tasks)/len(tasks):.2f}")
    designs = [
        ("pilot subset (a)", 15, 2, 0.10),
        ("pilot all", 25, 2, 0.10),
        ("full subset (a), 20 tasks x5", 20, 5, 0.05),
        ("full subset (a), 30 tasks x5", 30, 5, 0.05),
        ("full all, 80 tasks x3", 80, 3, 0.05),
        ("full all, 80 tasks x5", 80, 5, 0.05),
    ]
    for name, n, reps, alpha in designs:
        row = [f"{name:32s} alpha={alpha:.2f}"]
        for delta in (0.0, 0.05, 0.10, 0.15, 0.20):
            row.append(f"d={delta:+.2f}:{power(n, reps, delta, alpha, rng):.2f}")
        print("  ".join(row), flush=True)
