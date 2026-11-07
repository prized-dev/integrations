"""Model-free task classification for fork-search (PREREG section 2: T1, T2, P).

    harbor run -p TB21/tasks -a oracle -k 2 -e forksearch.classify_env:ClassifyEnvironment \\
        --ek shapes=/home/prized/shapes.json --ek labels=agent=agent-xp,task=...

A harbor-prized environment for Harbor's oracle agent (the reference solution;
no model). Per trial, with A the sandbox the oracle runs in:

* ``start()``: A starts as harbor-prized starts it. B, a fresh environment of
  the same task (its own sandbox, same image and shape), starts alongside and
  waits. A start marker is written on A once A is ready.
* When Harbor's verifier starts on A (its reward-file cleanup or the upload of
  the tests, the trigger ``verifier_fork`` uses), A is forked twice before any
  verifier step reaches it: C (control) and K (kill). The verifier then runs on
  A, unchanged, as in Harbor's shared mode: Harbor's reward is the oracle's own.
* Meanwhile, in the background:
  - C: the workdir is listed (manifest: type, mode, size, sha256, link target),
    files changed outside the workdir since the start marker are counted, and
    the workdir is packed with tar.
  - B (T1, the transplant): gets that tar; its workdir is emptied and the tar
    extracted, then listed.
  - C (the identity control): its workdir is emptied and the same tar
    extracted, then listed. Only the workdir's trip through tar differs from A,
    so C must pass wherever A passes; if it doesn't, the transplant itself is
    suspect and the task is excluded.
  - K (stratum P): every process the trial left running is killed.
* ``stop()``: the verifier steps Harbor ran on A are replayed on C, K and B at
  once, each into ``verifier-<role>/`` of the trial dir and bounded by the
  task's verifier timeout. ``classify.json`` in the trial dir records verdicts,
  timings, sizes, manifest checks and errors. Then B, C, K and A are deleted.

``shapes`` (``--ek shapes=PATH``) is a JSON map task -> {"image_headroom_gb",
"min_vcpu", "digest"}: each sandbox's disk is the task's storage plus its
measured image. ``mirror`` (``--ek mirror=mirror.gcr.io``) pulls the task's
image from that registry, pinned by the digest its Docker Hub tag had
("digest"), instead of by tag from Docker Hub, whose anonymous limit is 100
pulls an hour per IP. The digest names the same bytes on both registries.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import shlex
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import prized
from harbor_prized import PrizedEnvironment
from harbor_prized import _environment as hp
from prized import PrizedError

ROLES = ("control", "kill", "transplant")
TAR = f"{hp._XFER}/fs-workdir.tar"  # inside the image
HOST_TAR = hp._ROOT + TAR  # the same file, as the sandbox sees it
MARKER = f"{hp._HOME}/fs-start-marker"
START_TRIES = 3
START_BACKOFF_SEC = (60, 180)
FORK_TIMEOUT_SEC = 120
PREPARE_TIMEOUT_SEC = 3600
REPLAY_SLACK_SEC = 120
DEFAULT_VERIFIER_TIMEOUT_SEC = 900.0

# Host-side (sandbox python3, outside the image). argv: root out
MANIFEST_PY = r"""
import hashlib, json, os, stat, sys
root, out = sys.argv[1], sys.argv[2]
lines, errors = [], []
def entry(path, rel):
    try:
        st = os.lstat(path)
    except OSError as e:
        return f"{rel}\t?\t\t\t\t\t"
    m = st.st_mode
    mode = format(stat.S_IMODE(m), "o")
    if stat.S_ISLNK(m):
        return f"{rel}\tl\t{mode}\t\t\t{os.readlink(path)}\t"
    if stat.S_ISDIR(m):
        return f"{rel}\td\t{mode}\t\t\t\t"
    if stat.S_ISREG(m):
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            d = h.hexdigest()
        except OSError:
            d = "unreadable"
        return f"{rel}\tf\t{mode}\t{st.st_size}\t{d}\t\t{int(st.st_mtime)}"
    kind = "s" if stat.S_ISSOCK(m) else "p" if stat.S_ISFIFO(m) else "o"
    return f"{rel}\t{kind}\t{mode}\t\t\t\t"
