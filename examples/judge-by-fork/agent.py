"""A stand-in coding agent (no model). It runs inside the sandbox and edits
repo/roman.py one step every STEP seconds, the way an agent's edit tool does
(replace this text with that text), and commits each step. Step 5 is a
refactor that breaks things, as real agents do.

It leads its own process group and writes its pid to ~/agent/pid, so a judge can
freeze it and its children. It holds ~/agent/lock while it edits and commits, so a
judge that takes the lock first freezes it between steps, never halfway through
one. It writes ~/agent/ready once the repo has its first commit and,
when it finishes, ~/agent/done with every gap over 0.1 s in its 50 ms heartbeat
(a gap of 0.25 s means it was held up for about 0.2 s)."""
import fcntl
import json
import os
import subprocess
import sys
import time

AG, STEP = os.environ.get("AGENT_HOME", "/home/prized/agent"), float(sys.argv[1]) if len(sys.argv) > 1 else 10
REPO = os.environ.get("AGENT_REPO", "/home/prized/work/repo")
TABLE = '[(1000, "M"), (500, "D"), (100, "C"), (50, "L"), (10, "X"), (5, "V"), (1, "I")]'
FULL = ('[(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"),\n'
        '            (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]')
TO_STUB = "def to_roman(n):\n    raise NotImplementedError\n"
FROM_STUB = "def from_roman(s):\n    raise NotImplementedError\n"
GREEDY = ("NUMERALS = " + TABLE + "\n\n\ndef to_roman(n):\n    out = \"\"\n    for value, sym in NUMERALS:\n"
          "        while n >= value:\n            out += sym\n            n -= value\n    return out\n")
ADD = "    values = {sym: v for v, sym in NUMERALS if len(sym) == 1}\n    return sum(values[c] for c in s)\n"
SUB = ("    values = {sym: v for v, sym in NUMERALS if len(sym) == 1}\n    total = 0\n"
       "    for i, c in enumerate(s):\n        nxt = values[s[i + 1]] if i + 1 < len(s) else 0\n"
       "        total += -values[c] if values[c] < nxt else values[c]\n    return total\n")
REPEAT = 'def to_roman(n):\n    return "I" * n\n'
STEPS = [  # (commit message, old text, new text)
    ("to_roman: repeat I", TO_STUB, REPEAT),
    ("to_roman: greedy over a table", REPEAT, GREEDY),
    ("to_roman: subtractive pairs", TABLE, FULL),
    ("from_roman: add up the symbols", FROM_STUB, "def from_roman(s):\n" + ADD),
    ("refactor: keep NUMERALS sorted", "\n\n\ndef to_roman", "\nNUMERALS.sort()\n\n\ndef to_roman"),
    ("docs: docstrings", "def to_roman(n):\n", 'def to_roman(n):\n    """Integer to Roman numeral."""\n'),
    ("from_roman: subtractive pairs", ADD, SUB),
]


def git(*args):
    subprocess.run(["git", "-C", REPO, *args], check=True, capture_output=True)


def put(path, text):  # write whole, then rename: a reader never sees half a file
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


try:
    os.setpgid(0, 0)  # lead a process group: `kill -STOP -- -PID` then freezes git too
except OSError:
    pass  # already a session leader, so already a group leader
put(f"{AG}/pid", str(os.getpid()))
if not os.path.isdir(REPO):
    os.makedirs(REPO)
    put(f"{REPO}/roman.py", TO_STUB + "\n\n" + FROM_STUB)
    for args in (["init", "-q"], ["config", "user.name", "agent"], ["config", "user.email", "agent@example.com"],
                 ["add", "-A"], ["commit", "-qm", "start: stubs"]):
        git(*args)
put(f"{AG}/ready", "")
stalls = []
for msg, old, new in STEPS:
    last = time.monotonic()
    end = last + STEP
    while last < end:  # think, with a heartbeat
        time.sleep(0.05)
        now = time.monotonic()
        if now - last > 0.1:
            stalls.append(round(now - last, 2))
        last = now
    with open(f"{AG}/lock", "w") as lock:  # one step at a time: a judge freezes us only between steps
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(f"{REPO}/roman.py") as f:
            src = f.read()
        if old not in src:
            print("edit failed:", msg, flush=True)
            continue
        put(f"{REPO}/roman.py", src.replace(old, new, 1))
        git("commit", "-qam", msg)
    print("step:", msg, flush=True)
put(f"{AG}/done", json.dumps({"stalls": stalls}))
