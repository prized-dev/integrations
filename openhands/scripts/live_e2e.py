"""Live end-to-end check: a real OpenHands agent fixes a real bug in a Prized sandbox.

Needs PRIZED_API_KEY, LLM_MODEL (a LiteLLM model name, e.g. anthropic/claude-sonnet-5-5),
LLM_API_KEY and optionally LLM_BASE_URL. Creates two small sandboxes (2 vCPU, 4 GB) and
deletes both at the end. Prints one JSON line: RESULT {...}.

    python scripts/live_e2e.py

What it checks:
1. Cold start: a new sandbox, the Agent Server installed and running, the proxy up.
2. The agent fixes two bugs in a small module so its unit tests pass, without
   editing the tests (checked independently afterwards).
3. The SDK's git API (served by the Agent Server, through the proxy) sees the change, and a
   200 KB upload and a 3 MB download through the Agent Server's file API arrive intact.
4. pause() and resume() keep the server running.
5. A fork of the sandbox starts warm (the server is already installed) and has the fix.
"""

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BUGGY = '''"""Tiny inventory module."""


def total_value(items):
    """Sum of price * quantity over items: dicts with 'price' and 'qty'."""
    total = 0
    for item in items:
        total = item["price"] * item["qty"]
    return total


def restock(items, name, qty):
    """Add qty to the item called name; return the new quantity, or None if absent."""
    for item in items:
        if item["name"] == name:
            item["qty"] += qty
    return None
'''

TESTS = """import unittest

from inventory import restock, total_value


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"name": "bolt", "price": 2, "qty": 10},
            {"name": "nut", "price": 1, "qty": 5},
        ]

    def test_total_value(self):
        self.assertEqual(total_value(self.items), 25)

    def test_empty_total(self):
        self.assertEqual(total_value([]), 0)

    def test_restock_returns_new_quantity(self):
        self.assertEqual(restock(self.items, "nut", 3), 8)

    def test_restock_missing(self):
        self.assertIsNone(restock(self.items, "gear", 1))


if __name__ == "__main__":
    unittest.main()
"""

TASK = (
    "The project in /home/prized/work/inv has failing unit tests (run them with "
    "`cd /home/prized/work/inv && python3 -m unittest -v`). Fix the bugs in inventory.py "
    "without changing test_inventory.py, run the tests to confirm they pass, then finish."
)

VCPU, MEMORY_GB = 2, 4
# Prized list price: $0.04 per vCPU-hour plus $0.0125 per GB-hour of memory.
SANDBOX_USD_PER_HOUR = 0.04 * VCPU + 0.0125 * MEMORY_GB


def default_agent():
    from openhands.sdk import LLM
    from openhands.tools.preset.default import get_default_agent
    from pydantic import SecretStr

    llm = LLM(
        usage_id="agent",
        model=os.environ["LLM_MODEL"],
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=SecretStr(os.environ["LLM_API_KEY"]),
    )
    return get_default_agent(llm=llm, cli_mode=True)


def run(agent_factory=default_agent, labels=None) -> dict:
    from openhands.sdk import Conversation

    from openhands_prized import PrizedWorkspace

    labels = {"openhands": "live-e2e", **(labels or {})}
    t0 = time.monotonic()
    ws = PrizedWorkspace(vcpu=VCPU, memory_gb=MEMORY_GB, labels=labels, max_runtime_min=60, on_exit="keep")
    res: dict = {"sandbox": ws.sandbox.id, "transport": ws.transport, "cold_start_s": round(time.monotonic() - t0, 2)}
    res["cold_breakdown"] = ws.timings
    fork = None
    try:
        assert ws.file_upload(BUGGY.encode(), "inv/inventory.py").success
        assert ws.file_upload(TESTS.encode(), "inv/test_inventory.py").success
        ws.execute_command(
            "git init -q && git add -A && git -c user.email=e2e@example.com -c user.name=e2e commit -qm start",
            cwd="inv",
        )
        res["tests_before"] = ws.execute_command("python3 -m unittest -q 2>&1 | tail -1", cwd="inv").stdout.strip()

        events: list = []
        conv = Conversation(agent=agent_factory(), workspace=ws, callbacks=[events.append])
        t1 = time.monotonic()
        conv.send_message(TASK)
        conv.run()
        res["task_s"] = round(time.monotonic() - t1, 1)
        res["status"] = str(conv.state.execution_status)
        res["events"] = len(events)
        metrics = conv.conversation_stats.get_combined_metrics()
        usage = metrics.accumulated_token_usage
        res["llm_cost_usd"] = round(metrics.accumulated_cost, 4)
        res["tokens"] = (
            {
                "prompt": usage.prompt_tokens,
                "completion": usage.completion_tokens,
                "cache_read": usage.cache_read_tokens,
                "cache_write": usage.cache_write_tokens,
            }
            if usage
            else None
        )
        conv.close()

        unchanged = ws.execute_command("cat test_inventory.py", cwd="inv").stdout == TESTS
        passing = ws.execute_command("python3 -m unittest -q", cwd="inv").exit_code == 0
        res["solved"] = passing and unchanged
        res["git_changes"] = sorted(str(c.path) for c in ws.git_changes("/home/prized/work/inv"))

        # Large transfers through the Agent Server (the proxy stages big bodies as files).
        from openhands.sdk.workspace import RemoteWorkspace

        up = os.urandom(200 * 1024)
        assert RemoteWorkspace.file_upload(ws, up, "/home/prized/work/up.bin").success
        same = ws.execute_command("sha256sum up.bin").stdout.split()[0] == hashlib.sha256(up).hexdigest()
        down = os.urandom(3 * 1024 * 1024)
        assert ws.file_upload(down, "down.bin").success
        with tempfile.TemporaryDirectory() as tmp:
            got = RemoteWorkspace.file_download(ws, "/home/prized/work/down.bin", f"{tmp}/down.bin")
            res["big_transfers_ok"] = bool(same and got.success and Path(f"{tmp}/down.bin").read_bytes() == down)

        t2 = time.monotonic()
        ws.pause()
        ws.resume()
        res["pause_resume_s"] = round(time.monotonic() - t2, 2)
        res["alive_after_resume"] = ws.alive

        t3 = time.monotonic()
        fork = PrizedWorkspace(fork_from=ws.sandbox.id, labels=labels)
        res["warm_fork_start_s"] = round(time.monotonic() - t3, 2)
        res["warm_breakdown"] = fork.timings
        res["fork_has_fix"] = fork.execute_command("python3 -m unittest -q", cwd="inv").exit_code == 0
        elapsed_h = (time.monotonic() - t0) / 3600
        res["sandbox_cost_usd"] = round(SANDBOX_USD_PER_HOUR * elapsed_h, 4)
    finally:
        if fork is not None:
            fork.cleanup()
        ws.on_exit = "delete"
        ws.cleanup()
    return res


if __name__ == "__main__":
    result = run()
    print("RESULT " + json.dumps(result), flush=True)
    ok = result.get("solved") and result.get("fork_has_fix") and result.get("big_transfers_ok")
    sys.exit(0 if ok else 1)
