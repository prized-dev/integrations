"""``prized-managed-agents``: serve an environment (``serve``), make a warm parent to fork sessions
from (``prepare``), or show what it runs (``status``)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from typing import Any

import httpx
from prized import AsyncSandbox, PrizedError

from . import __version__
from . import worker as w
from .cma import CMA
from .config import ConfigError, Settings
from .orchestrator import Orchestrator
from .sandboxes import ROLE, SESSION, WARM_PARENT, SessionSandboxes
from .server import WebhookApp
from .webhooks import Verifier

SIGN_UP = (
    "Make a Prized API key with Sandboxes: Write at "
    "https://prized.dev/dashboard/workspace/api-keys?via=claude-managed-agents-orchestrator. No Prized account "
    "yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow "
    "https://prized.dev/auth.md?via=claude-managed-agents-orchestrator"
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="prized-managed-agents", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--log-format", choices=("text", "json"), default=os.environ.get("LOG_FORMAT", "text"))
    sub = p.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="claim the environment's work and run each session in a sandbox")
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    serve.add_argument(
        "--poll", action="store_true", help="long-poll the queue and take no webhooks, even with a signing key set"
    )
    prep = sub.add_parser("prepare", help="make a warm parent: a paused sandbox with ant installed to fork from")
    prep.add_argument("--setup", action="append", default=[], help="a command to run in it first (repeatable)")
    prep.add_argument("--template", default=None)
    sub.add_parser("status", help="the queue, and the sandboxes this environment's sessions have")
    args = p.parse_args(argv)
    _logging(args.log_format)
    if not os.environ.get("PRIZED_API_KEY"):
        print(f"PRIZED_API_KEY is not set. {SIGN_UP}", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env()
    except ConfigError as err:
        print(str(err), file=sys.stderr)
        return 2
    if os.environ.get("ANTHROPIC_API_KEY"):
        logging.getLogger("prized_managed_agents").warning(
            "ANTHROPIC_API_KEY is set; nothing here uses it (the environment key is the only Anthropic credential)"
        )
    try:
        if args.cmd == "serve":
            if args.poll and settings.webhook_key:
                settings = _without_webhooks(settings)
            return asyncio.run(_serve(settings, args.host, args.port))
        if args.cmd == "prepare":
            return asyncio.run(_prepare(settings, args.setup, args.template))
        return asyncio.run(_status(settings))
    except PrizedError as err:
        print(f"prized: {err}", file=sys.stderr)
        if err.code in ("missing_api_key", "invalid_api_key", "unauthorized"):
            print(SIGN_UP, file=sys.stderr)
        return 1


def _without_webhooks(s: Settings) -> Settings:
    from dataclasses import replace

    return replace(s, webhook_key=None)


async def _serve(settings: Settings, host: str, port: int) -> int:
    import uvicorn

    cma = CMA(settings.base_url, settings.environment_id, settings.environment_key)
    # One connection pool for every Prized call: each SDK call otherwise opens a client of its own,
    # kept open by the handles it returns, which a process that runs for weeks can't afford.
    prized_http = httpx.AsyncClient()
    verifier = Verifier(settings.webhook_key) if settings.webhook_key else None
    orch = Orchestrator(settings, cma, SessionSandboxes(settings, http_client=prized_http))
    await orch.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, orch.stop)
    server = None
    serving = None
    if verifier is not None:
        server = _quiet_server(
            uvicorn.Config(
                WebhookApp(orch, verifier),
                interface="asgi3",
                host=host,
                port=port,
                lifespan="off",
                log_level="warning",
                access_log=False,
                proxy_headers=False,
                server_header=False,
            )
        )
        serving = asyncio.create_task(server.serve())
        logging.getLogger("prized_managed_agents").info(
            "taking webhooks at http://%s:%d/webhook (put HTTPS on 443 in front of it)", host, port
        )
    try:
        await orch.run()
    finally:
        if server is not None and serving is not None:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await serving
        await cma.close()
        await prized_http.aclose()
    return 0


async def _prepare(settings: Settings, setup: list[str], template: str | None) -> int:
    boxes = SessionSandboxes(settings)  # one call: the SDK's own client is fine

    async def ready(sb: Any) -> None:
        await w.install_ant(sb, settings)
        for cmd in setup:
            print(f"$ {cmd}", file=sys.stderr)
            r = await sb.exec(cmd, cwd=settings.workdir, timeout=0, full_output=True)
            sys.stderr.write(r.stdout[-2000:] + r.stderr[-2000:])
            if r.exit_code != 0:
                raise SystemExit(f"setup command failed ({r.exit_code}): {cmd}")

    sb = await boxes.make_parent(ready, template=template)
    print(sb.id)
    print(f"paused with ant {settings.ant_version} installed. Fork sessions from it:", file=sys.stderr)
    print(f"  export WARM_PARENT={sb.id}", file=sys.stderr)
    return 0


async def _status(settings: Settings) -> int:
    cma = CMA(settings.base_url, settings.environment_id, settings.environment_key)
    try:
        stats = await cma.stats()
    finally:
        await cma.close()
    boxes = await SessionSandboxes(settings).managed()
    key = w.run_key(settings.environment_key)
    out: dict[str, Any] = {"environment": settings.environment_id, "queue": stats, "sandboxes": []}
    for sb in boxes:
        sid = (sb.labels or {}).get(SESSION)
        row = {"id": sb.id, "state": sb.state, "session": sid}
        if sb.state == "running":
            with contextlib.suppress(Exception):
                r = await sb.exec(w.STATUS, env=w.env(), timeout=20)
                # genuine: started by this environment's orchestrator for this sandbox's session;
                # the orchestrator ignores any other line (the agent can write the files behind it).
                row["runs"] = [
                    {**st.__dict__, "genuine": st.session_id == sid and w.genuine(key, st)}
                    for st in w.parse_status(r.stdout)
                ]
        out["sandboxes"].append(row)
    out["warm_parents"] = [
        {"id": sb.id, "state": sb.state}
        for sb in await AsyncSandbox.list(labels={**SessionSandboxes(settings).base, ROLE: WARM_PARENT})
    ]
    print(json.dumps(out, indent=2))
    return 0


def _quiet_server(config: Any) -> Any:
    """uvicorn's Server without its signal handlers: the orchestrator owns SIGTERM, so it can turn
    new deliveries away with 503 and finish its handoffs before the listener closes."""
    import uvicorn

    class Quiet(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # type: ignore[no-untyped-def,override]
            yield

        def install_signal_handlers(self) -> None:  # uvicorn before 0.29
            return None

    return Quiet(config)


def _logging(fmt: str) -> None:
    handler = logging.StreamHandler()
    if fmt == "json":

        class Json(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                return json.dumps(
                    {"time": self.formatTime(record), "level": record.levelname, "msg": record.getMessage()}
                )

        handler.setFormatter(Json())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


if __name__ == "__main__":
    sys.exit(main())
