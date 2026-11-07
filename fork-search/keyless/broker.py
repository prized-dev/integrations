"""Run Harbor in a keyless Prized sandbox: this broker holds the key.

    broker.py setup HARBOR_PRIZED_DIR    # make the runner: Harbor 0.25.0 + harbor-prized + the shim
                                         # + forksearch, and Terminal-Bench 2.1 cloned at TB_COMMIT
    broker.py run NAME -- HARBOR ARGS    # run `harbor run ARGS` in the runner, serving its SDK calls
    broker.py sh 'CMD'                   # run a shell command in the runner (tests, inspection)
    broker.py fetch NAME                 # copy the runner's jobs/NAME back to runs/NAME
    broker.py cleanup                    # delete every sandbox with this task's labels

Inside the runner, harbor-prized imports a stand-in `prized` (keyless/shim) that
turns each SDK call into a request file; this broker carries it out with the
agent-xp workspace's key, adding the task's labels and keeping to its limits.
The key never enters a sandbox. State: keyless/runner.id, logs in runs/.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import pathlib
import shlex
import sys
import tarfile
import time

from prized import AsyncSandbox, CommandTimeoutError, PrizedError

HERE = pathlib.Path(__file__).resolve().parent
TASK = HERE.parent  # fork-search/
RUNS = pathlib.Path(os.environ.get("FS_RUNS", TASK / "runs"))
LABELS = {"agent": "agent-xp", "task": os.environ.get("FS_TASK_LABEL", "fork-search-1107")}
SPOOL = "/home/prized/rpc"
MAX_LIVE = 24  # trial sandboxes, fresh copies and forks at once
MAX_RUNTIME_MIN = 480
MAX_DISK_GB = 40
HARBOR = "harbor==0.25.0"
TB_REPO = "https://github.com/harbor-framework/terminal-bench-2-1"
TB_COMMIT = "12b7565"
CHUNK = 64 * 1024 * 1024


def _key() -> str:
    path = pathlib.Path(os.environ["AX_KEY_FILE"])
    return path.read_text().strip()


async def _runner() -> AsyncSandbox:
    rid = (HERE / "runner.id").read_text().strip()
    return await AsyncSandbox.connect(rid, api_key=_key())


async def _sh(sb: AsyncSandbox, cmd: str, timeout: float = 600) -> str:
    r = await sb.exec(cmd, timeout=timeout, full_output=True)
    if r.exit_code != 0:
        raise SystemExit(f"runner command failed ({r.exit_code}): {cmd[:120]}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r.stdout


def _tar_dir(path: pathlib.Path, arcname: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        skip = ("__pycache__", ".pyc", "/runs", "/.axkey", "/venv")
        tar.add(path, arcname=arcname, filter=lambda ti: None if any(s in ti.name for s in skip) else ti)
    return buf.getvalue()


async def setup(src: str) -> None:
    runner = await AsyncSandbox.create(
        "prized-base-v1", vcpu=4, memory_mb=8192, disk_gb=30,
        labels={**LABELS, "role": "runner"}, max_runtime_min=MAX_RUNTIME_MIN,
        auto_pause_min=30, auto_delete_min=120, api_key=_key(),
    )
    (HERE / "runner.id").write_text(runner.id + "\n")
    print("runner", runner.id, flush=True)
    await runner.files.write("/home/prized/take.py", (HERE / "take.py").read_bytes())
    await runner.files.write("/home/prized/shim.tgz", _tar_dir(HERE / "shim", "shim"))
    await runner.files.write("/home/prized/hp.tgz", _tar_dir(pathlib.Path(src), "harbor-prized"))
    out = await _sh(runner, (
        "set -e; cd ~; mkdir -p rpc/req rpc/resp rpc/taken jobs; tar -xzf shim.tgz; tar -xzf hp.tgz; "
        "python3 --version; command -v uv || (curl -LsSf https://astral.sh/uv/install.sh | sh) >/dev/null 2>&1; "
        "export PATH=$HOME/.local/bin:$PATH; "
        f"uv tool install -q --python 3.12 '{HARBOR}' --with ./harbor-prized 2>&1 | tail -3; "
        "harbor --version || true; ls ~/.local/share/uv/tools/harbor/lib/*/site-packages | grep -i -E '^(harbor|prized)' "
    ), timeout=900)
    print(out, flush=True)
    await push_code()
    out = await _sh(runner, (
        f"set -e; cd ~; rm -rf tb21; git clone -q {TB_REPO} tb21 && git -C tb21 checkout -q {TB_COMMIT} && "
        "git -C tb21 log -1 --format='tb21 %H %cI' && ls tb21/tasks | grep -v README | wc -l"
    ), timeout=900)
    print(out, flush=True)


async def push_code() -> None:
    """(Re)uploads forksearch/ and tests/ to ~/fork-search in the runner."""
    runner = await _runner()
    data = _tar_dir(TASK, "fork-search")
    await runner.files.write("/home/prized/fs.tgz", data)
    print(await _sh(runner, "cd ~ && rm -rf fork-search && tar -xzf fs.tgz && ls fork-search && sha256sum fork-search/forksearch/*.py"), flush=True)


class Broker:
    def __init__(self, runner: AsyncSandbox, log: pathlib.Path, spool: str = SPOOL) -> None:
        self.runner, self.spool = runner, spool
        self.boxes: dict[str, AsyncSandbox] = {}
        self.made: list[str] = []
        self.log = open(log, "a", buffering=1)
        self.inflight: set[asyncio.Task] = set()

    def note(self, **row) -> None:
        row["t"] = round(time.time(), 3)
        self.log.write(json.dumps(row) + "\n")

    def live(self) -> int:
        return sum(1 for b in self.boxes.values() if b.state not in ("deleted", "deleting"))

    async def copy(self, a: dict) -> dict:
        """Copies a file from one sandbox to another through this broker, a
        part at a time, so the runner never holds it."""
        src, dst = self.boxes.get(a.get("src", "")), self.boxes.get(a.get("dst", ""))
        if src is None or dst is None:
            raise PrizedError("not_found", "broker has no such sandbox", 404)
        q = shlex.quote
        sp, dp = a["src_path"], a["dst_path"]
        t = time.monotonic()
        r = await src.exec(f"stat -c %s {q(sp)}", timeout=60, full_output=True)
        size = int((r.stdout or "0").strip() or 0)
        parts = 1
        if size <= CHUNK:
            await dst.files.write(dp, await src.files.read_bytes(sp))
        else:
            r = await src.exec(f"split -b {CHUNK} -d -a 4 {q(sp)} {q(sp)}.part && ls {q(sp)}.part*", timeout=1800, full_output=True)
            names = (r.stdout or "").split()
            parts = len(names)
            try:
                for i, name in enumerate(names):
                    await dst.files.write(f"{dp}.part{i:04d}", await src.files.read_bytes(name))
                joined = " ".join(q(f"{dp}.part{i:04d}") for i in range(parts))
                r = await dst.exec(f"cat {joined} > {q(dp)}", timeout=1800, full_output=True)
                if r.exit_code != 0:
                    raise PrizedError("broker_error", f"assembling {dp} failed: {r.stderr[-300:]}")
            finally:
                await src.exec(f"rm -f {q(sp)}.part*", timeout=120, full_output=True)
                await dst.exec(f"rm -f {q(dp)}.part*", timeout=120, full_output=True)
        sec = round(time.monotonic() - t, 3)
        self.note(op="copy", src=src.id, dst=dst.id, bytes=size, parts=parts, sec=sec)
        return {"bytes": size, "parts": parts, "broker_sec": sec}

    async def dispatch(self, op: str, a: dict):
        if op == "copy":
            return await self.copy(a)
        if op == "create":
            if self.live() >= MAX_LIVE:
                raise PrizedError("sandbox_quota_exceeded", f"broker limit: {MAX_LIVE} live sandboxes")
            kw = {k: a[k] for k in ("vcpu", "memory_mb", "disk_gb", "auto_pause_min", "auto_delete_min", "timeout") if a.get(k) is not None}
            kw["disk_gb"] = min(int(kw.get("disk_gb") or 30), MAX_DISK_GB)
            kw["max_runtime_min"] = min(int(a.get("max_runtime_min") or MAX_RUNTIME_MIN), MAX_RUNTIME_MIN)
            kw["labels"] = {**(a.get("labels") or {}), **LABELS}
            box = await AsyncSandbox.create(a.get("template") or "prized-base-v1", api_key=_key(), **kw)
            self.boxes[box.id] = box
            self.made.append(box.id)
            self.note(op="create", id=box.id, vcpu=kw.get("vcpu"), memory_mb=kw.get("memory_mb"), disk_gb=kw["disk_gb"])
            return {"id": box.id, "state": box.state}
        box = self.boxes.get(a.get("sandbox", ""))
        if box is None:
            raise PrizedError("not_found", f"broker has no sandbox {a.get('sandbox')}", 404)
        if op == "exec":
            t = time.monotonic()
            r = await box.exec(a["command"], timeout=a.get("timeout"), full_output=True)
            self.note(op="exec", id=box.id, sec=round(time.monotonic() - t, 3), exit=r.exit_code, cmd=a["command"][:80])
            return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.exit_code}
        if op == "write":
            await box.files.write(a["path"], base64.b64decode(a["data"]), mode=a.get("mode"))
            return None
        if op == "read":
            return base64.b64encode(await box.files.read_bytes(a["path"])).decode()
        if op == "fork":
            if self.live() + int(a.get("count") or 1) > MAX_LIVE:
                raise PrizedError("sandbox_quota_exceeded", f"broker limit: {MAX_LIVE} live sandboxes")
            labels = {**(a.get("labels") or box.labels or {}), **LABELS}
            t = time.monotonic()
            copies = await box.fork(int(a.get("count") or 1), labels=labels)
            sec = time.monotonic() - t
            for c in copies:
                self.boxes[c.id] = c
                self.made.append(c.id)
            self.note(op="fork", id=box.id, copies=[c.id for c in copies], sec=round(sec, 3))
            return [{"id": c.id, "state": c.state} for c in copies]
        if op == "keep_alive":
            await box.keep_alive()
            return None
        if op == "pause":
            await box.pause()
            self.note(op="pause", id=box.id)
            return None
        if op == "delete":
            await box.delete()
            self.note(op="delete", id=box.id)
            return None
        if op == "close":
            await box.close()
            self.boxes.pop(box.id, None)
            return None
        raise PrizedError("broker_error", f"unknown op {op}")

    async def handle(self, req: dict) -> None:
        rid = req["id"]
        try:
            answer = {"id": rid, "result": await self.dispatch(req["op"], req.get("args") or {})}
        except CommandTimeoutError as exc:
            answer = {"id": rid, "error": {"type": "CommandTimeoutError", "message": str(exc)}}
        except PrizedError as exc:
            answer = {"id": rid, "error": {"type": "PrizedError", "code": exc.code, "status": exc.status, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001
            answer = {"id": rid, "error": {"type": "PrizedError", "code": "broker_error", "message": repr(exc)}}
        if "error" in answer:
            self.note(op=req["op"], error=answer["error"])
        for attempt in range(5):
            try:
                await self.runner.files.write(f"{self.spool}/resp/{rid}.json", json.dumps(answer).encode())
                return
            except PrizedError as exc:
                self.note(op="respond", error=str(exc), attempt=attempt)
                await asyncio.sleep(1 + attempt)

    async def serve(self, done_marker: str) -> None:
        while True:
            r = await self.runner.exec(f"python3 /home/prized/take.py {self.spool}; test -e {done_marker} && echo @done", timeout=90, full_output=True)
            lines = (r.stdout or "").splitlines()
            for line in lines:
                if not line.startswith("{"):
                    continue
                req = json.loads(line)
                if "ref" in req:
                    req = json.loads(await self.runner.files.read_bytes(req["ref"]))
                task = asyncio.create_task(self.handle(req))
                self.inflight.add(task)
                task.add_done_callback(self.inflight.discard)
            if "@done" in lines and not self.inflight:
                return


async def run(name: str, harbor_args: list[str]) -> None:
    RUNS.mkdir(exist_ok=True)
    runner = await _runner()
    done = f"/home/prized/jobs/{name}.done"
    args = " ".join(f"'{x}'" for x in harbor_args)
    spool = f"/home/prized/rpc-{name}"
    await _sh(runner, f"rm -f {done}; mkdir -p {spool}/req {spool}/resp {spool}/taken")
    cmd = (
        f"cd ~ && export PATH=$HOME/.local/bin:$PATH PYTHONPATH=/home/prized/shim:/home/prized/fork-search PRIZED_API_KEY=keyless-broker "
        f"PRIZED_BROKER_SPOOL={spool}; {os.environ.get('HARBOR_BIN', 'harbor')} run --jobs-dir jobs --job-name {name} {args} "
        f"> jobs/{name}.log 2>&1; echo $? > jobs/{name}.exit; touch {done}"
    )
    await runner.exec(cmd, background=True)
    broker = Broker(runner, RUNS / f"{name}.broker.jsonl", spool)
    t = time.monotonic()
    try:
        await broker.serve(done)
    finally:
        left = [i for i, b in broker.boxes.items() if b.state not in ("deleted",)]
        print(f"{name}: harbor done in {time.monotonic() - t:.0f}s; made {len(broker.made)} sandboxes; handles open {len(left)}", flush=True)
    print(await _sh(runner, f"cat ~/jobs/{name}.exit; tail -30 ~/jobs/{name}.log"))
    await fetch(name)


async def fetch(name: str) -> None:
    runner = await _runner()
    await _sh(runner, f"cd ~/jobs && tar -czf /home/prized/{name}.tgz {name} {name}.log {name}.exit")
    data = await runner.files.read_bytes(f"/home/prized/{name}.tgz")
    out = RUNS / name
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(out, filter="data")
    print("fetched", out)


async def sh(cmd: str, timeout: float) -> None:
    runner = await _runner()
    r = await runner.exec(cmd, timeout=timeout, full_output=True)
    print(r.stdout[-20000:])
    if r.stderr:
        print("--stderr--", r.stderr[-6000:])
    print("exit", r.exit_code)


async def cleanup() -> None:
    gone = await AsyncSandbox.delete_by_labels(LABELS, api_key=_key())
    print("deleted", gone)
    left = await AsyncSandbox.list(labels=LABELS, api_key=_key())
    print("left", [(s.id, s.state) for s in left])


def main() -> None:
    cmd = sys.argv[1]
    if cmd == "setup":
        asyncio.run(setup(sys.argv[2]))
    elif cmd == "run":
        i = sys.argv.index("--")
        asyncio.run(run(sys.argv[2], sys.argv[i + 1:]))
    elif cmd == "fetch":
        asyncio.run(fetch(sys.argv[2]))
    elif cmd == "push":
        asyncio.run(push_code())
    elif cmd == "sh":
        asyncio.run(sh(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 900))
    elif cmd == "cleanup":
        asyncio.run(cleanup())
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
