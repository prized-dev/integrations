"""GRPO-style rollouts that branch from one live coding_env state with Prized fork(N).

A parent episode in OpenEnv's coding_env (a Python REPL) runs a few cells that
build state: a noisy dataset, a train/test split, helpers. Every iteration the
policy samples a group of G next cells, ``fork_group`` copies the live
environment G times in one ``fork(G)`` call, each copy runs one cell, and the
group's rewards give group-relative advantages, as in GRPO. Every branch starts
from the parent's exact state; nothing is replayed.

The policy is a softmax over a fixed menu of five cells, so this runs on a CPU
in about a minute. Swap ``Policy.sample`` and ``Policy.update`` for your
model's sampler and optimizer step.

    pip install "openenv-prized @ git+https://github.com/prized-dev/integrations#subdirectory=openenv"
    pip install "openenv-coding_env @ git+https://github.com/openenv/OpenEnv#subdirectory=envs/coding_env"
    export PRIZED_API_KEY=...   # Sandboxes: Write
    python grpo_fork_group.py --group 8 --iters 6

``--no-reseed`` keeps every copy on the parent's RNG state: the
``random_search`` cell then draws the same numbers in every branch that runs it
(common random numbers), instead of its own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import time

from coding_env import CodeAction, CodingEnv

from openenv_prized import PrizedProvider, fork_group, forkable

# The shared prefix: the state every group branches from.
PREFIX = [
    "import random\n"
    "rng = random.Random(7)\n"
    "xs = [i / 20 for i in range(200)]\n"
    "ys = [3.0 * x + 2.0 + rng.gauss(0, 0.5) for x in xs]",
    "train = list(zip(xs[::2], ys[::2]))\ntest = list(zip(xs[1::2], ys[1::2]))",
    "def mse(f, data):\n    return sum([(f(x) - y) ** 2 for x, y in data]) / len(data)",
    "def mean(v):\n    return sum(v) / len(v)\n"
    "mx = mean([x for x, _ in train])\nmy = mean([y for _, y in train])",
]

# The policy's actions: candidate next cells. Each prints its test error.
CANDIDATES = {
    "mean": "def pred(x):\n    return my\nprint('mse=%.6f' % mse(pred, test))",
    "slope_only": "a = sum([x * y for x, y in train]) / sum([x * x for x, _ in train])\n"
    "def pred(x):\n    return a * x\nprint('mse=%.6f' % mse(pred, test))",
    "least_squares": "a = sum([(x - mx) * (y - my) for x, y in train]) / "
    "sum([(x - mx) ** 2 for x, _ in train])\nb = my - a * mx\n"
    "def pred(x):\n    return a * x + b\nprint('mse=%.6f' % mse(pred, test))",
    # Draws from the environment's own RNG: the cell reseeding is for.
    "random_search": "best = (1e9, 0.0, 0.0)\nfor _ in range(25):\n"
    "    a = random.uniform(0, 6)\n    b = random.uniform(-2, 6)\n"
    "    err = sum([(a * x + b - y) ** 2 for x, y in train]) / len(train)\n"
    "    if err < best[0]:\n        best = (err, a, b)\n"
    "def pred(x):\n    return best[1] * x + best[2]\nprint('mse=%.6f' % mse(pred, test))",
    "buggy": "print('mse=%.6f' % mse(predict, test))",
}
NAMES = list(CANDIDATES)


def reward(observation) -> float:
    """1 / (1 + test MSE) for a cell that ran; 0 for one that failed."""
    found = re.search(r"mse=([0-9.]+)", observation.stdout or "")
    if observation.exit_code != 0 or not found:
        return 0.0
    return 1.0 / (1.0 + float(found.group(1)))


class Policy:
    """A softmax over CANDIDATES; GRPO's update with a group-relative baseline."""

    def __init__(self, lr: float, seed: int):
        self.logits = [0.0] * len(NAMES)
        self.lr = lr
        self.rng = random.Random(seed)

    def probs(self) -> list[float]:
        top = max(self.logits)
        weights = [math.exp(v - top) for v in self.logits]
        return [w / sum(weights) for w in weights]

    def sample(self, n: int) -> list[int]:
        return self.rng.choices(range(len(NAMES)), weights=self.probs(), k=n)

    def update(self, actions: list[int], rewards: list[float]) -> list[float]:
        mean = sum(rewards) / len(rewards)
        std = math.sqrt(sum((r - mean) ** 2 for r in rewards) / len(rewards))
        advantages = [(r - mean) / (std + 1e-6) for r in rewards]
        probs = self.probs()
        for action, advantage in zip(actions, advantages):
            for k in range(len(NAMES)):
                grad = (1.0 if k == action else 0.0) - probs[k]
                self.logits[k] += self.lr * advantage * grad / len(actions)
        return advantages


