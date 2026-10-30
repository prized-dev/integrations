"""``openhands-prized serve``: a Prized sandbox as an Agent Canvas backend.

Starts (or reattaches to) a sandbox running the Agent Server and serves it on
``http://127.0.0.1:<port>`` until Ctrl-C. Add that URL and the printed API key in
Agent Canvas (Manage Backends > Add Backend).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from ._version import __version__


def _labels(values: list[str]) -> dict[str, str]:
    out = {}
    for v in values:
        k, sep, val = v.partition("=")
        if not sep or not k:
            raise SystemExit(f"--label wants KEY=VALUE, not {v!r}")
        out[k] = val
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openhands-prized", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"openhands-prized {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="serve a sandbox's Agent Server on 127.0.0.1 for Agent Canvas")
    where = s.add_mutually_exclusive_group()
    where.add_argument("--sandbox", metavar="ID", help="reattach to this sandbox (one a previous serve paused)")
    where.add_argument("--fork", metavar="ID", help="fork this sandbox instead of creating one")
    s.add_argument("--template", default="prized-base-v1", help="template for a new sandbox (default %(default)s)")
    s.add_argument("--vcpu", type=int)
    s.add_argument("--memory", type=float, metavar="GB")
    s.add_argument("--disk", type=int, metavar="GB")
    s.add_argument("--max-runtime", type=int, metavar="MIN", help="minutes before Prized deletes the sandbox")
    s.add_argument("--label", action="append", default=[], metavar="KEY=VALUE")
    s.add_argument("--port", type=int, default=None, help="local port (default: a free one)")
    s.add_argument("--connection", choices=["auto", "exec", "ingress"], default="auto")
    s.add_argument(
        "--on-exit",
        choices=["pause", "delete", "keep"],
        default="pause",
        help="what Ctrl-C does to the sandbox (default %(default)s: memory, files and conversations are kept)",
    )
    s.add_argument("--agent-server-version", metavar="V", help="default: your openhands-sdk version")
    s.add_argument("-v", "--verbose", action="store_true")
    return p


def serve(args: argparse.Namespace) -> int:
    from .workspace import PrizedWorkspace

    print("Starting the Agent Server in a Prized sandbox...", file=sys.stderr, flush=True)
    ws = PrizedWorkspace(
        sandbox_id=args.sandbox,
        fork_from=args.fork,
        template=args.template,
        vcpu=args.vcpu,
        memory_gb=args.memory,
        disk_gb=args.disk,
        max_runtime_min=args.max_runtime,
        labels={"openhands": "canvas", **_labels(args.label)},
        host_port=args.port,
        connection=args.connection,
        on_exit=args.on_exit,
        agent_server_version=args.agent_server_version,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        print(
            f"\nAgent Server for Agent Canvas, in Prized sandbox {ws.sandbox.id} ({ws.transport}):\n"
            f"  Host / Base URL:  {ws.host}\n"
            f"  API key:          {ws.api_key}\n\n"
            "Add it in Agent Canvas: Manage Backends > Add Backend.\n"
            f"Ctrl-C to stop; the sandbox is then {'paused' if args.on_exit == 'pause' else args.on_exit + 'd'}"
            + (f" (serve again with --sandbox {ws.sandbox.id})." if args.on_exit != "delete" else "."),
            flush=True,
        )
        try:
            while not stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
    finally:
        print("\nStopping...", file=sys.stderr, flush=True)
        ws.cleanup()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    if args.command == "serve":
        return serve(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
