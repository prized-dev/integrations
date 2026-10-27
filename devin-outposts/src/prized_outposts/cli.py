"""Command lines: ``prized-devin-outposts`` (connect, run, warm, sweep, status) and ``prized-machines``
(the session-machine core on its own, for spawn hooks such as Cursor's)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from prized import PrizedError, Sandbox

from .machines import Machines, MachineSpec, label_pairs

OWNER = "prized-devin-outposts"
DEFAULT_STATE = "~/.local/state/prized-devin-outposts"
NETWORK_POLICY_REFUSAL = (
    "its network policy limits where it may connect, and Prized sandboxes can't enforce that yet "
    "(they have open outbound internet). Run it on another outpost, or have your Devin admin drop "
    "the policy for it."
)


def minutes(value: str | None, default: int | None) -> int | None:
    """``"7d"``, ``"15m"``, ``"2h"``, ``"600s"`` or a number of minutes."""
    if value is None or value == "":
        return default
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", value)
    if not m:
        raise SystemExit(f"can't read {value!r} as a duration (try 15m, 2h or 7d)")
    n, unit = int(m.group(1)), m.group(2) or "m"
    return {"s": max(1, n // 60) if n else 0, "m": n, "h": n * 60, "d": n * 1440}[unit]


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def need(name: str, hint: str) -> str:
    value = env(name)
    if not value:
        raise SystemExit(f"{name} is not set: {hint}")
    return value


def load_env_file(path: str | None) -> None:
    if not path:
        return
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def machine_spec() -> MachineSpec:
    def num(name: str, cast: type) -> object | None:
        value = env(name)
        return cast(value) if value else None

    return MachineSpec(
        template=env("PRIZED_TEMPLATE", "prized-base-v1") or "prized-base-v1",
        vcpu=num("PRIZED_VCPU", int),  # type: ignore[arg-type]
        memory_gb=num("PRIZED_MEMORY_GB", float),  # type: ignore[arg-type]
        disk_gb=num("PRIZED_DISK_GB", int),  # type: ignore[arg-type]
        max_runtime_min=minutes(env("MAX_RUNTIME"), None),
        auto_pause_min=minutes(env("AUTO_PAUSE"), 15) or 0,
        auto_delete_min=minutes(env("KEEP_PAUSED"), 7 * 1440) or 0,
        warm_parent=env("WARM_PARENT"),
        labels=label_pairs(filter(None, (env("PRIZED_LABELS", "") or "").split(","))),
    )


def acceptor_id(state_dir: Path) -> str:
    given = env("ACCEPTOR_ID")
    if given:
        return given
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "acceptor_id"
    if path.exists():
        return path.read_text().strip()
    host = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().lower())[:30]
    value = f"prized-{host}-{secrets.token_hex(4)}"
    path.write_text(value + "\n")
    return value


# -- prized-devin-outposts ----------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    from .devin.api import OutpostsAPI
    from .devin.orchestrator import Orchestrator
    from .devin.plan import Rules
    from .devin.remote import STATIC_BASE, RemoteSource

    load_env_file(args.env_file)
    token = need("DEVIN_OUTPOSTS_TOKEN", "a Devin service-user token with Outposts write (or run `connect`)")
    need(
        "PRIZED_API_KEY",
        "make one with Sandboxes: Write at https://prized.dev/dashboard/workspace/api-keys?via=devin-outposts",
    )
    outpost = need("OUTPOST_ID", "the outpost's id (outpost_env-...) or name")
    state = Path(env("STATE_DIR", DEFAULT_STATE) or DEFAULT_STATE).expanduser()
    me = acceptor_id(state)
    api = OutpostsAPI(env("DEVIN_API_URL", "https://api.devin.ai") or "https://api.devin.ai", token, me)
    outpost_id, platform = api.resolve_outpost(outpost)
    if platform not in (None, "linux"):
        raise SystemExit(f"outpost {outpost} serves {platform}; Prized sandboxes run Linux")
    spec = machine_spec()
    ignore_policy = env("IGNORE_NETWORK_POLICY") == "1"

    def refuse(entry: object) -> str | None:
        policy = getattr(entry, "network_policy", {}) or {}
        return None if ignore_policy or not policy.get("enabled") else NETWORK_POLICY_REFUSAL

    rules = Rules(acceptor_id=me, max_concurrent=int(env("MAX_CONCURRENT_SESSIONS", "4") or 4), refuse=refuse)
    orch = Orchestrator(
        api,
        Machines(spec, owner=OWNER),
        RemoteSource(
            static_base=env("DEVIN_WORKER_STATIC_BASE_URL", STATIC_BASE) or STATIC_BASE,
            local_file=env("PRIZED_OUTPOSTS_REMOTE_FILE"),
        ),
        outpost_id=outpost_id,
        rules=rules,
        workdir=env("WORKDIR", "/home/prized/work") or "/home/prized/work",
        chrome_path=env("DEVIN_CHROME_PATH"),
        poll_interval=float(env("POLL_INTERVAL", "5") or 5),
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(f"serving outpost {outpost_id} as {me} on Prized ({spec.template}); Ctrl-C stops", file=sys.stderr)
    orch.run(stop)
    orch.close()
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    from .devin import connect as c

    verifier, challenge = c.pkce_pair()
    url = c.connect_url(challenge, callback_url=args.callback_url, outpost_name=args.name, platform="linux")
    print("Open this page as a Devin admin and click Connect:\n\n  " + url + "\n", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(url)
    code = c.wait_for_code(args.callback_url, timeout=args.timeout)
    conn = c.exchange(code, verifier)
    out = Path(args.env_file).expanduser()
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # an existing file keeps its mode through O_CREAT
    with os.fdopen(fd, "w") as fh:
        fh.write(
            f"DEVIN_OUTPOSTS_TOKEN={conn.access_token}\nDEVIN_API_URL={conn.api_base_url}\n"
            f"OUTPOST_ID={conn.outpost_id}\n"
        )
    print(
        f"Connected outpost {conn.outpost_name or conn.outpost_id}. Settings written to {out} (mode 600).\n"
        f"Next: prized-devin-outposts run --env-file {out}",
        file=sys.stderr,
    )
    return 0


def cmd_warm(args: argparse.Namespace) -> int:
    import shlex

    from .devin.remote import RemoteSource, shell_path

    need(
        "PRIZED_API_KEY",
        "make one with Sandboxes: Write at https://prized.dev/dashboard/workspace/api-keys?via=devin-outposts",
    )
    spec = machine_spec()
    binary = RemoteSource(local_file=env("PRIZED_OUTPOSTS_REMOTE_FILE")).resolve(None)
    workdir = env("WORKDIR", "/home/prized/work") or "/home/prized/work"

    def prepare(sb: Sandbox) -> None:
        cache = "~/.devin/worker/cache"
        steps = [f"mkdir -p {cache} {shell_path(workdir + '/repos')}"]
        if binary.url:
            path = f"{cache}/{binary.filename}"
            steps.append(
                f"curl -fsSL --retry 3 {shlex.quote(binary.url)} -o {path} && "
                f"printf '%s  %s\\n' {binary.digest} {path} | sha256sum -c --status && chmod 0755 {path}"
            )
        for repo in args.repo:
            name = re.sub(r"\.git$", "", repo.rstrip("/").rsplit("/", 1)[-1])
            steps.append(f"git clone -q {shlex.quote(repo)} {shell_path(f'{workdir}/repos/{name}')}")
        if args.setup:
            steps.append(f"cd {shell_path(workdir)} && {args.setup}")
        for step in steps:
            result = sb.exec(step, timeout=1800, full_output=True)
            if result.exit_code != 0:
                raise SystemExit(f"warm-up step failed ({result.exit_code}): {result.stderr.strip()[-500:]}")

    sb = Machines(spec, owner=OWNER).make_parent(prepare)
    print(sb.id)
    print(f"Paused. Set WARM_PARENT={sb.id} for new sessions to start as live copies of it.", file=sys.stderr)
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    from .devin.api import OutpostsAPI
    from .devin.orchestrator import Orchestrator
    from .devin.plan import Rules
    from .devin.remote import RemoteSource

    load_env_file(args.env_file)
    token = need("DEVIN_OUTPOSTS_TOKEN", "a Devin service-user token with Outposts write")
    me = acceptor_id(Path(env("STATE_DIR", DEFAULT_STATE) or DEFAULT_STATE).expanduser())
    api = OutpostsAPI(env("DEVIN_API_URL", "https://api.devin.ai") or "https://api.devin.ai", token, me)
    outpost_id, _ = api.resolve_outpost(need("OUTPOST_ID", "the outpost's id or name"))
    orch = Orchestrator(
        api,
        Machines(machine_spec(), owner=OWNER),
        RemoteSource(),
        outpost_id=outpost_id,
        rules=Rules(acceptor_id=me),
    )
    entries, _ = api.list_all(outpost_id)
    orch.entries = {e.session_id: e for e in entries}
    for sbx in orch.sweep():
        print(f"deleted {sbx}")
    orch.close()
    return 0


def devin_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prized-devin-outposts", description="Serve Devin Outposts sessions with Prized Sandboxes."
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="claim the outpost's sessions and serve each in its own sandbox")
    run.add_argument("--env-file", help="read settings (as written by `connect`) from this file")
    run.set_defaults(fn=cmd_run)
    con = sub.add_parser("connect", help="connect an outpost through Devin's admin consent page")
    con.add_argument("--name", default="prized", help="suggested outpost name (the admin can change it)")
    con.add_argument("--callback-url", default=None)
    con.add_argument("--env-file", default="~/.config/prized-devin-outposts/outpost.env")
    con.add_argument("--timeout", type=float, default=600.0)
    con.add_argument("--no-browser", action="store_true")
    con.set_defaults(fn=cmd_connect)
    warm = sub.add_parser("warm", help="build a paused parent sandbox that new sessions fork from")
    warm.add_argument(
        "--repo", action="append", default=[], help="clone this repository into repos/ (repeatable)"
    )
    warm.add_argument("--setup", help="a shell command to run in the working directory afterwards")
    warm.set_defaults(fn=cmd_warm)
    sweep = sub.add_parser("sweep", help="delete the sandboxes of sessions that terminated")
    sweep.add_argument("--env-file")
    sweep.set_defaults(fn=cmd_sweep)
    args = parser.parse_args(argv)
    if getattr(args, "callback_url", "unset") is None:
        from .devin.connect import CALLBACK_URL

        args.callback_url = CALLBACK_URL
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return args.fn(args)
    except PrizedError as err:
        print(f"prized: {err}", file=sys.stderr)
        return 1


# -- prized-machines ----------------------------------------------------------------------------


def machines_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prized-machines",
        description="One Prized sandbox per session key: ensure (resume, fork or create), hibernate (pause "
        "with memory), destroy. For spawn hooks; settings come from the same environment variables as "
        "prized-devin-outposts.",
    )
    parser.add_argument("action", choices=["ensure", "hibernate", "destroy", "find"])
    parser.add_argument("--key", action="append", required=True, help="the session's label, k=v (repeatable)")
    parser.add_argument("--label", action="append", default=[], help="extra label for a new sandbox, k=v")
    parser.add_argument("--owner", default="prized-machines", help="the managed-by label")
    args = parser.parse_args(argv)
    need(
        "PRIZED_API_KEY", "make one with Sandboxes: Write at https://prized.dev/dashboard/workspace/api-keys"
    )
    machines = Machines(machine_spec(), owner=args.owner)
    key = label_pairs(args.key)
    try:
        if args.action == "ensure":
            made = machines.ensure(key, extra_labels=label_pairs(args.label))
            out = {
                "id": made.sandbox.id,
                "how": made.how,
                "seconds": round(made.seconds, 3),
                "state": made.sandbox.state,
            }
        elif args.action == "hibernate":
            sb = machines.hibernate(key)
            out = {"id": sb.id if sb else None, "state": sb.state if sb else None}
        elif args.action == "destroy":
            out = {"deleted": machines.destroy(key)}
        else:
            sb = machines.find(key)
            out = {"id": sb.id if sb else None, "state": sb.state if sb else None}
    except PrizedError as err:
        print(json.dumps({"error": err.code, "reason": err.reason, "message": err.message}))
        return 1
    print(json.dumps(out))
    return 0
