"""Power for the measured set-a size (uses fork-search-1106/scripts/power.py's model)."""
import random, sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from power import power  # noqa: E402
rng = random.Random(1107)
for label, n, reps, alpha in [("pilot a 13x2", 13, 2, 0.10), ("full a 14x5", 14, 5, 0.05), ("full a 14x10", 14, 10, 0.05),
                              ("full a 20x5", 20, 5, 0.05), ("full a 25x5", 25, 5, 0.05)]:
    print(label, f"alpha={alpha}", " ".join(f"d=+{d:.2f}:{power(n, reps, d, alpha, rng):.2f}" for d in (0.0, 0.05, 0.10, 0.15)), flush=True)
