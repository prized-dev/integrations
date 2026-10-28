"""Check the built wheel as pip installs it: run with the wheel's venv, `python -I`.

The version matches the metadata, `py.typed` ships, the Agents SDK finds the
options and session state by their `prized` type, and nothing is left abstract.
Then an application runs as the SDK's provider guide has it: a `SandboxAgent`
through `Runner` with `SandboxRunConfig(client=...)` and scripted model turns,
against the tests' in-process fake of the API (no network, no key), and its
sandbox is gone afterwards.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
from importlib import metadata
from pathlib import Path

from agents import RunConfig, Runner, set_tracing_disabled
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.session import BaseSandboxClientOptions, SandboxSessionState
from agents.testing import ScriptedModel, assistant_message, function_call

import openai_agents_prized
from openai_agents_prized import (
    PrizedSandboxClient,
    PrizedSandboxClientOptions,
    PrizedSandboxSession,
    PrizedSandboxSessionState,
)

DIST = "openai-agents-prized"


async def run_app() -> str | None:
    """Run one scripted agent against the fake API; return a problem, or `None`."""
    spec = importlib.util.spec_from_file_location(
        "fake_prized", Path(__file__).with_name("fake_prized.py")
    )
    if spec is None or spec.loader is None:
        return "tests/fake_prized.py cannot be loaded"
    fake_prized = importlib.util.module_from_spec(spec)
    sys.modules["fake_prized"] = fake_prized  # its dataclasses look their module up
    spec.loader.exec_module(fake_prized)
    set_tracing_disabled(disabled=True)
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "echo wheel-ok"}, call_id="call_1")],
            [assistant_message("done")],
        ]
    )
    agent = SandboxAgent(name="wheel", model=model, instructions="Work in the sandbox.")
    with tempfile.TemporaryDirectory() as tmp:
        fake = fake_prized.FakePrized(Path(tmp))
        async with (
            fake.client() as http,
            PrizedSandboxClient(
                api_key=fake_prized.API_KEY, api_url=fake_prized.API_ORIGIN, http_client=http
            ) as client,
        ):
            config = RunConfig(sandbox=SandboxRunConfig(client=client))
            result = await Runner.run(agent, "say wheel-ok", run_config=config)
        if result.final_output != "done" or "wheel-ok" not in str(result.new_items):
            return f"the scripted run ended with {result.final_output!r}"
        if len(fake.sandboxes) != 1 or fake.live():
            return f"the run left sandboxes {fake.live()} of {len(fake.sandboxes)}"
    return None


def main() -> int:
    """Return 0 when the installed wheel looks right, 1 with the reason otherwise."""
    problems: list[str] = []
    installed = metadata.version(DIST)
    if openai_agents_prized.__version__ != installed:
        problems.append(f"__version__ {openai_agents_prized.__version__} != metadata {installed}")
    files = {str(f) for f in metadata.files(DIST) or []}
    if "openai_agents_prized/py.typed" not in files:
        problems.append("py.typed is missing from the wheel")
    options = BaseSandboxClientOptions.parse({"type": "prized"})
    if not isinstance(options, PrizedSandboxClientOptions):
        problems.append(f"options type `prized` parses as {type(options).__name__}")
    if SandboxSessionState._subclass_registry.get("prized") is not PrizedSandboxSessionState:
        problems.append("session state type `prized` is not registered")
    problems.extend(
        f"{cls.__name__} leaves {sorted(cls.__abstractmethods__)} abstract"
        for cls in (PrizedSandboxClient, PrizedSandboxSession)
        if cls.__abstractmethods__
    )
    app_problem = asyncio.run(run_app())
    if app_problem is not None:
        problems.append(app_problem)
    for problem in problems:
        sys.stderr.write(f"wheel check: {problem}\n")
    if not problems:
        sys.stdout.write(f"{DIST} {installed}: wheel ok\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
