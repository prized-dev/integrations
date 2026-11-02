"""Build a parent sandbox once and reuse it across runs:

    python -m verifiers_prized build --setup 'python3 -m venv ~/.venv' --setup '~/.venv/bin/pip install numpy'

prints the sandbox id (paused, with the environment built) for `parent = "<id>"` in the runtime
config. It is yours: delete it with `prized sandbox rm <id>` when you are done with it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from prized import PrizedError
from pydantic import ValidationError
from verifiers.v1.errors import SandboxError

from . import pool
from .config import PrizedConfig
from .pool import Credentials, Recipe, build_sandbox, credentials


def _labels(pairs: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--label takes key=value, not {pair!r}")
        labels[key] = value
    return labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m verifiers_prized")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="set up a parent sandbox and print its id")
    build.add_argument("--template", default="prized-base-v1")
    build.add_argument("--cpu", type=float, default=1)
    build.add_argument("--memory", type=float, default=2, help="GB")
    build.add_argument("--disk", type=float, default=10, help="GB")
    build.add_argument("--setup", action="append", default=[], help="a setup command (repeatable)")
    build.add_argument("--setup-env", action="append", default=[], help="a variable to pass to setup")
    build.add_argument("--setup-timeout", type=float, default=1800)
    build.add_argument("--label", action="append", default=[], help="key=value (repeatable)")
    build.add_argument("--max-runtime-min", type=int, default=None)
    build.add_argument("--no-pause", action="store_true", help="leave it running")
    build.add_argument("--api-key-env", default="PRIZED_API_KEY")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        config = PrizedConfig(
            import_path="verifiers_prized.PrizedBackend",
            template=args.template,
            cpu=args.cpu,
            memory=args.memory,
            disk=args.disk,
            setup=args.setup,
            setup_env=args.setup_env,
            setup_timeout=args.setup_timeout,
            labels=_labels(args.label),
            max_runtime_min=args.max_runtime_min,
            api_key_env=args.api_key_env,
        )
    except ValidationError as err:
        print(err, file=sys.stderr)
        return 2
    try:
        creds = credentials(config)
        print(asyncio.run(_build(config, creds, pause=not args.no_pause)))
    except (SandboxError, PrizedError) as err:
        print(f"build failed: {err}", file=sys.stderr)
        return 1
    return 0


async def _build(config: PrizedConfig, creds: Credentials, *, pause: bool) -> str:
    sandbox = await build_sandbox(
        Recipe.of(config, creds),
        creds,
        role="parent",
        pause=pause,
        tracked=False,  # it outlives this process: never deleted at exit
        run=f"build-{pool.RUN_ID}",
    )
    await sandbox.close()
    return str(sandbox.id)


if __name__ == "__main__":
    raise SystemExit(main())
