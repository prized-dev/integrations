"""A coding agent fixes a real bug in a Prized sandbox.

Clones python-slugify into a fresh sandbox, breaks one line, and asks a Pydantic AI agent with
Harness's `Coder` to find and fix it. The test run before and after is done outside the agent.

    PRIZED_API_KEY=... ANTHROPIC_API_KEY=... python examples/fix_bug.py

Set MODEL to any Pydantic AI model string to use another model.
"""

from __future__ import annotations

import asyncio
import os
import time

from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder

from pydantic_ai_prized import PrizedSandbox

MODEL = os.environ.get('MODEL', 'anthropic:claude-sonnet-5-5')
REPO = 'https://github.com/un33k/python-slugify'
TAG = 'v8.0.4'
SETUP = f"""
set -e
git clone -q --depth 1 --branch {TAG} {REPO} slugify-repo
cd slugify-repo
python3 -m venv .venv
.venv/bin/pip install -q pytest -e .
# The bug: separators are no longer collapsed, so 'a--b' stays 'a--b'.
sed -i "s/^DUPLICATE_DASH_PATTERN = re.compile(r'-{{2,}}')/DUPLICATE_DASH_PATTERN = re.compile(r'-{{3,}}')/" slugify/slugify.py
grep -q "r'-{{3,}}'" slugify/slugify.py
git -c user.email=a@b -c user.name=setup commit -qam 'refactor'
"""
TESTS = 'cd slugify-repo && .venv/bin/python -m pytest -q test.py 2>&1 | tail -3'
PROMPT = (
    'The repository in ./slugify-repo has failing tests (run them with `.venv/bin/python -m pytest -q test.py` '
    'from that directory). Find the bug in the library code, fix it with the smallest change, and run the '
    'tests again to confirm. Do not edit the tests.'
)


async def main() -> None:
    sandbox = PrizedSandbox(
        vcpu=2,
        memory_mb=4096,
        max_runtime_min=30,
        labels=dict(pair.split('=', 1) for pair in os.environ.get('PRIZED_LABELS', '').split(',') if '=' in pair),
    )
    backend = sandbox.backend()
    started = time.monotonic()
    try:
        setup = await backend.run(SETUP, shell=True, timeout=300)
        assert setup.exit_code == 0, setup.stderr
        print(f'sandbox {backend.ref.id} ready in {time.monotonic() - started:.1f}s')
        print('before:', (await backend.run(TESTS, shell=True, timeout=120)).stdout.strip().splitlines()[-1])

        agent = Agent(MODEL, capabilities=[sandbox, Coder()])
        t = time.monotonic()
        result = await agent.run(PROMPT, workspace=backend.ref)
        calls = [p for m in result.all_messages() for p in getattr(m, 'parts', []) if p.part_kind == 'tool-call']
        print(f'agent: {time.monotonic() - t:.1f}s, {len(calls)} tool calls, {result.usage}')
        print('tools used:', sorted({c.tool_name for c in calls}))
        print('answer:', result.output[:600])

        print('after:', (await backend.run(TESTS, shell=True, timeout=120)).stdout.strip().splitlines()[-1])
        diff = await backend.run('cd slugify-repo && git diff', shell=True, timeout=30)
        print(diff.stdout)
    finally:
        await backend.aclose()
        if backend.ref is not None:
            await sandbox.destroy(backend.ref)
            print('deleted', backend.ref.id)


if __name__ == '__main__':
    asyncio.run(main())
