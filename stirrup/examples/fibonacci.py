"""Stirrup's E2B example, on a Prized sandbox. Needs PRIZED_API_KEY and OPENROUTER_API_KEY.

pip install stirrup-prized
python examples/fibonacci.py
"""

import asyncio

from stirrup import Agent
from stirrup.clients.chat_completions_client import ChatCompletionsClient

from stirrup_prized import PrizedCodeExecToolProvider


async def main() -> None:
    client = ChatCompletionsClient(
        base_url="https://openrouter.ai/api/v1",
        model="anthropic/claude-opus-5",
        max_tokens=8_192,
        context_window_tokens=1_000_000,
    )
    code_exec = PrizedCodeExecToolProvider(timeout=1800)  # sandbox lifetime: 30 minutes of running
    agent = Agent(client=client, name="agent", tools=[code_exec], max_turns=15)

    async with agent.session(output_dir="output") as session:
        await session.run(
            "Make a venv, install matplotlib in it, generate the first 50 Fibonacci numbers and "
            "chart their growth. Save the chart as fibonacci.png"
        )


if __name__ == "__main__":
    asyncio.run(main())
