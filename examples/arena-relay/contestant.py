#!/usr/bin/env python3
"""Stand-in contestant (no model, no key): runs inside the contestant sandbox. It keeps
memory and a process tree busy like an agent, and serves its state on :8080/api/state, so
a viewer can tell a copy is a live fork (same pid, ticks still counting). Usage: contestant.py MEM_MB"""
import json, os, random, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
MEM_MB = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
START = time.time(); ticks = 0; dirty_rounds = 0
# Memory: MEM_MB touched once, then ~32 MB re-dirtied every second (an agent's heap churns).
buf = bytearray(MEM_MB * 1024 * 1024)
for i in range(0, len(buf), 4096): buf[i] = 1
def ticker():
    global ticks
    while True:
        time.sleep(0.05); ticks += 1
def churn():
    global dirty_rounds
    n = len(buf); chunk = 32 * 1024 * 1024
    while True:
        off = random.randrange(0, max(1, n - chunk)); v = dirty_rounds & 0xFF
        for i in range(off, off + chunk, 4096): buf[i] = v
        dirty_rounds += 1; time.sleep(1)
threading.Thread(target=ticker, daemon=True).start()
threading.Thread(target=churn, daemon=True).start()
# Busy children: a ~50%-duty CPU worker and a few sleepers (a process tree like an agent's).
kids = [subprocess.Popen([sys.executable, "-c",
        "import time\nwhile True:\n t=time.time()\n while time.time()-t<0.05: pass\n time.sleep(0.05)"])]
kids += [subprocess.Popen(["sleep", "86400"]) for _ in range(3)]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        alive = sum(1 for k in kids if k.poll() is None)
        body = json.dumps({"pid": os.getpid(), "ticks": ticks, "uptime_s": round(time.time() - START, 3),
                           "dirty_rounds": dirty_rounds, "kids_alive": alive, "kid_pids": [k.pid for k in kids],
                           "hostname": os.uname().nodename, "now": time.time()}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