lines.append(entry(root, "."))
for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: errors.append(str(e))):
    dirnames.sort()
    for name in sorted(dirnames + filenames):
        p = os.path.join(dirpath, name)
        lines.append(entry(p, os.path.relpath(p, root)))
lines.sort()
body = "\n".join(lines) + "\n"
with open(out, "w") as f:
    f.write(body)
content = "\n".join("\t".join(l.split("\t")[:6]) for l in lines) + "\n"
files = [l.split("\t") for l in lines if l.split("\t")[1] == "f"]
print(json.dumps({
    "entries": len(lines), "files": len(files),
    "bytes": sum(int(f[3] or 0) for f in files),
    "sockets": sum(1 for l in lines if l.split("\t")[1] == "s"),
    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    "walk_errors": errors[:5],
}))
"""

# Host-side: files under the image root changed (ctime) since the start marker,
# outside the workdir. argv: root marker workdir xfer
OUTSIDE_PY = r"""
import json, os, sys
root, marker, wd, xfer = sys.argv[1:5]
since = os.stat(marker).st_mtime
skip = {os.path.join(root, p.lstrip("/")) for p in ("/proc", "/sys", "/dev", xfer, wd)}
groups, total, nbytes = {}, 0, 0
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) not in skip]
    for name in dirnames + filenames:
        p = os.path.join(dirpath, name)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if st.st_ctime <= since:
            continue
        rel = "/" + os.path.relpath(p, root)
        key = "/".join(rel.split("/")[:3])
        groups[key] = groups.get(key, 0) + 1
        total += 1
        nbytes += st.st_size if not os.path.isdir(p) else 0
top = sorted(groups.items(), key=lambda kv: -kv[1])[:25]
print(json.dumps({"changed": total, "bytes": nbytes, "top": top}))
"""

# Host-side: kills every process in the trial's user namespaces (all of the
# trial's commands and what they left running), keeping the namespace holders
# and their launchers so later commands still run.
KILL_PY = r"""
import json, os, signal, time
me = os.readlink("/proc/self/ns/user")
KEEP = ("runtime-ns", "runtime-nsu", "unshare", "nsenter")
def trial_procs():
    out = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            ns = os.readlink(f"/proc/{p}/ns/user")
            with open(f"/proc/{p}/comm") as f:
                comm = f.read().strip()
            with open(f"/proc/{p}/stat") as f:
                state = f.read().rpartition(")")[2].split()[0]
        except OSError:
            continue
        if ns != me and comm not in KEEP and state not in "ZX":
            out.append([int(p), comm])
    return out
before = trial_procs()
killed = []
for _ in range(5):
    procs = trial_procs()
    if not procs:
        break
    for pid, comm in procs:
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append([pid, comm])
        except OSError:
            pass
    time.sleep(0.5)