async def main(args: argparse.Namespace) -> None:
    # fork_base: the parent runs in a fork of the sandbox that installed it, so
    # each group's fork copies only what the server touched since.
    provider = PrizedProvider(
        labels={"example": "grpo-fork-group"}, fork_base=not args.no_fork_base
    )
    started = time.monotonic()
    env = await CodingEnv.from_docker_image(
        "space:openenv/coding_env", provider=provider
    )
    async with env:  # closing it deletes the parent sandbox
        await forkable(env)  # the episode must live in a session a copy can rejoin
        await env.reset()
        for cell in PREFIX:
            result = await env.step(CodeAction(code=cell))
            assert result.observation.exit_code == 0, result.observation.stderr
        parent = await env.state()
        print(
            f"parent ready in {time.monotonic() - started:.1f} s: episode "
            f"{parent.episode_id}, step {parent.step_count}"
        )

        policy = Policy(lr=args.lr, seed=args.seed)
        for it in range(args.iters):
            actions = policy.sample(args.group)
            # One fork(G): G copies of the live env, each client on the parent's
            # episode at its step, with its own RNG seed and episode id.
            async with fork_group(
                env,
                args.group,
                reseed=not args.no_reseed,
                seeds=args.seed * 1_000_000 + it * args.group,
            ) as group:
                results = await asyncio.gather(
                    *(
                        client.step(CodeAction(code=CANDIDATES[NAMES[a]]))
                        for client, a in zip(group, actions)
                    )
                )
            # The copies are deleted; the parent is still at its step.
            rewards = [reward(r.observation) for r in results]
            advantages = policy.update(actions, rewards)
            line = {
                "iter": it,
                "fork_group_s": group.timings,
                "rate_limited": group.rate_limited,
                "rollouts": [
                    {
                        "episode_id": branch.episode_id,
                        "action": NAMES[a],
                        "reward": round(r, 4),
                        "advantage": round(adv, 3),
                    }
                    for branch, a, r, adv in zip(
                        group.branches, actions, rewards, advantages
                    )
                ],
                "mean_reward": round(sum(rewards) / len(rewards), 4),
                "policy": {n: round(p, 3) for n, p in zip(NAMES, policy.probs())},
            }
            print(json.dumps(line) if args.json else _summary(line))

        print(f"parent still at step {(await env.state()).step_count}")


def _summary(line: dict) -> str:
    timings = line["fork_group_s"]
    picks = ", ".join(f"{r['action']}={r['reward']}" for r in line["rollouts"])
    best = max(line["policy"], key=line["policy"].get)
    return (
        f"iter {line['iter']}: group ready in {timings['total_s']:.1f} s "
        f"(fork {timings['fork_s']:.1f} s), mean reward {line['mean_reward']:.3f}, "
        f"policy favours {best} ({line['policy'][best]:.2f})\n  {picks}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--group", type=int, default=8, help="rollouts per group (G)")
    parser.add_argument("--iters", type=int, default=6)
    parser.add_argument("--lr", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--no-reseed", action="store_true")
    parser.add_argument(
        "--no-fork-base", action="store_true", help="fork the installing sandbox"
    )
    parser.add_argument(
        "--json", action="store_true", help="one JSON line per iteration"
    )
    asyncio.run(main(parser.parse_args()))
