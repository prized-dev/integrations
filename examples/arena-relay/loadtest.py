#!/usr/bin/env python3
"""Simulated viewers: N arrive within --spread seconds, each wants a live copy of
one contestant. "Ready" is the viewer's own clock from asking to the first 200 from
its copy's live URL (/api/state), so it counts the window, the fork, the URL and
the first page.

  relay mode:  each viewer asks the relay (POST /v1/contestants/{id}/copies)
  direct mode: each viewer makes its own fork call (count 1, ?wait=running) and
               mints its own URL: the no-relay baseline. Needs the key.

  python loadtest.py relay  --relay http://127.0.0.1:PORT --contestant sbx_... --viewers 25 --out r.jsonl
  python loadtest.py direct --contestant sbx_... --viewers 25 --out d.jsonl --label agent=me
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import secrets
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from prized import AsyncSandbox


def state_url(url: str, path: str = "/api/state") -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, path, p.query, ""))


async def first_page(client: httpx.AsyncClient, url: str, limit: float = 60.0) -> Dict[str, Any]:
    """Poll the live URL until it answers 200; the doc and how many tries it took."""
    su = state_url(url)
    t_end = time.monotonic() + limit
    tries = 0
    last = None
    while time.monotonic() < t_end:
        tries += 1
        try:
            r = await client.get(su, timeout=5)
            if r.status_code == 200:
                return {"doc": r.json(), "tries": tries}
            last = r.status_code
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        await asyncio.sleep(0.1)
    return {"doc": None, "tries": tries, "last": last}


async def viewer_relay(i: int, a: argparse.Namespace, client: httpx.AsyncClient, t0: float, delay: float,
                       abort: bool, leave: bool) -> Dict[str, Any]:
    await asyncio.sleep(delay)
    vid = f"v{i:03d}"
    rec: Dict[str, Any] = {"viewer": vid, "offset_ms": round(delay * 1000)}
    start = time.monotonic()
    url = f"{a.relay}/v1/contestants/{a.contestant}/copies"
    if abort:  # asks, then hangs up before its copy comes: the relay must not keep one for it
        try:
            await client.post(url, json={"viewer": vid}, timeout=0.4)
        except httpx.HTTPError:
            pass
        rec.update(outcome="aborted")
        return rec
    try:
        r = await client.post(url, json={"viewer": vid}, timeout=90)
        doc = r.json()
    except httpx.HTTPError as exc:
        rec.update(outcome="relay_error", error=repr(exc), ms=round((time.monotonic() - start) * 1000))
        return rec
    answered = time.monotonic()
    rec.update(status=r.status_code, answer_ms=round((answered - start) * 1000))
    if r.status_code != 200:
        rec.update(outcome=doc.get("error", "error"), reason=doc.get("reason"), retry_after=doc.get("retryAfterS"))
        return rec
    rec.update(copy=doc["copyId"], batch=doc["batch"], batch_size=doc["batchSize"], relay_wait_ms=doc["waitMs"])
    page = await first_page(client, doc["url"])
    ready = time.monotonic()
    rec.update(ready_ms=round((ready - start) * 1000), page_ms=round((ready - answered) * 1000), tries=page["tries"])
    rec.update(outcome="ready" if page["doc"] else "no_page", first=page["doc"])
    if page["doc"]:  # poke: the copy is live, its counter moves on its own
        await asyncio.sleep(0.5)
        p2 = await first_page(client, doc["url"], 10)
        if p2["doc"]:
            rec["ticks_moved"] = p2["doc"]["ticks"] - page["doc"]["ticks"]
    if leave:
        d = await client.delete(f"{a.relay}/v1/copies/{doc['copyId']}", timeout=20)
        rec["left_early"] = d.status_code
    return rec


async def viewer_direct(i: int, a: argparse.Namespace, client: httpx.AsyncClient, key: str, run: str,
                        delay: float) -> Dict[str, Any]:
    await asyncio.sleep(delay)
    vid = f"v{i:03d}"
    rec: Dict[str, Any] = {"viewer": vid, "offset_ms": round(delay * 1000)}
    h = {"Authorization": f"Bearer {key}", "Idempotency-Key": f"direct-{run}-{i}"}
    base = a.api_url.rstrip("/") + "/api/v1"
    labels = {**dict(kv.split("=", 1) for kv in a.label), "role": "copy", "relay": "direct", "relay_run": run}
    start = time.monotonic()
    try:
        r = await client.post(f"{base}/sandboxes/{a.contestant}/fork", params={"wait": "running", "waitTimeoutMs": "8000"},
                              json={"count": 1, "labels": labels, "maxRuntimeMin": 10}, headers=h, timeout=60)
        doc = r.json()
    except httpx.HTTPError as exc:
        rec.update(outcome="fork_error", error=repr(exc))
        return rec
    forked = time.monotonic()
    rec.update(status=r.status_code, fork_ms=round((forked - start) * 1000))
    if r.status_code >= 300:
        rec.update(outcome=doc.get("error"), fits=doc.get("fits"))
        return rec
    sb = doc["sandboxes"][0]
    rec.update(copy=sb["id"], state_at_answer=sb.get("state"))
    while sb.get("state") == "starting":
        await asyncio.sleep(0.25)
        g = await client.get(f"{base}/sandboxes/{sb['id']}", headers=h, timeout=10)
        sb = g.json().get("sandbox", g.json())
    t = await client.post(f"{base}/sandboxes/{sb['id']}/ports/8080/token", json={"expiresIn": 600},
                          headers={"Authorization": h["Authorization"]}, timeout=10)
    page = await first_page(client, t.json()["url"])
    ready = time.monotonic()
    rec.update(ready_ms=round((ready - start) * 1000), outcome="ready" if page["doc"] else "no_page", first=page["doc"])
    await client.delete(f"{base}/sandboxes/{sb['id']}", headers={"Authorization": h["Authorization"]}, timeout=20)
    return rec


def pct(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 0)


def summary(recs: List[Dict[str, Any]], parent_pid: Optional[int]) -> Dict[str, Any]:
    ready = [r["ready_ms"] for r in recs if r.get("outcome") == "ready"]
    out: Dict[str, Any] = {
        "viewers": len(recs),
        "outcomes": {},
        "ready_p50_ms": pct(ready, 0.5), "ready_p95_ms": pct(ready, 0.95), "ready_max_ms": max(ready) if ready else None,
    }
    for r in recs:
        out["outcomes"][r.get("outcome")] = out["outcomes"].get(r.get("outcome"), 0) + 1
    full = [r["answer_ms"] for r in recs if r.get("outcome") == "full" and "answer_ms" in r]
    if full:
        out["full_answer_p50_ms"] = pct(full, 0.5)
        out["full_answer_max_ms"] = max(full)
    batches: Dict[str, int] = {}
    for r in recs:
        if r.get("batch"):
            batches[r["batch"]] = r["batch_size"]
    if batches:
        out["batches"] = batches
    firsts = [r["first"] for r in recs if r.get("first")]
    if parent_pid is not None and firsts:
        out["same_pid_as_contestant"] = sum(1 for f in firsts if f.get("pid") == parent_pid)
        out["kids_alive_4of4"] = sum(1 for f in firsts if f.get("kids_alive") == 4)
    moved = [r["ticks_moved"] for r in recs if "ticks_moved" in r]
    if moved:
        out["ticks_moving"] = sum(1 for m in moved if m > 0)
    return out


async def amain() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("mode", choices=["relay", "direct"])
    p.add_argument("--relay")
    p.add_argument("--contestant", required=True)
    p.add_argument("--viewers", type=int, default=25)
    p.add_argument("--spread", type=float, default=2.0, help="every viewer arrives within this many seconds")
    p.add_argument("--abort", type=int, default=0, help="viewers that hang up before their copy comes")
    p.add_argument("--leave", type=float, default=0.0, help="share of served viewers that hand their copy back early")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--parent-pid", type=int)
    p.add_argument("--out", required=True)
    p.add_argument("--label", action="append", default=[])
    p.add_argument("--api-url", default=os.environ.get("PRIZED_BASE_URL", "https://api.prized.dev"))
    p.add_argument("--api-key-env", default="PRIZED_API_KEY")
    a = p.parse_args()
    rnd = random.Random(a.seed)
    delays = sorted(rnd.uniform(0, a.spread) for _ in range(a.viewers))
    aborts = set(rnd.sample(range(a.viewers), a.abort)) if a.abort else set()
    leaves = {i for i in range(a.viewers) if i not in aborts and rnd.random() < a.leave}
    limits = httpx.Limits(max_connections=a.viewers * 2 + 10, max_keepalive_connections=a.viewers * 2)
    async with httpx.AsyncClient(limits=limits) as client:
        t0 = time.monotonic()
        wall0 = time.time()
        if a.mode == "relay":
            recs = await asyncio.gather(*(viewer_relay(i, a, client, t0, d, i in aborts, i in leaves) for i, d in enumerate(delays)))
        else:
            key = os.environ.get(a.api_key_env, "").strip()
            run = secrets.token_hex(3)
            try:
                recs = await asyncio.gather(*(viewer_direct(i, a, client, key, run, d) for i, d in enumerate(delays)))
            finally:  # whatever happened, nothing of this run stays
                swept = await AsyncSandbox.delete_by_labels({"relay": "direct", "relay_run": run}, api_key=key, base_url=a.api_url)
                print(json.dumps({"direct_run": run, "swept": len(swept)}), file=sys.stderr)
    with open(a.out, "w") as f:
        for r in recs:
            f.write(json.dumps({"wall0": wall0, **r}) + "\n")
    print(json.dumps(summary(list(recs), a.parent_pid)))


if __name__ == "__main__":
    asyncio.run(amain())
