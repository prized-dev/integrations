# Hidden tests. They are never written to the agent's sandbox: each judgment
# sends them on stdin to a throwaway fork, after freezing the agent's copy there.
# Sentinel for the check at the end: HIDDEN-7f3a
import json
import signal

CASES = [("to_roman", 1, "I"), ("to_roman", 3, "III"), ("to_roman", 4, "IV"), ("to_roman", 9, "IX"),
         ("to_roman", 58, "LVIII"), ("to_roman", 1994, "MCMXCIV"), ("from_roman", "III", 3),
         ("from_roman", "LVIII", 58), ("from_roman", "IV", 4), ("from_roman", "MCMXCIV", 1994)]
try:
    import roman
except Exception:  # code that doesn't import fails every case
    roman = None


def timeout(*_):
    raise TimeoutError


signal.signal(signal.SIGALRM, timeout)
failed = []
for fn, arg, want in CASES:
    signal.alarm(5)  # an infinite loop fails its case instead of hanging the judge
    try:
        ok = getattr(roman, fn)(arg) == want
    except Exception:
        ok = False
    signal.alarm(0)
    if not ok:
        failed.append(f"{fn}({arg!r})")
print("@score", json.dumps({"passed": len(CASES) - len(failed), "total": len(CASES), "failed": failed}))
