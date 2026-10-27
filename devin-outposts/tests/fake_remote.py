#!/usr/bin/env python3
"""A stand-in for Cognition's devin-remote: `fake_remote.py serve`.

It does what the real one does at the edges the orchestrator sees: needs the three
DEVIN_OUTPOST_* variables, writes Devin's stamp files, attaches to the gateway with its connect
token, and exits 0 when the gateway says the session slept or ended. It also leaves a background
"dev server" running (a counter), the kind of process a paused sandbox keeps and a disk snapshot
loses.

Gateway modes:
- http(s)://...: the test's fake gateway (POST /attach, GET /wait).
- file://: no network (a real Prized sandbox can't reach the test machine). The remote writes
  sha256(token) to $DEVIN_REMOTE_STATE_DIR/attached and waits for the test to write "sleep" or
  "terminate" to $DEVIN_REMOTE_STATE_DIR/gateway-action.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request


def main() -> int:
    if sys.argv[1:] != ["serve"]:
        print("usage: devin-remote serve", file=sys.stderr)
        return 2
    try:
        gateway = os.environ["DEVIN_OUTPOST_GATEWAY_URL"]
        token = os.environ["DEVIN_OUTPOST_CONNECT_TOKEN"]
        sid = os.environ["DEVIN_OUTPOST_SESSION_ID"]
    except KeyError as err:
        print(f"missing {err}", file=sys.stderr)
        return 3
    state = os.environ.get("DEVIN_REMOTE_STATE_DIR") or "/opt/.devin"
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "env-keys.json"), "w") as fh:
        json.dump(sorted(os.environ), fh)
    with open(os.path.join(state, "launch.json"), "w") as fh:
        json.dump({"cwd": os.getcwd(), "uid": os.getuid()}, fh)
    os.makedirs(".devin-outpost-stamps", exist_ok=True)
    open(os.path.join(".devin-outpost-stamps", sid), "a").close()
    open(os.path.join(state, "outpost_stamp"), "a").close()
    launches_path = os.path.join(state, "launches")
    launches = int(open(launches_path).read()) + 1 if os.path.exists(launches_path) else 1
    with open(launches_path, "w") as fh:
        fh.write(str(launches))

    counter = os.path.join(state, "devserver.count")
    pid_path = os.path.join(state, "devserver.pid")
    alive = False
    if os.path.exists(pid_path):
        try:
            os.kill(int(open(pid_path).read()), 0)
            alive = True
        except (OSError, ValueError):
            alive = False
    if not alive:
        loop = (
            "import time,sys\nn=0\n"
            "while True:\n n+=1\n open(sys.argv[1],'w').write(str(n))\n time.sleep(0.2)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", loop, counter],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(pid_path, "w") as fh:
            fh.write(str(proc.pid))

    if gateway.startswith("file://"):
        with open(os.path.join(state, "attached"), "w") as fh:
            fh.write(hashlib.sha256(token.encode()).hexdigest())
        action_path = os.path.join(state, "gateway-action")
        while True:
            if os.path.exists(action_path):
                action = open(action_path).read().strip()
                os.remove(action_path)
                if action in ("sleep", "terminate"):
                    return 0
            time.sleep(0.2)

    req = urllib.request.Request(
        f"{gateway}/attach",
        data=json.dumps({"session_id": sid}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as err:  # a refused token: exit non-zero, as a remote that can't connect would
        print(f"attach failed: {err}", file=sys.stderr)
        return 4
    while True:
        try:
            with urllib.request.urlopen(f"{gateway}/wait?session_id={sid}", timeout=10) as resp:
                action = json.loads(resp.read()).get("action")
        except Exception:
            time.sleep(0.5)
            continue
        if action in ("sleep", "terminate"):
            return 0


if __name__ == "__main__":
    sys.exit(main())
