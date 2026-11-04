"""Build a parent sandbox once and reuse it across runs:

    python -m verifiers_prized build --setup 'python3 -m venv ~/.venv' --setup '~/.venv/bin/pip install numpy'

prints the sandbox id (paused, with the environment built) for `parent = "<id>"` in the runtime
config. It is yours: delete it with `prized sandbox rm <id>` when you are done with it.

Once paused, it is forked once and the copy deleted (`--no-warm` skips that): a freshly paused
sandbox's first fork writes its memory to disk before any copy starts (12 to 25 s for 8 GB, about
a second for the default 2 GB), so this keeps that write out of a run that starts within the 15
minutes after which Prized writes it anyway. A Ctrl-C during that warm-up keeps the parent, set up
and paused, and prints its id (exit status 130).
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
from .pool import Credentials, Recipe, build_sandbox, credentials, warm_fork


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
    build.add_argument(
        "--no-warm",
        action="store_true",
        help="skip the throwaway fork that readies the paused parent for a fast first fork",
    )
    build.add_argument("--api-key-env", default="PRIZED_API_KEY")
    build.add_argument(
        "--base-url", default=None, help="the Prized API's origin (default https://api.prized.dev)"
    )
    args = parser.parse_args(argv)

    # Progress lines from this package only: the root logger would add one per HTTP request.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    pool.logger.addHandler(handler)
    pool.logger.setLevel(logging.INFO)
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
            base_url=args.base_url,
        )
    except ValidationError as err:
        print(err, file=sys.stderr)
        return 2
    try:
        creds = credentials(config)
        pause = not args.no_pause
        sandbox_id, interrupted = asyncio.run(
            _build(config, creds, pause=pause, warm=pause and not args.no_warm)
        )
    except (SandboxError, PrizedError) as err:
        print(f"build failed: {err}", file=sys.stderr)
        return 1
    print(sandbox_id)
    return 130 if interrupted else 0


async def _build(config: PrizedConfig, creds: Credentials, *, pause: bool, warm: bool) -> tuple[str, bool]:
    """The parent's id, and whether a Ctrl-C cut its warm-up short (the parent is kept then: its
    setup is done, and only its first fork is slower)."""
    recipe = Recipe.of(config, creds)
    sandbox = await build_sandbox(
        recipe,
        creds,
        role="parent",
        pause=pause,
        tracked=False,  # it outlives this process: never deleted at exit
        run=f"build-{pool.RUN_ID}",
    )
    try:
        if warm:
            pool.logger.info(
                "verifiers-prized: parent %s is set up and paused; forking it once so a run's first fork "
                "is fast (a Ctrl-C now keeps it)",
                sandbox.id,
            )
            try:
                await warm_fork(sandbox, recipe, creds)
            except asyncio.CancelledError:  # Ctrl-C: asyncio.run cancels this task
                pool.logger.warning(
                    "verifiers-prized: warm-up interrupted; parent %s is kept, set up and paused "
                    "(its first fork will be slower)",
                    sandbox.id,
                )
                return str(sandbox.id), True
        return str(sandbox.id), False
    finally:
        await sandbox.close()


if __name__ == "__main__":
    raise SystemExit(main())
