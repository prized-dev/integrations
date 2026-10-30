"""``openhands-prized serve``: a Prized sandbox as an Agent Canvas backend.

Starts (or reattaches to) a sandbox running the Agent Server and serves it on
``http://127.0.0.1:<port>`` until Ctrl-C. Add that URL and the printed API key in
Agent Canvas (Add Backend > Agent-server > Local).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time

from ._version import __version__


def _labels(values: list[str]) -> dict[str, str]:
    out = {}
    for v in values:
        k, sep, val = v.partition("=")
        if not sep or not k:
            raise SystemExit(f"--label wants KEY=VALUE, not {v!r}")
        out[k] = val
    return out


DEFAULT_PORT = 8780


def _free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openhands-prized", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"openhands-prized {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="serve a sandbox's Agent Server on 127.0.0.1 for Agent Canvas")
    where = s.add_mutually_exclusive_group()
    where.add_argument("--sandbox", metavar="ID", help="reattach to this sandbox")
    where.add_argument("--fork", metavar="ID", help="fork this sandbox instead of creating one")
    where.add_argument(
        "--new",
        action="store_true",
        help="always create a new sandbox (default: reattach to the newest one an earlier serve left, if any)",
    )
    s.add_argument("--template", default="prized-base-v1", help="template for a new sandbox (default %(default)s)")
    s.add_argument("--vcpu", type=int)
    s.add_argument("--memory", type=float, metavar="GB")
    s.add_argument("--disk", type=int, metavar="GB")
    s.add_argument("--max-runtime", type=int, metavar="MIN", help="minutes before Prized deletes the sandbox")
    s.add_argument("--label", action="append", default=[], metavar="KEY=VALUE")
    s.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"local port (default {DEFAULT_PORT} when free, so Canvas's backend URL stays the same; else any free one)",
    )
    s.add_argument("--connection", choices=["auto", "exec", "ingress"], default="exec")
    s.add_argument(
        "--on-exit",
        choices=["pause", "delete", "keep"],
        default="pause",
        help="what Ctrl-C does to the sandbox (default %(default)s: memory, files and conversations are kept)",
    )
    s.add_argument("--agent-server-version", metavar="V", help="default: your openhands-sdk version")
    s.add_argument("-v", "--verbose", action="store_true")
    return p


CANVAS_LABEL = {"openhands": "canvas"}


def _interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def _stop_on_signals() -> None:
    """Make SIGTERM and SIGHUP (the terminal closing) stop serve like Ctrl-C does, so the sandbox is
    still paused. SIGHUP does not exist on Windows, a SIGHUP that was ignored on purpose (``nohup``)
    stays ignored, and ``signal.signal`` raises ValueError off the main thread."""
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            if signal.getsignal(sig) is signal.SIG_IGN:
                continue
            signal.signal(sig, _interrupt)
        except ValueError:
            return  # not the main thread: the caller owns the signals


def _wait_for_interrupt() -> None:
    while True:
        time.sleep(3600)


def _previous_sandbox() -> str | None:
    """The newest sandbox an earlier serve left (paused or running), so a plain
    ``serve`` picks up where the last one stopped instead of piling up paused ones."""
    from prized import Sandbox

    try:
        found = Sandbox.list(labels=CANVAS_LABEL)
    except Exception:
        return None
    usable = [s for s in found if s.state not in ("failed", "deleting", "deleted")]
    return usable[0].id if usable else None


def serve(args: argparse.Namespace) -> int:
    from .workspace import PrizedWorkspace

    # A signal during the ~20 s startup must still clean up (KeyboardInterrupt does).
    _stop_on_signals()
    sandbox_id = args.sandbox
    if not sandbox_id and not args.fork and not args.new:
        sandbox_id = _previous_sandbox()
        if sandbox_id:
            print(f"Reattaching to {sandbox_id} (--new for a fresh sandbox)...", file=sys.stderr, flush=True)
    print("Starting the Agent Server in a Prized sandbox...", file=sys.stderr, flush=True)
    ws = PrizedWorkspace(
        sandbox_id=sandbox_id,
        fork_from=args.fork,
        template=args.template,
        vcpu=args.vcpu,
        memory_gb=args.memory,
        disk_gb=args.disk,
        max_runtime_min=args.max_runtime,
        labels={**CANVAS_LABEL, **_labels(args.label)},
        host_port=args.port or (DEFAULT_PORT if _free(DEFAULT_PORT) else None),
        connection=args.connection,
        on_exit=args.on_exit,
        agent_server_version=args.agent_server_version,
    )
    try:
        print(
            f"\nAgent Server for Agent Canvas, in Prized sandbox {ws.sandbox.id} ({ws.transport}):\n"
            f"  Host / Base URL:  {ws.host}\n"
            f"  API key:          {ws.api_key}\n\n"
            "Add it in Agent Canvas: Add Backend > Agent-server > Local (a name, this Host, this API key).\n"
            f"Ctrl-C to stop; the sandbox is then {'paused' if args.on_exit == 'pause' else args.on_exit + 'd'}"
            + (" (serve again to pick it up)." if args.on_exit != "delete" else "."),
            flush=True,
        )
        try:
            _wait_for_interrupt()
        except KeyboardInterrupt:
            pass
    finally:
        try:
            print("\nStopping...", file=sys.stderr, flush=True)
        except OSError:
            pass  # stderr already gone (a closed pipe or terminal): the sandbox must still be paused
        ws.cleanup()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The OpenHands SDK prints a banner box when it is imported; serve's output is the URL and key.
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    if args.command == "serve":
        return serve(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
