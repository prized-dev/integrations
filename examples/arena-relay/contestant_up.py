#!/usr/bin/env python3
"""Start or stop the stand-in contestant sandbox (contestant.py on :8080).

  python contestant_up.py up --label agent=me     # prints the sandbox id and its pid
  python contestant_up.py down sbx_...

The sandbox gets no environment variables and no key. The key stays on the
machine that runs this script and the relay.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from prized import AsyncSandbox

HERE = Path(__file__).resolve().parent


async def up(a: argparse.Namespace, key: str) -> None:
    labels = dict(kv.split("=", 1) for kv in a.label)
    labels.setdefault("role", "contestant")
    sb = await AsyncSandbox.create(
        a.template, vcpu=a.vcpu, memory_gb=a.memory_gb, labels=labels, max_runtime_min=a.max_runtime_min, api_key=key
    )
    try:
        await sb.files.write("contestant.py", (HERE / "contestant.py").read_text())
        await sb.exec(f"python3 /home/prized/work/contestant.py {a.mem_mb}", background=True)
        for _ in range(60):
            r = await sb.exec("curl -s -m 2 localhost:8080/api/state || true")
            if r.stdout.strip().startswith("{"):
                state = json.loads(r.stdout)
                env = await sb.exec("env | grep -ciE 'key|token|secret' || true")
                print(json.dumps({"id": sb.id, "pid": state["pid"], "ticks": state["ticks"],
                                  "secret_like_env_vars": int(env.stdout.strip() or 0)}))
                return
            await asyncio.sleep(1)
        raise SystemExit("contestant did not answer on :8080")
    except BaseException:
        await sb.delete()
        raise
    finally:
        await sb.close()


async def down(a: argparse.Namespace, key: str) -> None:
    sb = await AsyncSandbox.connect(a.id, api_key=key)
    await sb.delete(wait=True)
    print(json.dumps({"deleted": a.id}))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--api-key-env", default="PRIZED_API_KEY")
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--template", default="prized-agent-v1")
    u.add_argument("--vcpu", type=int, default=2)
    u.add_argument("--memory-gb", type=float, default=4)
    u.add_argument("--mem-mb", type=int, default=1500, help="memory the stand-in touches")
    u.add_argument("--max-runtime-min", type=int, default=180)
    u.add_argument("--label", action="append", default=[])
    d = sub.add_parser("down")
    d.add_argument("id")
    a = p.parse_args()
    key = os.environ.get(a.api_key_env, "").strip()
    if not key:
        sys.exit(f"set {a.api_key_env}")
    asyncio.run(up(a, key) if a.cmd == "up" else down(a, key))


if __name__ == "__main__":
    main()
