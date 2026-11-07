"""Operating characteristics for the real set-a sizes (pilot 13x2, full 14x5 and 14x10).
Same model as scripts/power.py. WIN = one-sided sign-flip p <= alpha and dhat >= 0.05 (accuracy alone);
NEGATIVE = 90% bootstrap upper bound < 0.05. 400 simulations per cell, so each rate is +-2-3 points."""
import math, random
A_, B_ = 0.35, 0.53
def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6); return math.log(p / (1 - p))
def expit(x): return 1 / (1 + math.exp(-x))
def draw(n, rng): return [1 - (1 - rng.betavariate(A_, B_)) ** 4 for _ in range(n)]
def beta_for(delta, rng):
    t = draw(20000, rng); lo, hi = -5.0, 5.0
    for _ in range(50):
        mid = (lo + hi) / 2
        d = sum(expit(logit(a) + mid) - a for a in t) / len(t)
        lo, hi = (mid, hi) if d < delta else (lo, mid)
    return (lo + hi) / 2
def sf_exact(d):
    import itertools
    nz = [x for x in d if x != 0]; obs = sum(d)
    if not nz: return 1.0
    if len(nz) <= 14:
        ge = tot = 0
        for s in itertools.product((1, -1), repeat=len(nz)):
            tot += 1; ge += sum(a * b for a, b in zip(s, nz)) >= obs - 1e-12
        return ge / tot
    return None
def boot_hi(d, rng, n=1000):
    k = len(d); m = sorted(sum(rng.choice(d) for _ in range(k)) / k for _ in range(n)); return m[int(.95 * n) - 1]
def sim(n, reps, delta, alpha, sims, rng, verdicts=True):
    beta = beta_for(delta, rng) if delta else 0.0
    go = win = neg = 0
    for _ in range(sims):
        d = []
        for a in draw(n, rng):
            b = expit(logit(a) + beta) if beta else a
            d.append(sum(rng.random() < b for _ in range(reps)) / reps - sum(rng.random() < a for _ in range(reps)) / reps)
        m = sum(d) / n
        go += m > 0
        if verdicts:
            p = sf_exact(d)
            win += (p <= alpha and m >= 0.05)
            neg += boot_hi(d, rng) < 0.05
    return go / sims, win / sims, neg / sims
rng = random.Random(1107)
print("pilot 13x2 go-rule P(dhat>0):", flush=True)
for dl in (0.0, 0.05, 0.10):
    print(f"  delta={dl:+.2f} go={sim(13, 2, dl, 0.10, 4000, rng, verdicts=False)[0]:.2f}", flush=True)
for n, reps in ((14, 5), (14, 10)):
    print(f"full {n} a-tasks x{reps}, alpha .05: go, WIN, NEGATIVE, INCONCLUSIVE", flush=True)
    for dl in (0.0, 0.05, 0.10):
        g, w, ng = sim(n, reps, dl, 0.05, 400, rng)
        print(f"  delta={dl:+.2f} WIN={w:.2f} NEGATIVE={ng:.2f} INCONCLUSIVE={1-w-ng:.2f}", flush=True)