print(json.dumps({"before": before, "killed": killed, "left": trial_procs()}))
"""

# The same fixup verifier_fork makes in its fork: the copy's new hostname.
HOSTNAME_FIXUP = (
    f"old=$(cat {hp._HOSTNAME} 2>/dev/null || cat {hp._ROOT}/etc/hostname); "
    'new=$(hostname); if [ -n "$old" ] && [ "$old" != "$new" ]; then '
    f'sed -i "s/[[:space:]]$old\\$/\\t$new $old/" {hp._ROOT}/etc/hosts || exit 1; '
    f'if [ "$(cat {hp._ROOT}/etc/hostname)" = "$old" ]; then '
    f'echo "$new" > {hp._ROOT}/etc/hostname || exit 1; fi; fi'
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _labels(value) -> dict[str, str]:
    if isinstance(value, str):
        return dict(p.split("=", 1) for p in value.split(",") if "=" in p)
    return dict(value or {})


def _task_name(environment_name: str, environment_dir: Path) -> str:
    name = (environment_name or "").rsplit("/", 1)[-1]
    return name or Path(environment_dir).parent.name


def _verifier_timeout(environment_dir: Path) -> float:
    try:
        with open(Path(environment_dir).parent / "task.toml", "rb") as f:
            value = tomllib.load(f).get("verifier", {}).get("timeout_sec")
        return float(value) if value else DEFAULT_VERIFIER_TIMEOUT_SEC
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return DEFAULT_VERIFIER_TIMEOUT_SEC


def _json_line(text: str | None) -> dict:
    for line in reversed((text or "").strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"unparsed": (text or "")[-500:]}


def manifest_diff(ref: str, other: str, limit: int = 20) -> dict:
    """Content differences (path, type, mode, size, sha256, link) between two
    manifests, and how many files differ only in mtime."""
    def rows(text: str) -> dict[str, list[str]]:
        out = {}
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 7:
                out[parts[0]] = parts
        return out

    a, b = rows(ref), rows(other)
    missing = sorted(set(a) - set(b))
    extra = sorted(set(b) - set(a))
    changed = sorted(p for p in set(a) & set(b) if a[p][1:6] != b[p][1:6])
    mtime_only = sum(
        1 for p in set(a) & set(b) if a[p][1:6] == b[p][1:6] and a[p][6] != b[p][6]
    )
    sockets_missing = sum(1 for p in missing if a[p][1] == "s")
    return {
        "missing": len(missing),
        "sockets_missing": sockets_missing,
        "extra": len(extra),
        "changed": len(changed),
        "mtime_only": mtime_only,
        "examples": (
            [f"missing {p} ({a[p][1]})" for p in missing[:limit]]
            + [f"extra {p}" for p in extra[:limit]]
            + [f"changed {p}: {a[p][1:6]} -> {b[p][1:6]}" for p in changed[:limit]]
        )[:limit],
    }


class ClassifyEnvironment(PrizedEnvironment):
    """See the module doc. Only Harbor's agent environment (session
    ``<trial>__env``) classifies; any other session is a plain PrizedEnvironment."""

    def __init__(self, *args, shapes: str | None = None, mirror: str | None = None, **kwargs):
        kwargs.pop("verifier_fork", None)  # this environment does its own forking
        self._task = _task_name(kwargs.get("environment_name", ""), kwargs["environment_dir"])
        shape = {}
        if shapes:
            table = json.loads(Path(shapes).read_text())
            shape = table.get(self._task) or {}
        if "image_headroom_gb" in shape:
            kwargs["image_headroom_gb"] = int(shape["image_headroom_gb"])
        if "min_vcpu" in shape:
            kwargs["min_vcpu"] = int(shape["min_vcpu"])
        image = kwargs["task_env_config"].docker_image
        pinned = None
        if mirror and shape.get("digest") and image:
            repo = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
            pinned = f"{mirror.rstrip('/')}/{repo}@{shape['digest']}"
            kwargs["task_env_config"] = kwargs["task_env_config"].model_copy(update={"docker_image": pinned})
        labels = _labels(kwargs.get("labels"))
        session_id = kwargs["session_id"]
        self._role = "agent" if session_id.endswith("__env") else "other"
        kwargs["labels"] = {**labels, "fs-role": self._role}
        super().__init__(*args, **kwargs)
        self._phase = "agent"  # agent, verifier (running on A), done
        self._steps: list[tuple[str, dict]] = []
        self._copies: dict[str, PrizedEnvironment] = {}
        self._prepare_task: asyncio.Task | None = None
        self._fresh_task: asyncio.Task | None = None
        self._fresh: PrizedEnvironment | None = None
        self._verifier_timeout_sec = _verifier_timeout(kwargs["environment_dir"])
        self._cls: dict = {
            "task": self._task,
            "session": session_id,
            "shape_table": shape,
            "verifier_timeout_sec": self._verifier_timeout_sec,
            "image": image,
            "image_ref": pinned or image,
        }
        if self._role == "agent":
            fresh_kwargs = dict(kwargs)
            fresh_kwargs["session_id"] = session_id[: -len("__env")] + "__transplant"
            fresh_kwargs["labels"] = {**labels, "fs-role": "transplant"}
            self._fresh = PrizedEnvironment(*args, **fresh_kwargs)

    # -- start ------------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        if self._role != "agent":
            await super().start(force_build)
            return
        self._fresh_task = asyncio.create_task(self._start_fresh())
        t = time.monotonic()
        await super().start(force_build)
        rec = self._cls
        rec["agent_sandbox"] = self._sandbox.id if self._sandbox else None
        rec["agent_start_sec"] = round(time.monotonic() - t, 3)
        vcpu, memory_mb, disk_gb = self._shape()
        rec["shape"] = {"vcpu": vcpu, "memory_mb": memory_mb, "disk_gb": disk_gb}
        rec["workdir"] = self._workdir()
        q = shlex.quote
        try:
            r = await self._host_exec(
                f"touch {MARKER}; echo image_mb=$(du -sm {hp._ROOT} 2>/dev/null | cut -f1); "
                f"echo disk_mb=$(df -Pm {hp._HOME} | tail -1 | awk '{{print $2}}'); "
                f"echo free_mb=$(df -Pm {hp._HOME} | tail -1 | awk '{{print $4}}'); "
                f"echo digest=$(cd {hp._HOME} && ./crane digest {q(self.task_env_config.docker_image or '')} 2>/dev/null)",
                timeout_sec=600,
            )
            vals = dict(line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
            for key in ("image_mb", "disk_mb", "free_mb"):
                rec[key] = int(vals[key]) if vals.get(key, "").isdigit() else None
            rec["image_digest"] = vals.get("digest") or None
        except (PrizedError, RuntimeError) as exc:
            rec["image_measure_error"] = repr(exc)[:300]

    async def _start_fresh(self) -> None:
        assert self._fresh is not None
        errors = self._cls.setdefault("fresh_start_errors", [])
        t = time.monotonic()
        for attempt in range(START_TRIES):
            try:
                await self._fresh.start(force_build=False)
                self._cls["fresh_sandbox"] = self._fresh._sandbox.id if self._fresh._sandbox else None
                self._cls["fresh_start_sec"] = round(time.monotonic() - t, 3)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 (retried, then reported)
                errors.append(repr(exc)[:500])
                if attempt + 1 < START_TRIES:
                    await asyncio.sleep(START_BACKOFF_SEC[attempt])
        raise RuntimeError(f"the fresh environment did not start: {errors[-1]}")

    def _workdir(self) -> str:
        return self.task_env_config.workdir or self._image_workdir

    # -- the verifier on A ------------------------------------------------

    async def _verifier_starts(self, command: str | None = None, target: str | None = None) -> None:
        if self._role != "agent" or self._phase != "agent":
            return
        starts = (target is not None and hp._under(target, hp._TESTS_DIR)) or (
            command is not None
            and command.startswith("rm -f -- ")
            and f"{hp._VERIFIER_DIR}/reward." in command
        )
        if starts:
            self._phase = "verifier"
            await self._snapshot()

    def _remember(self, kind: str, **step: object) -> None:
        if self._role == "agent" and self._phase == "verifier" and not self._in_transfer:
            self._steps.append((kind, step))

    def _verifier_ends(self) -> None:
        if self._role != "agent" or self._phase != "verifier":
            return
        self._phase = "done"
        started = self._cls.pop("_verifier_t0", None)
        if isinstance(started, float):
            self._cls["oracle_verifier_sec"] = round(time.monotonic() - started, 3)

    async def _snapshot(self) -> None:
        """Forks A into C and K before the verifier's first step reaches A."""
        rec = self._cls
        rec["verifier_started_at"] = _now()
        agent = self._require_sandbox()
        t = time.monotonic()
        try:
            forks = await agent.fork(
                2, labels={**self._labels, "fs-role": "copy"}, timeout=FORK_TIMEOUT_SEC
            )
        except PrizedError as exc:
            rec["fork_error"] = str(exc)[:500]
            rec["_verifier_t0"] = time.monotonic()
            return
        rec["fork_sec"] = round(time.monotonic() - t, 3)
        for role, sandbox in zip(("control", "kill"), forks):
            self._copies[role] = self._clone(sandbox, role)
        rec["copies"] = {role: env._sandbox.id for role, env in self._copies.items()}
        rec["_verifier_t0"] = time.monotonic()
        self._prepare_task = asyncio.create_task(self._prepare())

    def _clone(self, sandbox, role: str) -> "ClassifyEnvironment":
        """This environment's view (image env, users, workdir) on another sandbox."""
        clone = copy.copy(self)
        clone._role = role
        clone._sandbox = sandbox
        clone._agent_sandbox = clone._fork_sandbox = None
        clone._fork_heartbeat = None
        clone._heartbeat = asyncio.create_task(self._keep_alive(sandbox))
        return clone

    # -- background: pack, transplant, restore, kill ----------------------

    async def _prepare(self) -> None:
        rec = self._cls
        control, kill = self._copies["control"], self._copies["kill"]
        wd = self._workdir()
        q = shlex.quote
        t0 = time.monotonic()
        fixups = await asyncio.gather(
            *(env._host_exec(HOSTNAME_FIXUP, timeout_sec=60) for env in (control, kill)),
            return_exceptions=True,
        )
        rec["fixup_ok"] = [not isinstance(r, BaseException) and r.return_code == 0 for r in fixups]
        # what the oracle changed outside the workdir, and the reference manifest
        outside = await control._host_exec(
            f"python3 - {q(hp._ROOT)} {MARKER} {q(wd)} {hp._XFER} <<'PY'\n{OUTSIDE_PY}\nPY",
            timeout_sec=1200,
        )
        rec["outside_workdir"] = _json_line(outside.stdout)
        rec["manifest_ref"] = await self._manifest(control, "ref")
        packed = await control.exec(
            f"tar -C {q(wd)} -cf {TAR} .", cwd="/", timeout_sec=1800, user=0
        )
        ok = hp.tar_pack_succeeded(packed.return_code, stdout=packed.stdout, stderr=packed.stderr)
        rec["pack"] = {
            "ok": ok,
            "exit": packed.return_code,
            "stderr": (packed.stderr or "")[-1000:],
            "sec": round(time.monotonic() - t0, 3),
        }
        if not ok:
            raise RuntimeError(f"packing the workdir failed: {(packed.stderr or '')[-300:]}")
        size = await control._host_exec(f"stat -c %s {HOST_TAR}; sha256sum {HOST_TAR} | cut -c1-64", timeout_sec=600)
        parts = (size.stdout or "").split()
        rec["tar_bytes"] = int(parts[0]) if parts and parts[0].isdigit() else None
        rec["tar_sha256"] = parts[1] if len(parts) > 1 else None
        results = await asyncio.gather(
            self._transplant(control),
            self._restore(control, "control"),
            self._kill(kill),
            return_exceptions=True,
        )
        for name, result in zip(("transplant", "control", "kill"), results):
            if isinstance(result, BaseException):
                rec.setdefault("prepare_errors", {})[name] = repr(result)[:800]
        await control._host_exec(f"rm -f {HOST_TAR}", timeout_sec=60)
        rec["prepare_sec"] = round(time.monotonic() - t0, 3)

    async def _transplant(self, control: "ClassifyEnvironment") -> None:
        assert self._fresh is not None and self._fresh_task is not None
        rec = self._cls
        await self._fresh_task  # raises if the fresh environment never started
        fresh = self._fresh
        t = time.monotonic()
        fn = getattr(prized, "broker_copy", None)
        if fn is not None:
            info = await fn(control._sandbox.id, HOST_TAR, fresh._sandbox.id, HOST_TAR)
        else:
            data = await control._get(HOST_TAR)
            await fresh._put(HOST_TAR, data)
            info = {"bytes": len(data)}
        check = await fresh._host_exec(f"sha256sum {HOST_TAR} | cut -c1-64", timeout_sec=600)
        info["sha256_ok"] = (check.stdout or "").strip() == rec.get("tar_sha256")
        info["sec"] = round(time.monotonic() - t, 3)
        rec["copy"] = info
        if not info["sha256_ok"]:
            raise RuntimeError("the tar's sha256 differs after the copy")
        await self._restore(fresh, "transplant")
        await fresh._host_exec(f"rm -f {HOST_TAR}", timeout_sec=60)

    async def _restore(self, env: PrizedEnvironment, role: str) -> None:
        """Empties the workdir and extracts the tar into it, then lists it."""
        wd = shlex.quote(self._workdir())
        t = time.monotonic()
        r = await env.exec(
            f"set -e; mkdir -p {wd}; find {wd} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +; "
            f"tar -C {wd} -xf {TAR}",
            cwd="/",
            timeout_sec=1800,
            user=0,
        )
        self._cls[f"restore_{role}"] = {
            "exit": r.return_code,
            "stderr": (r.stderr or "")[-800:],
            "sec": round(time.monotonic() - t, 3),
        }
        if r.return_code != 0:
            raise RuntimeError(f"restoring the workdir in {role} failed: {(r.stderr or '')[-300:]}")
        self._cls[f"manifest_{role}"] = await self._manifest(env, role)

    async def _manifest(self, env: PrizedEnvironment, name: str) -> dict:
        out = f"{hp._HOME}/fs-manifest-{name}.tsv"
        r = await env._host_exec(
            f"python3 - {shlex.quote(hp._ROOT + self._workdir())} {out} <<'PY'\n{MANIFEST_PY}\nPY",
            timeout_sec=1800,
        )
        info = _json_line(r.stdout)
        info["path"] = out
        return info

    async def _kill(self, env: "ClassifyEnvironment") -> None:
        t = time.monotonic()
        r = await env._host_exec(f"python3 - <<'PY'\n{KILL_PY}\nPY", timeout_sec=120)
        info = _json_line(r.stdout)
        info["sec"] = round(time.monotonic() - t, 3)
        self._cls["kill"] = info

    # -- stop: replays, record, cleanup -----------------------------------

    async def stop(self, delete: bool) -> None:
        if self._role != "agent":
            await super().stop(delete)
            return
        cancelled = False
        try:
            await self._finish()
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:  # noqa: BLE001 (classification never fails the trial)
            self._cls["finish_error"] = repr(exc)[:800]
        finally:
            await hp._despite_cancel(self._cleanup())
            self._write_record()
            await super().stop(delete)
        if cancelled:
            raise asyncio.CancelledError

    async def _finish(self) -> None:
        rec = self._cls
        rec["oracle_verifier_completed"] = self._phase == "done"
        if self._prepare_task is not None:
            try:
                await asyncio.wait_for(self._prepare_task, timeout=PREPARE_TIMEOUT_SEC)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                rec["prepare_error"] = repr(exc)[:800]
        rec["oracle_rewards"] = hp._read_rewards(self.trial_paths.verifier_dir)
        if self._phase != "done" or not self._steps:
            rec["replays"] = "skipped: the oracle's verifier did not complete"
            return
        errors = rec.get("prepare_errors", {})
        targets = {}
        if "control" in self._copies and "control" not in errors and "prepare_error" not in rec:
            targets["control"] = self._copies["control"]
        if "kill" in self._copies and "kill" not in errors and rec.get("kill"):
            targets["kill"] = self._copies["kill"]
        if self._fresh is not None and "transplant" not in errors and "prepare_error" not in rec and rec.get("copy"):
            targets["transplant"] = self._fresh
        results = await asyncio.gather(*(self._replay(role, env) for role, env in targets.items()))
        rec["replays"] = dict(zip(targets, results))
        await self._compare_manifests()

    async def _replay(self, role: str, env: PrizedEnvironment) -> dict:
        out = self.trial_paths.trial_dir / f"verifier-{role}"
        limit = self._verifier_timeout_sec + REPLAY_SLACK_SEC
        t = time.monotonic()
        result: dict = {}
        try:
            async with asyncio.timeout(limit):
                for kind, step in self._steps:
                    if kind == "exec":
                        await env.exec(**step)
                    elif kind == "upload_tar":
                        await env._upload_tar(step["data"], step["target_dir"])
                    elif kind == "upload_file":
                        await env._upload_bytes(**step)
                    elif kind == "download_dir" and hp._under(str(step["source_dir"]), hp._VERIFIER_DIR):
                        await env.download_dir(str(step["source_dir"]), out)
            result["rewards"] = hp._read_rewards(out)
        except TimeoutError:
            result["timeout"] = True
            result["rewards"] = hp._read_rewards(out)
        except Exception as exc:  # noqa: BLE001
            result["error"] = repr(exc)[:800]
            result["rewards"] = hp._read_rewards(out)
        result["sec"] = round(time.monotonic() - t, 3)
        return result

    async def _compare_manifests(self) -> None:
        rec = self._cls
        ref = rec.get("manifest_ref") or {}
        for role in ("control", "transplant"):
            m = rec.get(f"manifest_{role}") or {}
            if not m or not ref:
                continue
            same = bool(m.get("content_sha256")) and m.get("content_sha256") == ref.get("content_sha256")
            rec[f"manifest_{role}"]["same_as_ref"] = same
            if same:
                continue
            try:
                ref_text = (await self._copies["control"]._get(ref["path"])).decode(errors="replace")
                env = self._fresh if role == "transplant" else self._copies["control"]
                other = (await env._get(m["path"])).decode(errors="replace")
                rec[f"manifest_{role}"]["diff"] = manifest_diff(ref_text, other)
            except Exception as exc:  # noqa: BLE001
                rec[f"manifest_{role}"]["diff_error"] = repr(exc)[:300]

    async def _cleanup(self) -> None:
        if self._prepare_task is not None and not self._prepare_task.done():
            self._prepare_task.cancel()
            with contextlib.suppress(BaseException):
                await self._prepare_task
        if self._fresh_task is not None and not self._fresh_task.done():
            self._fresh_task.cancel()
            with contextlib.suppress(BaseException):
                await self._fresh_task
        for env in self._copies.values():
            if env._heartbeat is not None:
                env._heartbeat.cancel()
            if env._sandbox is not None:
                await env._stop_sandbox(env._sandbox, True)
                env._sandbox = None
        if self._fresh is not None:
            with contextlib.suppress(Exception):
                await self._fresh.stop(delete=True)

    def _write_record(self) -> None:
        rec = {k: v for k, v in self._cls.items() if not k.startswith("_")}
        rec["finished_at"] = _now()
        try:
            (self.trial_paths.trial_dir / "classify.json").write_text(json.dumps(rec, indent=1, default=str) + "\n")
        except OSError as exc:
            self.logger.warning(f"classify: could not write classify.json: {exc}")


__all__ = ["ClassifyEnvironment", "manifest_diff"]
