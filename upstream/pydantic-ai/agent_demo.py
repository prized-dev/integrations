"""A real-model run of PrizedSandbox + Coder: write and run code, pause with a server running, resume from
the history, and fork the sandbox live.

From a pydantic-ai checkout with apply.py applied, with PRIZED_API_KEY and ANTHROPIC_API_KEY set:

    uv run python /path/to/agent_demo.py
"""

import asyncio
import os
import time

from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.prized_sandbox import PrizedSandbox


async def main() -> None:
    sandbox = PrizedSandbox(
        vcpu=2,
        memory_mb=4096,
        max_runtime_min=30,
        labels={'demo': 'pydantic-ai'},
        base_url=os.environ.get('PRIZED_BASE_URL'),
        edge_url=os.environ.get('PRIZED_EDGE_URL'),
    )
    agent = Agent('anthropic:claude-sonnet-5-5', capabilities=[sandbox, Coder()])
    t = time.monotonic()
    result = await agent.run(
        'Write primes.py that counts the primes below 100000 with a sieve, run it with python3, '
        'and start `python3 -m http.server 8000` in the background (nohup, output to server.log). '
        'Reply with the count only.'
    )
    ref = result.workspace.ref
    assert ref is not None
    print('run 1:', round(time.monotonic() - t, 1), 's, answer', result.output.strip(), ref)

    t = time.monotonic()
    await sandbox.pause(ref)
    print('paused in', round(time.monotonic() - t, 2), 's')

    t = time.monotonic()
    followup = await agent.run(
        'Is the http.server from before still running? Check with '
        'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ and reply with the status code only.',
        message_history=result.all_messages(),
    )
    print('run 2, resumed:', round(time.monotonic() - t, 1), 's, answer', followup.output.strip())

    t = time.monotonic()
    copy = await sandbox.fork(ref)
    print('forked in', round(time.monotonic() - t, 2), 's:', copy)
    try:
        backend = sandbox.backend(copy)
        check = await backend.run(
            'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/', shell=True, timeout=60
        )
        await backend.aclose()
        print('the copy has the running server too:', check.stdout)
    finally:
        await sandbox.destroy(copy)
        await sandbox.destroy(ref)


asyncio.run(main())
