"""Runs in the keyless runner: waits up to 20 s for request files, moves them
aside and prints each as one JSON line (a large one by reference)."""
import json
import os
import sys
import time

spool = sys.argv[1]
deadline = time.time() + 20
while True:
    names = sorted(n for n in os.listdir(f"{spool}/req") if n.endswith(".json"))
    if names or time.time() > deadline:
        break
    time.sleep(0.03)
for name in names:
    src, dst = f"{spool}/req/{name}", f"{spool}/taken/{name}"
    os.rename(src, dst)
    if os.path.getsize(dst) > 4_000_000:
        print(json.dumps({"ref": dst}))
    else:
        with open(dst) as f:
            print(f.read().strip())
        os.remove(dst)
