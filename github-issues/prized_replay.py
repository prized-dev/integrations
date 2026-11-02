#!/usr/bin/env python3
"""prized-replay: a replay of each agent run, linked from the pull request. Used by `prized_issues.py --replay`.

The poller runs the agent under this recorder. On one clock it keeps the agent's own event stream and, through
ptrace (fork, exec and exit events only, so the agent runs at full speed), every process the agent starts, with its
command line and exit code. Once the turn is committed it adds what changed. Secret values are gathered in memory
(the environment of every process the agent started, the worktree's .env files, the usual credential files in the
home directory, this poller's GitHub tokens), masked along with known key formats, and only then is anything
written: a bundle and one self-contained page under ~/.prized-issues/replays. The page is served on the box's
loopback only, for a members-only Prized port share (`prized ports share 7707`).

  python3 prized_replay.py serve [--port 7707]   # the replay server (the poller's systemd unit runs this)
  python3 prized_replay.py probe                 # can this box trace the agent's commands?

Standard library only; Linux.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import gzip
import json
import os
import re
import secrets
import select
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get("HOME") or str(Path.home()))
STATE_DIR = Path(os.environ.get("PRIZED_ISSUES_HOME") or str(HOME / ".prized-issues"))
PORT = int(os.environ.get("PRIZED_ISSUES_REPLAY_PORT") or 7707)
NAME_RE = re.compile(r"[A-Za-z0-9._-]{8,200}")
KEEP_DAYS = 30
GRACE = 1.5  # seconds a turn's leftover processes get to end on their own once the agent has exited
STREAM_MAX = 64 * 1024 * 1024  # stream bytes kept for the replay (the run log keeps everything)
ARGV_MAX = 1 << 20
RESULT_MAX = 6000
TEXT_MAX = 4000
DIFF_MAX = 200 * 1024
DIFFS_MAX = 2 * 1024 * 1024

# ---------------------------------------------------------------- redaction

SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSW|PASS$|_PW$|CREDENTIAL|AUTH(?!OR)|PRIVATE|COOKIE|DSN|DATABASE_URL|BASE_URL)", re.I
)
# Names that look secret but hold settings: git's -c pairs (GIT_CONFIG_KEY_0=core.hooksPath), sockets, session ids.
NOT_SECRET_NAME = re.compile(
    r"^(GIT_CONFIG_(KEY|VALUE|COUNT|PARAMETERS)(_\d+)?|SSH_AUTH_SOCK|GPG_AGENT_INFO|XDG_\w+|"
    r"\w*SESSION_ID|\w*_KEYBOARD\w*|KEYMAP|PYTHONHASHSEED|PWD|OLDPWD)$",
    re.I,
)
# A secret-ish word that ends the name or is followed by _...: GITHUB_TOKEN, token_secret, "password" (not tokenizer).
_KW = r"(?:SECRET|TOKEN|PASSWORD|PASSWD|PASS|PWD|API_?KEY|PRIVATE_KEY|ACCESS_KEY|CLIENT_SECRET|AUTH)"
ASSIGN = re.compile(
    r"(?i)(?P<pre>(?<![A-Za-z0-9])[A-Za-z0-9_]*" + _KW + r"(?:_[A-Za-z0-9_]*)?[\"']?\s*[=:]\s*[\"']?)"
    r"(?P<val>[^\s\"'*\[\],;{}()]{8,})"
)
KEY_FORMATS = [
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    ("github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("prized", re.compile(r"\bdcp_[A-Za-z0-9_]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
]
PATTERNS = [
    *KEY_FORMATS,
    # a key block, or the inside of one when only part of it is shown (no BEGIN line): long base64 lines
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|\Z)"
        ),
    ),
    ("key-material", re.compile(r"(?m)^[ \t\u2502|>+\-]*[A-Za-z0-9+/]{56,}={0,2}[ \t]*$")),
    ("url-password", re.compile(r"(?<=://)[^:/\s@]+:[^@\s/]{3,}(?=@)")),
    ("assignment", ASSIGN),
]
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
CRED_NAME = re.compile(r"token|secret|credential|passw|apikey|api_key|private|signing|auth|\.key$|\.pem$", re.I)
CRED_FILES = (
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".aws/credentials",
    ".config/gh/hosts.yml",
    ".docker/config.json",
    ".codex/auth.json",
    ".claude/.credentials.json",
    ".kube/config",
    ".config/gcloud/application_default_credentials.json",
)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache", "dist", "build", "target"}


def secret_env_items(raw: bytes) -> set[tuple[str, str]]:
    """(name, value) for the secret-looking variables in a NUL-separated environ block."""
    out = set()
    for kv in raw.split(b"\0"):
        k, _, v = kv.decode("utf-8", "replace").partition("=")
        if SECRET_NAME.search(k) and not NOT_SECRET_NAME.match(k) and len(v) >= 8:
            out.add((k, v))
    return out


def looks_secret(v: str) -> bool:
    """A value found next to a secret-ish name: keep it only if it isn't a word, a path or a plain URL."""
    if len(v) < 10 or v.startswith(("/", "./", "~/", "$", "%")) or re.fullmatch(r"[a-z_.-]+", v):
        return False
    if "://" in v and not re.search(r"://[^/\s:@]+:[^@\s]+@", v):
        return False
    return bool(re.search(r"\d", v)) or bool(re.search(r"[A-Z]", v) and re.search(r"[a-z]", v))


def shaped(v: str) -> bool:
    """A value that looks generated: mixed case and digits, or 20+ letters and digits. Not a URL, path or word."""
    if not looks_secret(v) or " " in v:
        return False
    if re.search(r"[A-Z]", v) and re.search(r"[a-z]", v) and re.search(r"\d", v):
        return True
    return len(v) >= 20 and bool(re.search(r"\d", v)) and bool(re.search(r"[A-Za-z]", v))


def kv_values(text: str, every: bool = True) -> set[str]:
    """KEY=VALUE lines -> values of 8+ characters. every=False keeps only secret-looking ones (a .env also holds
    harmless settings such as PORT or a hostname, and masking those everywhere would wreck the replay)."""
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.replace("export ", "").strip()
        try:
            v = (shlex.split(v, comments=True) or [""])[0]
        except ValueError:
            v = v.strip().strip("'\"")
        if len(v) < 8:
            continue
        if every or SECRET_NAME.search(k) or shaped(v) or re.search(r"://[^/\s:@]+:[^@\s]+@", v):
            out.add(v)
    return out


def variants(v: str) -> set[str]:
    """The forms a value takes on its way to a screen: itself, each line of it, CR LF line ends, JSON and URL
    escaping, and base64 at each of the three alignments (curl -u, Basic auth, k8s secrets)."""
    out = {v}
    if "\n" in v:
        out.add(v.replace("\n", "\r\n"))
        out |= {ln.strip() for ln in v.splitlines() if len(ln.strip()) >= 8}
    out.add(json.dumps(v)[1:-1])
    out.add(urllib.parse.quote(v, safe=""))
    raw = v.encode()
    for k in range(3):
        enc = base64.b64encode(b"\0" * k + raw).decode().rstrip("=")
        core = enc[(4 * k + 2) // 3 : len(enc) - 2]
        if len(core) >= 12:
            out.add(core)
    return {x for x in out if len(x) >= 8}


def json_strings(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from json_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from json_strings(v)
    elif isinstance(o, str):
        yield o


def read_small(path: Path, limit: int = 65536) -> str:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def dotenv_values(work: Path, sources: set[str]) -> set[str]:
    """Values from a worktree's .env files that look like secrets: by name, by shape, or a URL with a password."""
    out: set[str] = set()
    for root, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in files:
            if n == ".env" or n.startswith(".env.") or n.endswith(".env"):
                vals = kv_values(read_small(Path(root) / n, 1 << 20), every=False)
                if vals:
                    sources.add(f"file:{os.path.relpath(os.path.join(root, n), work)}")
                out |= vals
    return out


def home_secret_values(home: Path, sources: set[str]) -> set[str]:
    """Credentials kept in the home directory: the usual files, and small files under ~/.config whose names say they
    hold a secret (~/.config/<tool>/token, .../signing.key). A secret in a file named like anything else is not found
    this way (only the key-format rules can catch it)."""
    out: set[str] = set()
    paths = [home / p for p in CRED_FILES]
    base = home / ".config"
    for root, dirs, files in os.walk(base):
        if root[len(str(base)) :].count(os.sep) >= 3:
            dirs[:] = []
        paths += [Path(root) / n for n in files if CRED_NAME.search(n)]
    for p in paths:
        text = read_small(p)
        if not text:
            continue
        before = len(out)
        if p.suffix == ".json":
            with contextlib.suppress(ValueError):
                out |= {v for v in json_strings(json.loads(text)) if len(v) >= 16 and looks_secret(v)}
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) == 1 and len(lines[0]) >= 16 and " " not in lines[0] and "=" not in lines[0]:
            out.add(lines[0])  # a bare token file
        out |= kv_values(text, every=False)
        for m in re.finditer(r"(?:password|_authToken|oauth_token|auth)\W+([^\s\"',]{12,})", text):
            out.add(m.group(1))
        if len(out) > before:
            sources.add("file:~/" + str(p.relative_to(home)) if str(p).startswith(str(home)) else "file:" + str(p))
    return out


class Redactor:
    """Masks every known secret value (and its encodings) and every known key format in text."""

    def __init__(self, values=()):
        self.base = {v for v in values if isinstance(v, str) and len(v) >= 8}
        self.counts: dict[str, int] = {}
        self.values: list[str] = []
        self._build()

    def _build(self):
        vs: set[str] = set()
        for v in self.base:
            vs |= variants(v)
        self.values = sorted(vs, key=len, reverse=True)

    def harvest(self, text: str) -> int:
        """Values written next to a secret-ish name anywhere (export X_TOKEN=..., "password": "..."): mask them
        everywhere, so a later bare echo of the same value is caught too."""
        text = ANSI.sub("", text or "")
        added = {m.group("val") for m in ASSIGN.finditer(text) if looks_secret(m.group("val"))} - self.base
        if added:
            self.base |= added
            self._build()
        return len(added)

    def _hit(self, rule: str) -> None:
        self.counts[rule] = self.counts.get(rule, 0) + 1

    def text(self, s):
        """s with secrets masked. Terminal escape codes go first: a value split by one is still found."""
        if not isinstance(s, str) or not s:
            return s
        s = ANSI.sub("", s)
        for v in self.values:
            if v in s:
                self._hit("value")
                s = s.replace(v, "[redacted]")
        for name, rx in PATTERNS:

            def sub(m, name=name):
                pre = m.groupdict().get("pre") or ""
                secret = m.group(0)[len(pre) :]
                if name == "assignment" and not looks_secret(secret):
                    return m.group(0)
                self._hit(name)
                return pre + f"[redacted:{name}]"

            s = rx.sub(sub, s)
        return s

    def obj(self, o):
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, list):
            return [self.obj(x) for x in o]
        if isinstance(o, tuple):
            return tuple(self.obj(x) for x in o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        return o

    def leaks(self, text: str) -> list[str]:
        """What a finished page or bundle still holds: a known value, or a well-formed key. Empty when clean."""
        found = ["a secret value" for v in self.values if v in text][:1]
        return found + [name for name, rx in KEY_FORMATS if rx.search(text)]


# ---------------------------------------------------------------- ptrace

PTRACE_TRACEME, PTRACE_CONT, PTRACE_DETACH, PTRACE_SYSCALL = 0, 7, 17, 24
PTRACE_SETOPTIONS, PTRACE_GETEVENTMSG, PTRACE_GETSIGINFO, PTRACE_GET_SYSCALL_INFO = 0x4200, 0x4201, 0x4202, 0x420E
O_SYSGOOD, O_FORK, O_VFORK, O_CLONE, O_EXEC, O_EXIT = 0x1, 0x2, 0x4, 0x8, 0x10, 0x40
EV_FORK, EV_VFORK, EV_CLONE, EV_EXEC, EV_EXIT = 1, 2, 3, 4, 6
WALL = 0x40000000
# execve and execveat by audit arch: x86_64, aarch64, i386
EXECVE_NR = {0xC000003E: (59, 322), 0xC00000B7: (221, 281), 0x40000003: (11, 358)}
AT_FDCWD = -100
# Programs that exec others without forking first: they stop at syscalls too, so a setuid program they start is seen.
EXECERS = {
    "bash",
    "sh",
    "dash",
    "zsh",
    "ash",
    "ksh",
    "mksh",
    "fish",
    "busybox",
    "env",
    "nice",
    "nohup",
    "stdbuf",
    "setsid",
    "timeout",
    "time",
    "xargs",
    "chrt",
    "ionice",
    "taskset",
    "flock",
}


class SyscallInfo(ctypes.Structure):
    _fields_ = [
        ("op", ctypes.c_uint8),
        ("pad", ctypes.c_uint8 * 3),
        ("arch", ctypes.c_uint32),
        ("ip", ctypes.c_uint64),
        ("sp", ctypes.c_uint64),
        ("nr", ctypes.c_uint64),
        ("args", ctypes.c_uint64 * 6),
        ("more", ctypes.c_uint64 * 2),
    ]


_LIBC: list = []


def libc():
    if not _LIBC:
        lib = ctypes.CDLL(None, use_errno=True)
        lib.ptrace.restype = ctypes.c_long
        lib.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
        _LIBC.append(lib)
    return _LIBC[0]


def read_bytes(path: str):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def tgid_of(tid: int) -> int:
    s = read_bytes(f"/proc/{tid}/status")
    m = re.search(rb"^Tgid:\s*(\d+)", s or b"", re.M)
    return int(m.group(1)) if m else tid


def ppid_of(pid: int):
    s = read_bytes(f"/proc/{pid}/stat")
    return int(s[s.rfind(b")") + 2 :].split()[1]) if s else None


def exe_name(pid: int) -> str:
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe")).removesuffix(" (deleted)")
    except OSError:
        return ""


def privileged(path: str) -> bool:
    """A program that gains privileges when it starts (setuid, setgid, file capabilities). Traced, it would start
    without them, so sudo would fail; the recorder lets such a program run untraced instead."""
    try:
        if os.stat(path).st_mode & (stat.S_ISUID | stat.S_ISGID):
            return True
    except OSError:
        return False
    try:
        os.getxattr(path, "security.capability")
        return True
    except OSError:
        return False


def probe() -> dict:
    """Whether this box lets the recorder trace the agent: ptrace of its own children, and syscall info (to let
    setuid programs like sudo run untraced)."""
    if os.environ.get("PRIZED_ISSUES_REPLAY_PTRACE") == "0":
        return {
            "ptrace": False,
            "sysinfo": False,
            "why": "process tracing is turned off (PRIZED_ISSUES_REPLAY_PTRACE=0)",
        }
    if not sys.platform.startswith("linux"):
        return {"ptrace": False, "sysinfo": False, "why": "process tracing needs Linux"}
    scope = (read_bytes("/proc/sys/kernel/yama/ptrace_scope") or b"").strip().decode() or "?"
    lib = libc()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover (child)
        try:
            os.close(r)
            ok = lib.ptrace(PTRACE_TRACEME, 0, None, None) == 0
            os.write(w, b"T" if ok else b"U")
            if ok:
                os.kill(os.getpid(), signal.SIGSTOP)
        finally:
            os._exit(0)
    os.close(w)
    traced = os.read(r, 1) == b"T"
    os.close(r)
    sysinfo = False
    if traced:
        _, st = os.waitpid(pid, WALL)
        if os.WIFSTOPPED(st):
            info = SyscallInfo()
            sysinfo = (
                lib.ptrace(PTRACE_GET_SYSCALL_INFO, pid, ctypes.c_void_p(ctypes.sizeof(info)), ctypes.byref(info)) > 0
            )
            os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, WALL)
    if not traced:
        why = f"this box doesn't let a program trace its own children (kernel.yama.ptrace_scope={scope})"
        return {"ptrace": False, "sysinfo": False, "why": why}
    why = "" if sysinfo else "this kernel can't report system calls to the recorder, so setuid programs (sudo) fail"
    return {"ptrace": True, "sysinfo": sysinfo, "why": why}


# ---------------------------------------------------------------- one turn


class Recording:
    """One agent turn: the agent runs under it, and what the replay needs stays in memory until it is redacted."""

    def __init__(self, *, extra_secrets=(), workdir: Path | None = None, trace: bool | None = None):
        caps = probe() if trace is None or trace else {"ptrace": False, "sysinfo": False, "why": ""}
        self.trace = bool(caps["ptrace"])
        self.sysinfo = bool(caps["sysinfo"])
        self.notes = [caps["why"]] if caps["why"] else []
        self.workdir = Path(workdir) if workdir else None
        self.t0 = self.t1 = 0.0
        self.stream: list[tuple[float, bytes]] = []
        self.stream_bytes = 0
        self.procs: list[dict] = []
        self.cpu: list[list] = []
        self.stats = {"stops": 0, "execs": 0, "untraced": 0, "killed_leftovers": 0}
        self.errors: list[str] = []
        self.sources: set[str] = set()
        self.values: set[str] = {v for v in extra_secrets if isinstance(v, str) and len(v) >= 8}
        if extra_secrets:
            self.sources.add("the poller's GitHub tokens")
        self.prompt = ""
        self.rc = None
        self.root = 0
        self._red: Redactor | None = None
        self._exited: set[int] = set()
        self._live: set[int] = set()
        self._lock = threading.Lock()

    # -- secrets, gathered in memory only

    def gather(self, env: dict | None = None) -> None:
        if env:
            for k, v in secret_env_items(b"\0".join(f"{k}={v}".encode() for k, v in env.items())):
                self.values.add(v)
                self.sources.add("env:" + k)
        if self.workdir:
            self.values |= dotenv_values(self.workdir, self.sources)
        self.values |= home_secret_values(HOME, self.sources)

    def redactor(self) -> Redactor:
        """Every value gathered, plus every value the turn wrote next to a secret-ish name anywhere."""
        if self._red is None:
            red = Redactor(self.values)
            red.harvest(self.prompt)
            red.harvest(b"\n".join(line for _, line in self.stream).decode("utf-8", "replace"))
            red.harvest("\n".join(" ".join(p.get("argv") or []) for p in self.procs))
            self._red = red
        return self._red

    def redact(self, text: str) -> str:
        return self.redactor().text(text)

    def forget(self) -> None:
        """Drop the raw recording and the secret values (after the replay is written)."""
        self.stream, self.procs, self.values, self._red, self.prompt = [], [], set(), None, ""

    # -- running the turn

    def run(self, cmd: list[str], cwd: Path, env: dict, stdin: bytes, log, timeout: float) -> int:
        """Run cmd like subprocess with start_new_session: stdin given, stdout and stderr to `log` (a binary file),
        killed after `timeout` (exit -9). Returns its exit code (-N for a signal). Raises OSError when it can't start."""
        path = env.get("PATH", os.defpath)
        if "/" not in cmd[0] and not any(os.access(os.path.join(d, cmd[0]), os.X_OK) for d in path.split(os.pathsep)):
            raise FileNotFoundError(2, "No such file or directory", cmd[0])
        self.prompt = stdin.decode("utf-8", "replace")
        self.gather(env)
        lib = libc() if self.trace else None
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        st_r, st_w = os.pipe()
        self.t0 = time.time()
        pid = os.fork()
        self.root = pid
        if pid == 0:  # pragma: no cover (child)
            try:
                os.setsid()
                os.dup2(in_r, 0)
                os.dup2(out_w, 1)
                os.dup2(out_w, 2)
                os.dup2(st_w, 3)
                os.closerange(4, 65536)
                for fd in (in_r, in_w, out_r, out_w, st_r, st_w):
                    if fd > 3:
                        with contextlib.suppress(OSError):
                            os.close(fd)
                for s in (signal.SIGPIPE, signal.SIGXFSZ):
                    signal.signal(s, signal.SIG_DFL)
                os.chdir(cwd)
                traced = lib is not None and lib.ptrace(PTRACE_TRACEME, 0, None, None) == 0
                os.write(3, b"T" if traced else b"U")
                os.close(3)
                if traced:
                    os.kill(os.getpid(), signal.SIGSTOP)
                os.execvpe(cmd[0], cmd, env)  # noqa: S606 (an argument list, as subprocess would)
            except BaseException as e:
                with contextlib.suppress(OSError):
                    os.write(2, f"couldn't start {cmd[0]}: {e}\n".encode())
            os._exit(127)
        for fd in (in_r, out_w, st_w):
            os.close(fd)
        traced = os.read(st_r, 1) == b"T"
        os.close(st_r)
        if self.trace and not traced:
            self.notes.append("the agent couldn't be traced, so commands' exit codes weren't recorded")
        self.trace = traced
        stop_pump = threading.Event()
        pump = threading.Thread(target=self._pump, args=(out_r, log, stop_pump), daemon=True)
        pump.start()
        feeder = threading.Thread(target=self._feed, args=(in_w, stdin), daemon=True)
        feeder.start()
        sampling = threading.Event()
        sampler = threading.Thread(target=self._sample, args=(pid, sampling), daemon=True)
        sampler.start()
        timed_out = threading.Event()

        def kill_all():
            timed_out.set()
            with self._lock:
                pids = list(self._live)
            for p in pids:
                with contextlib.suppress(OSError):
                    os.kill(p, signal.SIGKILL)
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)

        timer = threading.Timer(timeout, kill_all)
        timer.daemon = True
        timer.start()
        cpu0 = time.process_time()
        try:
            status = self._trace(pid) if traced else self._wait(pid)
        finally:
            timer.cancel()
            sampling.set()
            stop_pump.set()
            pump.join(5)
            feeder.join(1)
            with contextlib.suppress(OSError):
                os.close(out_r)
        self.t1 = time.time()
        self.stats["recorder_cpu_s"] = round(time.process_time() - cpu0, 3)
        if timed_out.is_set():
            self.rc = -9
        elif os.WIFSIGNALED(status):
            self.rc = -os.WTERMSIG(status)
        else:
            self.rc = os.WEXITSTATUS(status)
        if self.workdir:
            self.values |= dotenv_values(self.workdir, self.sources)  # written during the turn
        self.values |= home_secret_values(HOME, self.sources)
        return self.rc

    def _feed(self, fd: int, data: bytes) -> None:
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _pump(self, fd: int, log, stop: threading.Event) -> None:
        """The agent's output: every byte to the run log as before, and each line, timestamped, to the recording."""
        buf, stop_at = b"", None
        while True:
            if stop.is_set() and stop_at is None:
                stop_at = time.monotonic() + 2  # a process that still holds the pipe can't hold up the run
            if stop_at is not None and time.monotonic() > stop_at:
                break
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                if stop.is_set():
                    break
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            with contextlib.suppress(OSError, ValueError):
                log.write(chunk)
                log.flush()
            t = time.time()
            *lines, buf = (buf + chunk).split(b"\n")
            for line in lines:
                self._keep(t, line)
            if len(buf) > 1 << 20:
                self._keep(t, buf)
                buf = b""
        if buf:
            self._keep(time.time(), buf)

    def _keep(self, t: float, line: bytes) -> None:
        if self.stream_bytes + len(line) > STREAM_MAX:
            if "the stream was cut" not in " ".join(self.notes):
                self.notes.append(
                    f"the replay keeps the first {STREAM_MAX >> 20} MB of the agent's stream (the run log has all)"
                )
            return
        self.stream_bytes += len(line)
        self.stream.append((t, line))

    def _sample(self, root: int, stop: threading.Event) -> None:
        """CPU and memory of the turn's processes, once a second."""
        clk, page_mb = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE") / 1048576
        last: dict[int, int] = {}
        total = 0.0
        while not stop.wait(1.0):
            with self._lock:
                pids = list(self._live) if self.trace else [root]
            rss = 0
            for pid in pids:
                s = read_bytes(f"/proc/{pid}/stat")
                if not s:
                    continue
                fl = s[s.rfind(b")") + 2 :].split()
                ticks = int(fl[11]) + int(fl[12])
                total += max(0, ticks - last.get(pid, 0)) / clk
                last[pid] = ticks
                rss += int(fl[21])
            self.cpu.append([round(time.time() - self.t0, 2), round(total, 2), round(rss * page_mb, 1)])

    def _wait(self, pid: int) -> int:
        """No tracing: wait for the agent, then end what it left in its session."""
        with self._lock:
            self._live.add(pid)
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
        time.sleep(0.2)
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)
        return status

    # -- the tracer: runs on the thread that forked, which is the tracer of every process in the turn

    def _trace(self, root: int) -> int:
        lib = libc()
        mode: dict[int, bool] = {}  # tid -> stops at system calls (until it execs, and in shells)
        seen: set[int] = set()
        thread: dict[int, int] = {}  # tid -> tgid, for threads that aren't their group's leader
        root_status = None
        root_done = swept = 0.0

        def resume(tid: int, sig: int = 0) -> None:
            lib.ptrace(PTRACE_SYSCALL if mode.get(tid) else PTRACE_CONT, tid, None, ctypes.c_void_p(sig or None))

        def gone(tid: int) -> None:
            mode.pop(tid, None)
            thread.pop(tid, None)
            with self._lock:
                self._live.discard(tid)

        os.waitpid(root, WALL)  # its first stop, before it execs
        opts = O_FORK | O_VFORK | O_CLONE | O_EXEC | O_EXIT | (O_SYSGOOD if self.sysinfo else 0)
        lib.ptrace(PTRACE_SETOPTIONS, root, None, ctypes.c_void_p(opts))
        seen.add(root)
        mode[root] = self.sysinfo
        with self._lock:
            self._live.add(root)
        resume(root)
        while True:
            if root_status is not None:
                with self._lock:
                    left = list(self._live)
                now = time.monotonic()
                if not left or (swept and now - swept > 2):
                    break
                if not swept and now - root_done > GRACE:
                    # The turn is over: end what it left running, so nothing from it is alive while the replay and
                    # the pull request are written (a leftover could otherwise change what the redaction checked).
                    for p in left:
                        with contextlib.suppress(OSError):
                            os.kill(p, signal.SIGKILL)
                    self.stats["killed_leftovers"] = len(left)
                    swept = now
            try:
                w, status = os.waitpid(-1, WALL | (os.WNOHANG if root_status is not None else 0))
            except ChildProcessError:
                break
            if w == 0:
                time.sleep(0.002)  # only once the agent has exited: its leftovers get a moment, then end
                continue
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                if thread.get(w, w) == w:
                    self._on_exit(w, status)
                gone(w)
                if w == root:
                    root_status, root_done = status, time.monotonic()
                continue
            if not os.WIFSTOPPED(status):
                continue
            self.stats["stops"] += 1
            sig, ev = os.WSTOPSIG(status), (status >> 16) & 0xFF
            if sig == signal.SIGTRAP | 0x80:
                try:
                    if self._at_syscall(w, mode):
                        gone(w)  # let go: a setuid program runs untraced
                        continue
                except Exception as e:  # never leave a process stopped over a recording problem
                    self._error(e)
                resume(w)
            elif sig == signal.SIGTRAP and ev:
                try:
                    self._at_event(w, ev, mode, thread)
                except Exception as e:
                    self._error(e)
                resume(w)
            elif w not in seen:  # a new process's or thread's first stop, maybe before its parent's fork event
                seen.add(w)
                try:
                    tg = tgid_of(w)
                    if tg != w:
                        thread[w] = tg
                        mode.setdefault(w, mode.get(tg, False))
                    else:
                        mode.setdefault(w, self.sysinfo)
                        with self._lock:
                            self._live.add(w)
                except Exception as e:
                    self._error(e)
                resume(w, 0 if sig == signal.SIGSTOP else sig)
            else:
                # A signal for the process: deliver it. A group-stop (no signal info): let it run on.
                info = ctypes.create_string_buffer(128)
                resume(w, sig if lib.ptrace(PTRACE_GETSIGINFO, w, None, info) >= 0 else 0)
        return root_status if root_status is not None else 0

    def _error(self, e: BaseException) -> None:
        if len(self.errors) < 5:
            self.errors.append(f"{type(e).__name__}: {e}"[:200])

    def _at_event(self, w: int, ev: int, mode: dict, thread: dict) -> None:
        lib = libc()
        msg = ctypes.c_ulong(0)
        lib.ptrace(PTRACE_GETEVENTMSG, w, None, ctypes.byref(msg))
        if ev in (EV_FORK, EV_VFORK, EV_CLONE):
            new = msg.value
            if ev == EV_CLONE and tgid_of(new) == thread.get(w, w):
                thread[new] = thread.get(w, w)
                mode.setdefault(new, mode.get(w, False))
            else:
                mode.setdefault(new, self.sysinfo)  # stops at system calls until it execs
                with self._lock:
                    self._live.add(new)
        elif ev == EV_EXEC:
            if msg.value != w:  # a thread exec'd: it took the leader's id
                mode.pop(msg.value, None)
                thread.pop(msg.value, None)
            thread.pop(w, None)
            with self._lock:
                self._live.add(w)
            mode[w] = self.sysinfo and exe_name(w) in EXECERS
            self._on_exec(w)
        elif ev == EV_EXIT and thread.get(w, w) == w:
            self._on_exit(w, msg.value)

    def _at_syscall(self, w: int, mode: dict) -> bool:
        """At a system call of a process that hasn't exec'd yet (or a shell): True when it was let go because it is
        about to exec a setuid program."""
        lib = libc()
        info = SyscallInfo()
        if lib.ptrace(PTRACE_GET_SYSCALL_INFO, w, ctypes.c_void_p(ctypes.sizeof(info)), ctypes.byref(info)) <= 0:
            mode[w] = False
            return False
        nrs = EXECVE_NR.get(info.arch)
        if info.op != 1 or not nrs or info.nr not in nrs:
            return False
        at = info.nr == nrs[1]
        ptr = 4 if info.arch == 0x40000003 else 8
        try:
            mem = os.open(f"/proc/{w}/mem", os.O_RDONLY)
        except OSError:
            return False
        try:
            path = self._cstr(mem, info.args[1] if at else info.args[0])
            if at:
                dirfd = ctypes.c_int(info.args[0] & 0xFFFFFFFF).value
                base = os.readlink(f"/proc/{w}/cwd") if dirfd == AT_FDCWD else os.readlink(f"/proc/{w}/fd/{dirfd}")
                full = os.path.join(base, path) if path else base
            else:
                full = path if path.startswith("/") else os.path.join(os.readlink(f"/proc/{w}/cwd"), path)
            if (not path and not at) or not privileged(full):
                return False
            argv = []
            addr = info.args[2] if at else info.args[1]
            for i in range(64):
                raw = os.pread(mem, ptr, addr + i * ptr)
                p = int.from_bytes(raw, sys.byteorder) if len(raw) == ptr else 0
                if not p:
                    break
                argv.append(self._cstr(mem, p))
        finally:
            os.close(mem)
        libc().ptrace(PTRACE_DETACH, w, None, None)
        self.stats["untraced"] += 1
        t = time.time()
        self.procs.append(
            {"t": t, "pid": w, "ppid": ppid_of(w), "argv": argv or [full], "cwd": None, "untraced": True, "t_end": None}
        )
        return True

    @staticmethod
    def _cstr(mem: int, addr: int, limit: int = 4096) -> str:
        try:
            raw = os.pread(mem, limit, addr)
        except OSError:
            return ""
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

    def _on_exec(self, pid: int) -> None:
        raw = read_bytes(f"/proc/{pid}/cmdline")
        argv = [a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")] if raw else []
        if sum(map(len, argv)) > ARGV_MAX:
            argv = [a[:ARGV_MAX] for a in argv][:64]
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = None
        env = read_bytes(f"/proc/{pid}/environ")
        if env:
            for k, v in secret_env_items(env):
                self.values.add(v)
                self.sources.add("env:" + k)
        self.stats["execs"] += 1
        prev = next((p for p in reversed(self.procs) if p["pid"] == pid and p.get("t_end") is None), None)
        t = time.time()
        if prev:  # the same process ran another program
            prev["t_end"], prev["code"] = t, None
        self.procs.append({"t": t, "pid": pid, "ppid": ppid_of(pid), "argv": argv, "cwd": cwd, "t_end": None})

    def _on_exit(self, pid: int, status: int) -> None:
        if pid in self._exited:
            return
        self._exited.add(pid)
        with self._lock:
            self._live.discard(pid)
        p = next((p for p in reversed(self.procs) if p["pid"] == pid and not p.get("untraced")), None)
        if p and p.get("t_end") is None:
            p["t_end"] = time.time()
            p["code"] = -(status & 0x7F) if status & 0x7F else (status >> 8) & 0xFF


# ---------------------------------------------------------------- what the agent said and did

RUN_TOOLS = {"Bash", "shell", "exec_command", "local_shell"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch"}
TEST_CMD = re.compile(
    r"(^|[\s/])(pytest|py\.test)(\s|$)|-m (pytest|unittest)\b|\b(npm|pnpm|yarn|bun) (run )?test\b|"
    r"\bgo test\b|\bcargo test\b|\b(jest|vitest|mocha|rspec|phpunit)\b|\bmake (test|check)\b|\bctest\b|\btox\b"
)
SHELLS = {"bash", "sh", "dash", "zsh", "ash", "ksh", "mksh", "fish", "busybox"}


def category(name: str) -> str:
    return "run" if name in RUN_TOOLS else "edit" if name in EDIT_TOOLS else "read" if name in READ_TOOLS else "other"


def clip(s, n: int):
    if s is None:
        return None
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"\n\u2026 [{len(s) - n} more characters]"


def tool_summary(name: str, inp) -> str:
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return str(inp.get("command", ""))
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return str(inp.get("file_path") or inp.get("notebook_path") or "")
    if name == "Grep":
        return f"{inp.get('pattern', '')}  {inp.get('path', '') or ''}".strip()
    if name == "Glob":
        return str(inp.get("pattern", ""))
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return f"{len(todos)} todos: " + "; ".join(
            str(t.get("content", ""))[:40] for t in todos[:4] if isinstance(t, dict)
        )
    if name in ("Task", "Agent"):
        return str(inp.get("description", ""))
    if name in ("WebFetch", "WebSearch"):
        return str(inp.get("url") or inp.get("query") or "")
    if name in ("shell", "exec_command", "local_shell"):
        cmd = inp.get("command") or inp.get("cmd")
        return " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd or "")
    if name == "apply_patch":
        return str(inp.get("paths") or "")
    return json.dumps(inp, ensure_ascii=False)


def result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text") or (f"[{c.get('type')}]" if c.get("type") else "")))
            else:
                parts.append(str(c))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


def parse_stream(stream: list[tuple[float, bytes]], t0: float) -> tuple[list[dict], dict, list[tuple[float, str]]]:
    """Claude Code's stream-json or `codex exec --json`, timestamped as it arrived: (events, meta, other lines)."""
    events: list[dict] = []
    tools: dict[str, dict] = {}
    meta: dict = {"usage": {}}
    usage_seen: set = set()
    other: list[tuple[float, str]] = []
    for t_abs, raw in stream:
        t = round(t_abs - t0, 3)
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            d = json.loads(line) if line.startswith("{") else None
        except ValueError:
            d = None
        if not isinstance(d, dict):
            other.append((t, line))
            continue
        typ = d.get("type")
        if typ == "system" and d.get("subtype") == "init":
            meta.update(agent="Claude Code", model=d.get("model"), session=d.get("session_id"))
        elif typ == "assistant":
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            meta["agent"] = "Claude Code"
            meta["model"] = msg.get("model") or meta.get("model")
            u = msg.get("usage")
            if isinstance(u, dict) and msg.get("id") not in usage_seen:
                usage_seen.add(msg.get("id"))
                mu = meta["usage"]
                for k, src in (
                    ("input", "input_tokens"),
                    ("output", "output_tokens"),
                    ("cache_read", "cache_read_input_tokens"),
                    ("cache_write", "cache_creation_input_tokens"),
                ):
                    mu[k] = mu.get(k, 0) + int(u.get(src) or 0)
            side = bool(d.get("parent_tool_use_id"))
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and str(c.get("text", "")).strip():
                    events.append({"t": t, "kind": "say", "text": str(c["text"]), "side": side})
                elif c.get("type") == "tool_use":
                    name = str(c.get("name") or "?")
                    ev = {
                        "t": t,
                        "kind": "tool",
                        "id": str(c.get("id")),
                        "name": name,
                        "input": c.get("input"),
                        "summary": tool_summary(name, c.get("input")),
                        "t_end": None,
                        "result": None,
                        "is_error": False,
                        "exit_code": None,
                        "side": side,
                    }
                    tools[ev["id"]] = ev
                    events.append(ev)
        elif typ == "user":
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            content = msg.get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and str(c.get("tool_use_id")) in tools:
                    ev = tools[str(c["tool_use_id"])]
                    ev["t_end"] = t
                    ev["result"] = result_text(c.get("content"))
                    ev["is_error"] = bool(c.get("is_error"))
                    m = re.search(r"\b[Ee]xit code:? (\d+)", ev["result"] or "")
                    if m and ev["name"] in RUN_TOOLS:
                        ev["exit_code"] = int(m.group(1))
        elif typ == "result":
            meta["turns"] = d.get("num_turns")
            meta["cost_usd"] = d.get("total_cost_usd")
        elif typ == "thread.started":
            meta.update(agent="Codex", session=d.get("thread_id"))
        elif typ in ("item.started", "item.updated", "item.completed") and isinstance(d.get("item"), dict):
            meta["agent"] = "Codex"
            item = d["item"]
            it, iid, done = item.get("type"), str(item.get("id")), typ == "item.completed"
            if it in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                ev = tools.get(iid)
                if ev is None:
                    if it == "command_execution":
                        name, inp = "shell", {"command": item.get("command")}
                    elif it == "file_change":
                        paths = [str(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict)]
                        name, inp = "apply_patch", {"paths": ", ".join(paths)}
                    elif it == "mcp_tool_call":
                        name, inp = f"{item.get('server')}.{item.get('tool')}", item.get("arguments")
                    else:
                        name, inp = "WebSearch", {"query": item.get("query")}
                    ev = {
                        "t": t,
                        "kind": "tool",
                        "id": iid,
                        "name": name,
                        "input": inp,
                        "summary": tool_summary(name, inp),
                        "t_end": None,
                        "result": None,
                        "is_error": False,
                        "exit_code": None,
                    }
                    tools[iid] = ev
                    events.append(ev)
                if done:
                    ev["t_end"] = t
                    if it == "command_execution":
                        ev["result"] = str(item.get("aggregated_output") or "")
                        code = item.get("exit_code")
                        ev["exit_code"] = code if isinstance(code, int) else None
                        ev["is_error"] = bool(code) or item.get("status") == "failed"
                    elif it == "mcp_tool_call":
                        ev["result"] = result_text(item.get("result"))
                        ev["is_error"] = item.get("status") == "failed"
                    else:
                        ev["is_error"] = item.get("status") == "failed"
            elif it == "agent_message" and done and str(item.get("text", "")).strip():
                events.append({"t": t, "kind": "say", "text": str(item["text"])})
            elif it == "error" and done:
                events.append({"t": t, "kind": "say", "text": "Error: " + str(item.get("message", ""))})
        elif typ == "turn.completed" and isinstance(d.get("usage"), dict):
            u = d["usage"]
            meta["usage"] = {
                "input": int(u.get("input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0),
                "cache_read": int(u.get("cached_input_tokens") or 0),
            }
    return events, meta, other


# ---------------------------------------------------------------- the bundle, the summary and the page


def tilde(p) -> str:
    s = str(p or "")
    return "~" + s[len(str(HOME)) :] if s == str(HOME) or s.startswith(str(HOME) + "/") else s


def fmt_argv(argv, n: int = 160) -> str:
    if not argv:
        return "?"
    parts = [a if a and not re.search(r"[\s'\"$`|&;<>()]", a) else "'" + a.replace("'", "'\\''") + "'" for a in argv]
    s = re.sub(r"\s+", " ", " ".join(parts))
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def git_out(wt: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
    r = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(wt), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    return r.stdout if r.returncode == 0 else ""


def turn_files(wt: Path, start: str, head: str) -> list[list]:
    """[status, path, added, deleted, diff] for each file the turn changed (start..head)."""
    if not head or not start or head == start:
        return []
    files = []
    stats = {}
    for line in git_out(wt, "diff", "--no-renames", "--numstat", "-z", start, head).split("\0"):
        parts = line.split("\t", 2)
        if len(parts) == 3:
            a, d, path = parts
            stats[path] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
    names = git_out(wt, "diff", "--no-renames", "--name-status", "-z", start, head).split("\0")
    total = 0
    for i in range(0, len(names) - 1, 2):
        status, path = names[i][:1], names[i + 1]
        if not status or not path:
            continue
        diff = ""
        if total < DIFFS_MAX:
            diff = git_out(
                wt,
                "diff",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "-U3",
                start,
                head,
                "--",
                path,
            )
            total += len(diff)
        a, d = stats.get(path, (0, 0))
        files.append([status, path, a, d, diff])
    return files


def summarize(b: dict) -> dict:
    """What the pull request's three lines say, from what the recorder saw (not from what the agent says it did)."""
    tools = [e for e in b["agent"] if e["kind"] == "tool"]
    runs = [e for e in tools if category(e["name"]) == "run"]
    traced = bool(b["meta"].get("ptrace"))
    procs = b["procs"]
    by_pid = {}
    for p in procs:
        by_pid.setdefault(p["pid"], p)
    root = b["meta"].get("root_pid")
    groups = tool_groups(b)
    failed = 0
    for e in runs:
        codes = [by_pid[g]["code"] for g, tid in groups.items() if tid == e["id"] and g in by_pid]
        if traced and codes:
            failed += any(c not in (0, None) for c in codes)
        else:
            failed += bool(e.get("is_error") or e.get("exit_code"))
    out = {"tool_calls": len(tools), "commands": len(runs), "failed": failed, "traced": traced}
    out["processes"] = len([p for p in procs if p["pid"] != root])
    test = None
    if traced:
        hits = [
            p
            for p in procs
            if p.get("argv") and TEST_CMD.search(" ".join(p["argv"])) and os.path.basename(p["argv"][0]) not in SHELLS
        ]
        hit_pids = {p["pid"] for p in hits}

        def nested(p):
            q, n = by_pid.get(p.get("ppid")), 0
            while q and n < 50:
                if q["pid"] in hit_pids:
                    return True
                q, n = by_pid.get(q.get("ppid")), n + 1
            return False

        top = [p for p in hits if not nested(p) and p["pid"] != root]
        if top:
            p = top[-1]
            test = {"cmd": fmt_argv(p["argv"], 60), "code": p.get("code"), "seen": True}
    if test is None:
        for e in runs:
            if TEST_CMD.search(e["summary"] or ""):
                code = e.get("exit_code")
                test = {"cmd": e["summary"].strip().splitlines()[0][:60] if e["summary"].strip() else "?", "code": code}
                test["seen"] = False
                test["failed"] = bool(e.get("is_error")) or bool(code)
    out["tests"] = test
    return out


def md_code(s: str, n: int = 60) -> str:
    s = " ".join(str(s).split()).replace("`", "'")
    return "`" + (s if len(s) <= n else s[: n - 1] + "\u2026") + "`"


def summary_lines(sm: dict, files: list[list]) -> list[str]:
    a, d = sum(f[2] for f in files), sum(f[3] for f in files)
    names = ", ".join(md_code(f[1], 80) for f in files[:4]) + (f" and {len(files) - 4} more" if len(files) > 4 else "")
    l1 = f"**Files changed:** {len(files)} (+{a} \u2212{d})" + (f" \u00b7 {names}" if files else "")
    l2 = f"**Commands run:** {sm['commands']} by the agent" + (
        f", {sm['failed']} exited non-zero" if sm["failed"] else ""
    )
    if sm["traced"]:
        l2 += f" ({sm['processes']} processes in all)"
    t = sm.get("tests")
    if not t:
        l3 = "**Tests:** none run by the agent"
    elif t.get("seen"):
        code = t["code"]
        verdict = "passed" if code == 0 else "still running when the turn ended" if code is None else "FAILED"
        tail = f" exited {code}" if code is not None else ""
        l3 = f"**Tests:** {verdict} \u00b7 {md_code(t['cmd'])}{tail} (exit code seen on the box, not reported by the agent)"
    else:
        verdict = "FAILED" if t.get("failed") else "passed"
        how = f"exit {t['code']}, " if t.get("code") is not None else ""
        l3 = f"**Tests:** {verdict} \u00b7 {md_code(t['cmd'])} ({how}as the agent's tool reported it)"
    return [l1, l2, l3]


def tool_groups(b: dict) -> dict[int, str]:
    """Each command the agent started (a direct child of the agent), matched to the tool call that started it: by
    its command text (Claude Code runs `bash -c "... eval 'CMD'"`, Codex `bash -lc CMD`), else a shell started
    while the call ran. Anything else the agent ran (its own git calls, say) stays the agent's."""
    root = b["meta"].get("root_pid")
    runs = [e for e in b["agent"] if e["kind"] == "tool" and category(e["name"]) == "run"]

    def norm(s: str) -> str:
        return " ".join(re.sub(r"[\\'\"]", "", s).split())

    out = {}
    for p in b["procs"]:
        if p.get("ppid") != root or p["pid"] == root or not p.get("argv"):
            continue
        joined = norm(" ".join(p["argv"]))
        live = []
        for e in runs:
            end = e["t_end"] if e.get("t_end") is not None else e["t"] + 7200
            if e["t"] - 0.5 <= p["t"] <= end + 0.5:
                live.append(e)
        said = [
            e for e in live if norm((e["summary"] or "").strip().split("\n")[0])[:40] in joined and e["summary"].strip()
        ]
        shell = os.path.basename(p["argv"][0]) in SHELLS
        pick = said or (live if shell else [])
        if pick:
            out[p["pid"]] = max(pick, key=lambda e: e["t"])["id"]
    return out


def build(b: dict) -> dict:
    """The page's data: the activity feed, process lane, file snapshots and log, from one redacted bundle."""
    meta, root = b["meta"], b["meta"].get("root_pid")
    procs = b["procs"]
    by_pid = {}
    for p in procs:
        by_pid.setdefault(p["pid"], p)
    groups = tool_groups(b)

    def group_of(p):
        cur, n = p, 0
        while cur and n < 60:
            if cur["pid"] in groups:
                return cur["pid"]
            if cur.get("ppid") == root:
                return None
            cur, n = by_pid.get(cur.get("ppid")), n + 1
        return None

    out_procs, tool_procs = [], {}
    for p in procs:
        if p["pid"] == root:
            continue
        name = os.path.basename((p.get("argv") or ["?"])[0])
        g = group_of(p)
        owner = groups.get(g) if g else None
        wrap = name in SHELLS and any(a in ("-c", "-lc") for a in p.get("argv") or [])
        rec = {
            "t": round(p["t"], 3),
            "te": round(p["t_end"], 3) if p.get("t_end") is not None else None,
            "cmd": fmt_argv(p.get("argv")),
            "code": p.get("code"),
            "who": "tool" if owner else "agent",
            "tool": owner,
            "wrap": wrap,
            "untraced": bool(p.get("untraced")),
        }
        out_procs.append(rec)
        if owner and not wrap:
            tool_procs.setdefault(owner, []).append(rec)

    items = []
    for e in b["agent"]:
        if e["kind"] == "tool":
            ps = tool_procs.get(e["id"], [])
            te = e.get("t_end") if e.get("t_end") is not None else e["t"]
            t_run, te_run = (min(p["t"] for p in ps), max((p["te"] or p["t"]) for p in ps)) if ps else (e["t"], te)
            items.append(
                {
                    "t": t_run,
                    "te": te_run,
                    "kind": "tool",
                    "id": e["id"],
                    "name": e["name"],
                    "cat": category(e["name"]),
                    "summary": e["summary"],
                    "result": e.get("result"),
                    "err": bool(e.get("is_error")),
                    "procs": [{"cmd": p["cmd"], "code": p["code"], "untraced": p["untraced"]} for p in ps],
                }
            )
        else:
            items.append({"t": e["t"], "kind": e["kind"], "text": e.get("text", "")})

    # Files: each one shows from the agent's first edit to it (or the end of the turn), with the turn's final diff.
    duration = meta["duration"]
    first: dict[str, float] = {}
    for e in b["agent"]:
        if e["kind"] == "tool" and category(e["name"]) == "edit":
            for f in b["files"]:
                if f[1] and (str(f[1]) in (e["summary"] or "")):
                    first.setdefault(f[1], e.get("t_end") if e.get("t_end") is not None else e["t"])
    diffs, snaps = [], [{"t": 0, "changed": [], "vs_base": []}]
    entries = []
    for status, path, _, _, diff in b["files"]:
        diffs.append(diff or "(binary or too large to show)")
        entries.append([status, path, len(diffs) - 1, round(first.get(path, duration), 3)])
    for t in sorted({e[3] for e in entries}):
        snaps.append(
            {
                "t": t,
                "changed": [[e[0], e[1]] for e in entries if e[3] == t],
                "vs_base": [e[:3] for e in entries if e[3] <= t],
            }
        )
        items.append({"t": t, "kind": "files", "changed": [[e[0], e[1]] for e in entries if e[3] == t]})
    items.sort(key=lambda i: (i["t"], {"prompt": 0}.get(i["kind"], 1)))
    return {
        "meta": meta,
        "duration": max(duration, 0.1),
        "items": items,
        "procs": out_procs,
        "snaps": snaps,
        "diffs": diffs,
        "files": [],
        "cpu": b["cpu"],
        "stats": b["stats"],
        "log": log_lines(b, items),
        "redactions": b["redactions"],
        "secretValuesChecked": b["secret_values_checked"],
        "secretSources": b["secret_sources"],
    }


def one_line(s, n: int = 140) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def log_lines(b: dict, items: list[dict]) -> list[list]:
    """The agent log pane: one line per step, as a person watching the box would have seen it."""
    meta, ctx = b["meta"], b["context"]
    head = " \u00b7 ".join(
        x
        for x in (
            "prized-issues",
            ctx.get("repo"),
            f"issue #{ctx['issue']}" if ctx.get("issue") else "",
            ctx.get("branch"),
            f"{meta.get('agent') or 'agent'} ({meta.get('mode')})",
        )
        if x
    )
    out = [[0, "hd", head]]
    for it in items:
        t, k = it["t"], it["kind"]
        if k == "prompt":
            first = next((ln for ln in str(it["text"]).splitlines() if ln.strip()), "")
            out.append([t, "dim", "\u25b8 task: " + one_line(first)])
        elif k == "say":
            lines = [ln for ln in str(it["text"]).splitlines() if ln.strip()][:6]
            out += [[t, "say", ("\u25cf " if i == 0 else "  ") + one_line(ln)] for i, ln in enumerate(lines)]
        elif k == "tool":
            mark = {"run": "$", "edit": "\u270e", "read": "\u25e6"}.get(it["cat"], "\u25b8")
            out.append([t, it["cat"], f"{mark} {it['name']}  {one_line(it['summary'], 120)}"])
            te = it["te"] if it.get("te") is not None else t
            if it["cat"] == "run" and it.get("result"):
                lines = [ln for ln in str(it["result"]).splitlines() if ln.strip()]
                out += [[te, "lo", "  \u2502 " + one_line(ln, 130)] for ln in lines[-8:]]
            bad = [p for p in it["procs"] if p["code"]]
            if bad:
                out.append([te, "bad", f"  \u2717 {one_line(bad[-1]['cmd'], 80)} exited {bad[-1]['code']}"])
            elif it["err"]:
                out.append([te, "bad", "  \u2717 failed"])
        elif k == "files":
            out.append([t, "edit", "\u270e files: " + ", ".join(c[1] for c in it["changed"][:6])])
    rc = meta.get("exit_code")
    end = meta["duration"]
    mark = "\u2713" if rc == 0 else "\u2717"
    out.append([end, "ok" if rc == 0 else "bad", f"{mark} agent exited {rc}"])
    out.sort(key=lambda x: x[0])
    return out


def render(page: dict, title: str, context: dict) -> str:
    page = dict(page, title=title, context=context)
    # Every "<" escaped: neither </script nor <!--<script in agent text can end or hide the data block.
    payload = (
        json.dumps(page, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return TEMPLATE.replace("__DATA_JSON__", payload)


def assemble(rec: Recording, files: list[list], context: dict, mode: str) -> tuple[dict, Redactor]:
    """The redacted bundle: everything is masked first, and only then is long text cut (a cut must never leave half a
    secret no rule recognizes)."""
    red = rec.redactor()
    for f in files:
        red.harvest(f[4])
    events, meta, other = parse_stream(rec.stream, rec.t0)
    agent = [{"t": 0.0, "kind": "prompt", "text": rec.prompt}, *events]
    t0 = rec.t0
    procs = [
        {
            "t": round(p["t"] - t0, 3),
            "pid": p["pid"],
            "ppid": p.get("ppid"),
            "argv": p.get("argv") or [],
            "cwd": tilde(p.get("cwd")),
            "t_end": round(p["t_end"] - t0, 3) if p.get("t_end") is not None else None,
            "code": p.get("code"),
            "untraced": bool(p.get("untraced")),
        }
        for p in rec.procs
    ]
    bundle = {
        "v": 1,
        "meta": {
            "t0": t0,
            "duration": round(max(rec.t1 - t0, 0.1), 3),
            "host": socket.gethostname(),
            "workdir": tilde(rec.workdir),
            "root_pid": rec.root,
            "ptrace": rec.trace,
            "exit_code": rec.rc,
            "mode": mode,
            "notes": list(rec.notes),
            "agent": meta.get("agent"),
            "model": meta.get("model"),
            "usage": meta.get("usage") or {},
            "turns": meta.get("turns"),
        },
        "context": context,
        "agent": agent,
        "procs": procs,
        "cpu": rec.cpu,
        "files": [list(f) for f in files],
        "other": [[t, line] for t, line in other],
        "stats": {**rec.stats, "errors": len(rec.errors)},
    }
    bundle = red.obj(bundle)
    for e in bundle["agent"]:
        e.pop("input", None)
        if e.get("text") is not None:
            e["text"] = clip(e["text"], TEXT_MAX)
        if e.get("result") is not None:
            e["result"] = clip(e["result"], RESULT_MAX)
        if e.get("summary") is not None:
            e["summary"] = clip(e["summary"], 2000)
    for p in bundle["procs"]:
        if sum(map(len, p["argv"])) > 8192:
            p["argv"] = [a[:2048] + ("\u2026" if len(a) > 2048 else "") for a in p["argv"]][:64]
    for f in bundle["files"]:
        if len(f[4]) > DIFF_MAX:
            f[4] = f[4][:DIFF_MAX] + "\n\u2026 [the rest of this diff is on the pull request]"
    bundle["other"] = [[t, clip(line, 1000)] for t, line in bundle["other"][-200:]]
    bundle["redactions"] = dict(red.counts)
    bundle["secret_values_checked"] = len(red.base)
    bundle["secret_sources"] = sorted(rec.sources)
    return bundle, red


class Report:
    """One turn's replay: its link, the pull request's three lines, and the page (written by write())."""

    def __init__(
        self,
        rec: Recording,
        *,
        state_dir: Path,
        link: str,
        name: str,
        wt: Path,
        start: str,
        head: str,
        context: dict,
        title: str,
        mode: str,
    ):
        self.name, self.state_dir = name, Path(state_dir)
        self.page_path = self.state_dir / "replays" / "pages" / f"{name}.html"
        self.bundle_path = self.state_dir / "replays" / "bundles" / f"{name}.json.gz"
        self.url = f"{link.rstrip('/')}/r/{name}.html" if link else ""
        self.bundle, self.red = assemble(rec, turn_files(wt, start, head), context, mode)
        self.title = self.red.text(title)
        self.traced = rec.trace
        self.notes = list(self.bundle["meta"]["notes"])
        sm = summarize(self.bundle)
        self.lines = [self.red.text(ln) for ln in summary_lines(sm, self.bundle["files"])]
        self.duration = self.bundle["meta"]["duration"]
        # A last check of what leaves this function: no gathered value and no well-formed key, anywhere.
        self.leaks = self.red.leaks(json.dumps(self.bundle, ensure_ascii=False) + "\n".join(self.lines) + self.title)
        self.written = False

    def write(self, pr_url: str = "", pr: int | None = None) -> bool:
        """Write (or rewrite, once the pull request exists) the bundle and the page. False when withheld."""
        if self.leaks:
            return False
        ctx = dict(self.bundle["context"], pr_url=pr_url or self.bundle["context"].get("pr_url"))
        if pr:
            ctx["pr"] = pr
        html = render(build(self.bundle), self.title, ctx)
        if self.red.leaks(html):
            self.leaks = ["the page"]
            return False
        for path, data in (
            (self.page_path, html.encode()),
            (self.bundle_path, gzip.compress(json.dumps(self.bundle).encode())),
        ):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_name("." + path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        self.written = True
        return True

    def block(self, docs: str) -> str:
        """Markdown for the pull request (or a comment): the link and the three lines."""
        m, s = divmod(int(self.duration), 60)
        if self.leaks:
            head = "**Replay:** withheld, because the recorder found something in it that it couldn't mask."
        elif self.url:
            head = (
                f"**[\u25b6 Replay this run]({self.url})** \u00b7 {m}:{s:02d} of the agent's steps, every command it ran and the "
                "files it changed, secrets masked on the box. Opens for members of the Prized workspace."
            )
        else:
            head = f"**Replay this run:** on the box at `{tilde(self.page_path)}` (no members link yet: [Replay]({docs}#replay))."
        out = head + "\n\n" + "".join(f"- {ln}\n" for ln in self.lines)
        if not self.traced and self.notes:
            out += f"\n_Exit codes are as the agent reported them: {self.notes[0]}._\n"
        return out


def server_up(port: int = PORT) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        return True
    return False


def prune(state_dir: Path, days: int = KEEP_DAYS) -> int:
    """Replays older than `days`: removed. Returns how many files went."""
    cutoff, n = time.time() - days * 86400, 0
    for sub in ("pages", "bundles"):
        d = Path(state_dir) / "replays" / sub
        for p in d.glob("*") if d.is_dir() else []:
            with contextlib.suppress(OSError):
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
    return n


def new_name(repo_key: str, number: int) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{repo_key.lower()}-{number}-{stamp}-{secrets.token_hex(8)}"


# ---------------------------------------------------------------- the server


CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
    "connect-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
PAGE_PATH = re.compile(r"/r/([A-Za-z0-9._-]{8,200})\.html")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


class ReplayHandler(BaseHTTPRequestHandler):
    """GET /r/<name>.html from the pages folder, and nothing else: no listing, no other files."""

    server_version = "prized-replays"
    sys_version = ""
    root = STATE_DIR / "replays" / "pages"

    def do_GET(self):
        self._serve(body=True)

    def do_HEAD(self):
        self._serve(body=False)

    def _serve(self, body: bool) -> None:
        # The share edge and the laptop's mirror both ask for localhost:<port>; any other name is a page somewhere
        # trying to reach this server through DNS rebinding.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        path = urllib.parse.urlsplit(self.path).path
        m = PAGE_PATH.fullmatch(path)
        f = self.root / f"{m.group(1)}.html" if m and host in LOCAL_HOSTS else None
        if f is None or not f.is_file() or f.is_symlink():
            data = b"No replay here. Replays open from the links on pull requests.\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)
            return
        data = f.read_bytes()
        self.send_response(200)
        for k, v in (
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", CSP),
        ):
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(data)

    def log_message(self, format, *args):  # the path only: no query strings, no client details
        sys.stderr.write(f"{self.command} {urllib.parse.urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}\n")


def serve(port: int, root: Path, ready=None) -> None:
    handler = type("Handler", (ReplayHandler,), {"root": Path(root)})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    if ready is not None:
        ready(httpd)
    httpd.serve_forever()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="prized_replay.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="serve replay pages on 127.0.0.1, for a members-only port share")
    s.add_argument("--port", type=int, default=PORT)
    s.add_argument("--root", default=str(STATE_DIR / "replays" / "pages"))
    sub.add_parser("probe", help="can this box trace the agent's commands?")
    args = p.parse_args(argv)
    if args.cmd == "probe":
        print(json.dumps(probe()))
        return 0
    serve(args.port, Path(args.root))
    return 0


# The page: one file, no network (its server sends a CSP that allows none). Adapted from the flight-recorder
# prototype's replay page, with a log pane in place of the terminal.
TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Replay &#xb7; Prized</title>
<style>
:root {
  color-scheme: dark;
  --page: #0f0f0e;
  --surface-1: #1a1a19;
  --surface-2: #222220;
  --surface-3: #2b2b29;
  --line: #2f2f2c;
  --text-primary: #f0efec;
  --text-secondary: #c3c2b7;
  --text-muted: #8f8e86;
  --run: #3987e5;
  --edit: #d95926;
  --read: #199e70;
  --other: #8f8e86;
  --good: #0ca30c;
  --critical: #d03b3b;
  --term-bg: #141413;
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; overflow-x: hidden; background: var(--page); color: var(--text-primary); font: 14px/1.45 var(--sans); -webkit-font-smoothing: antialiased; }
button { font: inherit; color: inherit; }
.app { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; height: 100vh; height: 100dvh; padding: 14px 16px 14px; gap: 12px; }

/* header */
header { display: flex; align-items: center; gap: 18px; min-width: 0; }
.brand { display: flex; align-items: center; gap: 9px; flex: none; }
.mark { width: 26px; height: 26px; border-radius: 7px; background: var(--surface-3); display: grid; place-items: center; }
.mark i { width: 9px; height: 9px; border-radius: 50%; background: #e66767; box-shadow: 0 0 0 3px rgba(230,103,103,.18); }
.brand b { font-weight: 600; letter-spacing: -.01em; }
.brand span { color: var(--text-muted); }
.title { min-width: 0; flex: 1; }
.title h1 { margin: 0; font-size: 15px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; letter-spacing: -.01em; }
.title p { margin: 1px 0 0; color: var(--text-muted); font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chips { display: flex; gap: 6px; flex: none; }
.chip { background: var(--surface-1); border: 1px solid var(--line); border-radius: 999px; padding: 4px 10px; font-size: 12.5px; color: var(--text-secondary); white-space: nowrap; }
.chip strong { color: var(--text-primary); font-weight: 600; }
.chip .plus { color: #4fc26b; } .chip .minus { color: #e66767; }
.share { flex: none; background: var(--text-primary); color: #111; border: 0; border-radius: 8px; padding: 7px 12px; font-weight: 600; font-size: 13px; cursor: pointer; display: flex; gap: 7px; align-items: center; }
.share:hover { background: #fff; }
.share.ghost { background: var(--surface-2); color: var(--text-primary); border: 1px solid var(--line); text-decoration: none; font-weight: 500; }
.share.ghost:hover { background: var(--surface-3); }
.share svg { width: 14px; height: 14px; }

/* main grid */
main { display: grid; grid-template-columns: minmax(0, 1.42fr) minmax(380px, 1fr); grid-template-rows: minmax(0, 1.15fr) minmax(0, 1fr); gap: 12px; min-height: 0; }
.panel { background: var(--surface-1); border: 1px solid var(--line); border-radius: 12px; min-height: 0; min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
.panel > .ph { display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-bottom: 1px solid var(--line); font-size: 12.5px; color: var(--text-muted); flex: none; }
.ph .name { color: var(--text-secondary); font-weight: 600; }
.ph .grow { flex: 1; }
.term-panel { grid-column: 1; grid-row: 1 / span 2; background: var(--term-bg); }
.activity-panel { grid-column: 2; grid-row: 1; }
.diff-panel { grid-column: 2; grid-row: 2; }
.term-wrap { flex: 1; min-height: 0; overflow: auto; padding: 10px 14px 16px; overscroll-behavior: contain; }
.log { font: 12.5px/1.55 var(--mono); color: #e8e6e1; white-space: pre-wrap; word-break: break-word; }
.ll { min-height: 1.55em; }
.ll.future { display: none; }
.ll.hd { color: var(--text-primary); font-weight: 600; }
.ll.dim, .ll.lo { color: var(--text-muted); }
.ll.say { color: #d6d4ce; }
.ll.run { color: #86b6ef; } .ll.edit { color: #f0a07a; } .ll.read { color: #7ad98f; } .ll.other { color: var(--text-secondary); }
.ll.bad { color: #ff8a8a; } .ll.ok { color: #7ad98f; }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); display: inline-block; }
.playing .live-dot { background: #4fc26b; }

/* activity */
* { scrollbar-width: thin; scrollbar-color: #3a3a37 transparent; }
.feed { overflow-y: auto; flex: 1; padding: 6px 0 40vh; scroll-behavior: auto; overscroll-behavior: contain; }
.it { position: relative; padding: 8px 14px 9px 62px; cursor: pointer; transition: opacity .25s, background .2s; }
.it:hover { background: var(--surface-2); }
.it.future { opacity: .32; }
.it.current { background: var(--surface-2); }
.it.current::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px; background: var(--text-primary); }
.it .tm { position: absolute; left: 12px; top: 10px; font: 11px/1 var(--mono); color: var(--text-muted); }
.it .hd { display: flex; align-items: baseline; gap: 8px; }
.it .tag { font-size: 12px; font-weight: 600; color: var(--text-secondary); display: inline-flex; align-items: center; gap: 6px; }
.it .tag i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.it .dur { margin-left: auto; font: 11px var(--mono); color: var(--text-muted); }
.it .cmd { font: 12px/1.45 var(--mono); color: var(--text-primary); margin-top: 3px; white-space: pre-wrap; word-break: break-word; max-height: 7.3em; overflow: hidden; }
.it .txt { margin-top: 3px; color: var(--text-secondary); font-size: 13px; white-space: pre-wrap; word-break: break-word; }
.it .txt code, .it .cmd code { font: 12px var(--mono); background: var(--surface-3); padding: 0 4px; border-radius: 4px; color: var(--text-primary); }
.it.prompt .txt { color: var(--text-primary); }
.it .procs { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.pc { font: 11px/1.3 var(--mono); padding: 2px 6px; border-radius: 5px; background: var(--surface-3); color: var(--text-secondary); max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pc.bad { background: rgba(208,59,59,.16); color: #ff9b9b; }
.pc.bad::after { content: " \00b7  exit " attr(data-code); }
.out { margin-top: 6px; font: 11.5px/1.45 var(--mono); color: var(--text-muted); background: var(--page); border: 1px solid var(--line); border-radius: 7px; padding: 6px 8px; white-space: pre-wrap; word-break: break-word; max-height: 0; opacity: 0; overflow: hidden; padding-top: 0; padding-bottom: 0; border-width: 0; transition: max-height .25s, opacity .2s; }
.it.open .out { max-height: 260px; opacity: 1; overflow: auto; padding-top: 6px; padding-bottom: 6px; border-width: 1px; }
.more { font-size: 11.5px; color: var(--text-muted); margin-top: 5px; display: inline-block; }
.more:hover { color: var(--text-secondary); }
.files-list { margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px; }
.fl { font: 11.5px var(--mono); color: var(--text-primary); background: rgba(217,89,38,.14); border-radius: 5px; padding: 2px 6px; }
.icon { width: 22px; height: 22px; }

/* diff */
.tabs { display: flex; gap: 2px; overflow-x: auto; padding: 6px 8px 0; border-bottom: 1px solid var(--line); flex: none; scrollbar-width: none; }
.tab { border: 0; background: none; color: var(--text-muted); font: 12px var(--mono); padding: 6px 10px 7px; border-radius: 7px 7px 0 0; cursor: pointer; white-space: nowrap; position: relative; }
.tab:hover { color: var(--text-secondary); }
.tab.on { color: var(--text-primary); background: var(--surface-2); }
.tab .n { margin-left: 6px; font-size: 11px; }
.tab .n b { font-weight: 500; color: #4fc26b; } .tab .n s { text-decoration: none; color: #e66767; }
.tab.fresh::after { content: ""; position: absolute; top: 5px; right: 3px; width: 5px; height: 5px; border-radius: 50%; background: var(--edit); }
.diff { flex: 1; overflow: auto; font: 12px/1.6 var(--mono); padding: 6px 0 10px; }
.dl { display: grid; grid-template-columns: 36px 36px 1fr; white-space: pre; min-width: max-content; }
.dl .ln { color: #5d5c57; text-align: right; padding-right: 10px; user-select: none; }
.dl .c { padding: 0 14px 0 6px; }
.dl.add { background: rgba(79,194,107,.10); } .dl.add .c { color: #b9f0c6; }
.dl.del { background: rgba(230,103,103,.10); } .dl.del .c { color: #ffc2c2; }
.dl.add .c::before { content: "+"; color: #4fc26b; margin-right: 6px; }
.dl.del .c::before { content: "\2212 "; color: #e66767; margin-right: 6px; }
.dl.ctx .c::before { content: " "; margin-right: 6px; }
.dl.hunk { color: var(--text-muted); background: var(--surface-2); margin: 6px 0 2px; }
.dl.hunk .c { font-size: 11.5px; }
.empty { color: var(--text-muted); padding: 22px 16px; font-size: 13px; }

/* timeline */
.tl { background: var(--surface-1); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px 12px; display: grid; grid-template-columns: auto 1fr; column-gap: 14px; row-gap: 6px; user-select: none; -webkit-user-select: none; }
.controls { grid-column: 1 / span 2; display: flex; align-items: center; gap: 12px; }
.play { width: 34px; height: 34px; border-radius: 50%; border: 0; background: var(--text-primary); color: #111; display: grid; place-items: center; cursor: pointer; flex: none; }
.play:hover { background: #fff; }
.play svg { width: 14px; height: 14px; }
.clock { font: 13px var(--mono); color: var(--text-secondary); min-width: 110px; }
.clock b { color: var(--text-primary); font-weight: 500; }
.seg { display: flex; background: var(--surface-2); border-radius: 8px; padding: 2px; }
.seg button { border: 0; background: none; color: var(--text-muted); font-size: 12px; padding: 4px 8px; border-radius: 6px; cursor: pointer; }
.seg button.on { background: var(--surface-3); color: var(--text-primary); }
.toggle { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--text-secondary); cursor: pointer; }
.toggle .sw { width: 28px; height: 16px; border-radius: 99px; background: var(--surface-3); position: relative; transition: background .2s; }
.toggle .sw::after { content: ""; position: absolute; left: 2px; top: 2px; width: 12px; height: 12px; border-radius: 50%; background: var(--text-muted); transition: transform .2s, background .2s; }
.toggle.on .sw { background: #3a3a37; } .toggle.on .sw::after { transform: translateX(12px); background: var(--text-primary); }
.legend { margin-left: auto; display: flex; gap: 12px; font-size: 12px; color: var(--text-muted); flex-wrap: wrap; justify-content: flex-end; }
.legend span { display: inline-flex; align-items: center; gap: 5px; }
.legend i { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }
.lanes-l { display: grid; grid-auto-rows: 18px; row-gap: 5px; font-size: 11.5px; color: var(--text-muted); padding-top: 1px; }
.lanes-l div { display: flex; align-items: center; justify-content: flex-end; }
.track { position: relative; display: grid; grid-auto-rows: 18px; row-gap: 5px; cursor: pointer; touch-action: none; }
.lane { position: relative; border-radius: 4px; background: var(--surface-2); }
.lane .m { position: absolute; top: 3px; bottom: 3px; border-radius: 3px; min-width: 4px; box-shadow: 0 0 0 1px var(--surface-2); }
.lane .busy { position: absolute; top: 7px; bottom: 7px; border-radius: 2px; background: #3a3a37; }
.lane .dot { position: absolute; top: 5px; width: 8px; height: 8px; margin-left: -4px; border-radius: 50%; background: var(--text-secondary); box-shadow: 0 0 0 2px var(--surface-2); }
.lane .dot.prompt { background: var(--text-primary); }
.lane .tick { position: absolute; top: 4px; bottom: 4px; width: 2px; margin-left: -1px; border-radius: 1px; background: var(--text-muted); }
.lane .tick.tool { background: #7fb0ee; }
.lane .tick.shell { background: var(--text-primary); }
.lane .tick.bad { background: var(--critical); top: 1px; bottom: 1px; width: 3px; }
.lane .tick.file { background: var(--edit); width: 3px; top: 2px; bottom: 2px; }
.lane svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.idle { position: absolute; top: 0; bottom: 0; background: repeating-linear-gradient(135deg, rgba(255,255,255,.035) 0 4px, transparent 4px 8px); border-left: 1px dashed #3a3a37; border-right: 1px dashed #3a3a37; pointer-events: none; }
.idle span { position: absolute; bottom: -1px; left: 50%; transform: translateX(-50%); font-size: 10.5px; color: var(--text-muted); white-space: nowrap; }
.head { position: absolute; top: -4px; bottom: -4px; width: 2px; margin-left: -1px; background: var(--text-primary); border-radius: 1px; pointer-events: none; box-shadow: 0 0 0 1px rgba(0,0,0,.4); }
.head::before { content: ""; position: absolute; top: -5px; left: -5px; width: 12px; height: 12px; border-radius: 50%; background: var(--text-primary); }
.hover-line { position: absolute; top: 0; bottom: 0; width: 1px; background: rgba(240,239,236,.35); pointer-events: none; display: none; }
.tip { position: fixed; z-index: 10; pointer-events: none; background: #262624; border: 1px solid #3a3a37; border-radius: 8px; padding: 7px 9px; font-size: 12px; max-width: 360px; box-shadow: 0 8px 24px rgba(0,0,0,.45); display: none; }
.tip .tt { font: 11px var(--mono); color: var(--text-muted); }
.tip .tc { font: 12px/1.4 var(--mono); color: var(--text-primary); white-space: pre-wrap; word-break: break-word; max-height: 6em; overflow: hidden; margin-top: 2px; }
.toast { position: fixed; left: 50%; bottom: 28px; transform: translate(-50%, 20px); opacity: 0; background: var(--text-primary); color: #111; padding: 8px 14px; border-radius: 9px; font-weight: 600; font-size: 13px; transition: opacity .2s, transform .2s; pointer-events: none; z-index: 20; }
.toast.on { opacity: 1; transform: translate(-50%, 0); }
.foot { grid-column: 1 / span 2; display: flex; gap: 16px; font-size: 11.5px; color: var(--text-muted); flex-wrap: wrap; }
.foot b { color: var(--text-secondary); font-weight: 500; }

@media (max-width: 1100px) { .chips .opt { display: none; } }
@media (max-width: 820px) {
  html, body { height: auto; }
  .app { display: flex; flex-direction: column; height: auto; min-height: 100dvh; padding: 10px 10px 0; gap: 10px; }
  header { flex-wrap: wrap; gap: 8px 10px; order: 0; }
  .brand span { display: none; }
  .title { order: 2; flex-basis: 100%; }
  .title h1, .title p { white-space: normal; }
  .share { margin-left: auto; padding: 6px 10px; }
  .chips { order: 3; width: 100%; overflow-x: auto; scrollbar-width: none; }
  .chips::-webkit-scrollbar { display: none; }
  main { display: contents; }
  .term-panel { order: 1; flex: none; }
  .term-wrap { flex: none; max-height: 60vh; }
  .tl { order: 2; position: sticky; top: 0; z-index: 5; padding: 8px 10px 10px; column-gap: 8px; box-shadow: 0 8px 18px rgba(0,0,0,.5); }
  .activity-panel { order: 3; flex: none; max-height: 78vh; }
  .diff-panel { order: 4; flex: none; height: 360px; margin-bottom: 10px; }
  .legend, .foot, .lanes-l div:nth-child(4), #lane-cpu { display: none; }
  .controls { gap: 8px; flex-wrap: wrap; }
  .clock { min-width: 0; }
  .lanes-l { font-size: 11px; }
  .ph > span:last-child:not(.name) { display: none; }
  .it { padding-left: 56px; }
  .idle span { display: none; }
}
</style>
</head>
<body>
<div class="app" id="app">
  <header>
    <div class="brand"><div class="mark"><i></i></div><b id="brandname">Replay</b><span>Prized</span></div>
    <div class="title"><h1 id="title"></h1><p id="subtitle"></p></div>
    <div class="chips" id="chips"></div>
    <a class="share ghost" id="back" style="display:none"></a>
    <button class="share" id="share" title="Copy a link to this moment"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M6.5 9.5l3-3M7 4.5l1.2-1.2a2.6 2.6 0 013.7 3.7L10.7 8.2M9 11.5l-1.2 1.2a2.6 2.6 0 01-3.7-3.7L5.3 7.8"/></svg>Copy link</button>
  </header>
  <main id="main">
    <section class="panel term-panel">
      <div class="ph"><span class="live-dot"></span><span class="name" id="termname"></span><span class="grow"></span><span id="termsize"></span></div>
      <div class="term-wrap" id="termwrap"><div class="log" id="log"></div></div>
    </section>
    <section class="panel activity-panel">
      <div class="ph"><span class="name">Activity</span><span id="actcount"></span><span class="grow"></span><span>click any step to jump</span></div>
      <div class="feed" id="feed"></div>
    </section>
    <section class="panel diff-panel">
      <div class="ph"><span class="name">Files</span><span id="diffsub">this turn's changes</span><span class="grow"></span><span id="diffstat"></span></div>
      <div class="tabs" id="tabs"></div>
      <div class="diff" id="diff"></div>
    </section>
  </main>
  <section class="tl" id="tl">
    <div class="controls">
      <button class="play" id="play" aria-label="Play"></button>
      <div class="clock" id="clock"></div>
      <div class="seg" id="speed"><button data-s="1">1&#xd7;</button><button data-s="2" class="on">2&#xd7;</button><button data-s="4">4&#xd7;</button></div>
      <div class="toggle on" id="skip"><span class="sw"></span>Skip idle</div>
      <div class="legend">
        <span><i style="background:var(--run)"></i>Command</span>
        <span><i style="background:var(--edit)"></i>File change</span>
        <span><i style="background:var(--read)"></i>Read or search</span>
        <span><i style="background:var(--critical)"></i>Exit &#x2260; 0</span>
      </div>
    </div>
    <div class="lanes-l"><div>Agent</div><div>Processes</div><div>Files</div><div>CPU</div></div>
    <div class="track" id="track">
      <div class="lane" id="lane-agent"></div>
      <div class="lane" id="lane-procs"></div>
      <div class="lane" id="lane-files"></div>
      <div class="lane" id="lane-cpu"></div>
      <div class="hover-line" id="hoverline"></div>
      <div class="head" id="head"></div>
    </div>
    <div class="foot" id="foot"></div>
  </section>
</div>
<div class="tip" id="tip"></div>
<div class="toast" id="toast"></div>
<script id="data" type="application/json">__DATA_JSON__</script>
<script>
(() => {
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const DUR = D.duration;
const pad = (n) => String(n).padStart(2, "0");
const clockT = (t) => { t = Math.max(0, t); return `${Math.floor(t / 60)}:${pad(Math.floor(t % 60))}`; };
const fineT = (t) => { t = Math.max(0, t); return `${Math.floor(t / 60)}:${pad(Math.floor(t % 60))}.${Math.floor((t * 10) % 10)}`; };
const durS = (s) => s < 1 ? `${Math.round(s * 1000)} ms` : s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
const kb = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const inline = (s) => esc(s).replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");

// ---------------------------------------------------------------- header
const M = D.meta;
const CTX = D.context || {};
const CI = true;
const started = new Date(M.t0 * 1000);
if (CTX.pr_url) { const b = $("back"); b.href = CTX.pr_url; b.textContent = `\u2190 PR #${CTX.pr}`; b.style.display = "flex"; }
$("title").textContent = D.title || `${CTX.repo || ""} \u00b7 ${M.agent || "agent"} run`;
const when = started.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC", hour12: false }) + " UTC";
const where = CTX.repo ? `${CTX.repo}${CTX.pr ? " \u00b7 PR #" + CTX.pr : CTX.issue ? " \u00b7 issue #" + CTX.issue : ""}` : "";
$("subtitle").textContent = [where, [M.agent, M.model].filter(Boolean).join(" "), M.mode, `box ${M.host}`, M.workdir, when].filter(Boolean).join(" \u00b7 ");
const finalSnap = D.snaps[D.snaps.length - 1];
let plus = 0, minus = 0;
for (const [, , di] of (finalSnap ? finalSnap.vs_base : [])) for (const l of D.diffs[di].split("\n")) { if (l.startsWith("+") && !l.startsWith("+++")) plus++; else if (l.startsWith("-") && !l.startsWith("---")) minus++; }
const tools = D.items.filter((i) => i.kind === "tool");
const cmds = D.procs.filter((p) => !p.wrap && p.who === "tool");
const u = M.usage || {};
const tokens = (u.input || 0) + (u.output || 0) + (u.cache_read || 0) + (u.cache_write || 0);
const chips = [
  `<span class="chip"><strong>${clockT(DUR)}</strong> recorded</span>`,
  `<span class="chip"><strong>${tools.length}</strong> tool calls</span>`,
  M.ptrace ? `<span class="chip"><strong>${cmds.length}</strong> processes</span>` : "",
  `<span class="chip"><strong>${finalSnap ? finalSnap.vs_base.length : 0}</strong> file${finalSnap && finalSnap.vs_base.length === 1 ? "" : "s"} <span class="plus">+${plus}</span> <span class="minus">\u2212${minus}</span></span>`,
  tokens ? `<span class="chip opt"><strong>${(tokens / 1000).toFixed(0)}k</strong> tokens</span>` : "",
];
$("chips").innerHTML = chips.join("");
$("termname").textContent = `Agent log \u00b7 ${M.host}`;
$("termsize").textContent = M.mode || "";
const st = D.stats || {};
const red = Object.values(D.redactions || {}).reduce((a, b) => a + b, 0);
$("foot").innerHTML = [
  M.ptrace ? `Recorded on the box: the agent's own event stream, and every process it started with its exit code (ptrace)` : `Recorded on the box: the agent's own event stream (no process tracing on this box)`,
  st.recorder_cpu_s != null ? `Recorder CPU <b>${st.recorder_cpu_s.toFixed(2)} s</b> over ${clockT(DUR)}` : "",
  `Secrets masked on the box before this page was written: <b>${red}</b> found` + (D.secretSources && D.secretSources.length ? `<span class="srcs" title="${esc(D.secretSources.join("\n"))}"> (${D.secretValuesChecked} value${D.secretValuesChecked === 1 ? "" : "s"} from ${esc(D.secretSources.map((x) => x.replace(/^(env|file):/, "")).slice(0, 4).join(", "))}${D.secretSources.length > 4 ? ` and ${D.secretSources.length - 4} more` : ""}, plus known key formats)</span>` : ` (checked ${D.secretValuesChecked} secret values and known key formats)`),
  ...(M.notes || []).map(esc),
].filter(Boolean).map((s) => `<span>${s}</span>`).join("");

// ---------------------------------------------------------------- log
const logEl = $("log"), logWrap = $("termwrap");
const logT = D.log.map((l) => l[0]);
const logEls = D.log.map(([, cls, text]) => { const d = document.createElement("div"); d.className = `ll ${cls} future`; d.textContent = text; logEl.appendChild(d); return d; });
let shownLines = 0;
function logTo(t) {
  let lo = 0, hi = logT.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (logT[m] <= t + 1e-6) lo = m + 1; else hi = m; }
  if (lo === shownLines) return;
  for (let i = Math.min(lo, shownLines); i < Math.max(lo, shownLines); i++) logEls[i].classList.toggle("future", i >= lo);
  shownLines = lo;
  logWrap.scrollTop = logWrap.scrollHeight;
}

// ---------------------------------------------------------------- activity
const CAT = { run: "var(--run)", edit: "var(--edit)", read: "var(--read)", other: "var(--other)" };
const feed = $("feed");
const itemEls = [];
$("actcount").textContent = `${D.items.length} steps`;
for (const it of D.items) {
  const el = document.createElement("div");
  el.className = `it ${it.kind}`;
  let h = `<span class="tm">${fineT(it.t)}</span>`;
  if (it.kind === "tool") {
    const d = it.te != null ? durS(Math.max(0, it.te - it.t)) : "";
    h += `<div class="hd"><span class="tag"><i style="background:${CAT[it.cat]}"></i>${esc(it.name)}</span><span class="dur">${d}</span></div>`;
    h += `<div class="cmd">${esc(it.summary)}</div>`;
    const ps = (it.procs || []).filter((p) => !/^\/bin\/(ba)?sh /.test(p.cmd));
    if (ps.length) h += `<div class="procs">${ps.slice(0, 8).map((p) => `<span class="pc${p.code ? " bad" : ""}" data-code="${p.code}" title="${esc(p.untraced ? p.cmd + " (setuid: ran untraced)" : p.cmd)}">${esc(p.cmd.length > 54 ? p.cmd.slice(0, 53) + "\u2026" : p.cmd)}</span>`).join("")}${ps.length > 8 ? `<span class="pc">+${ps.length - 8}</span>` : ""}</div>`;
    if (it.result) h += `<span class="more">Output \u00b7 ${it.result.split("\n").length} lines</span><div class="out">${esc(it.result)}</div>`;
  } else if (it.kind === "prompt") {
    h += `<div class="hd"><span class="tag">${it.slash ? "Command" : "Task"}</span></div><div class="txt">${inline(it.text)}</div>`;
  } else if (it.kind === "say") {
    h += `<div class="hd"><span class="tag">${esc(M.agent || "Agent")}</span></div><div class="txt">${inline(it.text)}</div>`;
  } else if (it.kind === "shell") {
    h += `<div class="hd"><span class="tag">${CI ? "agent-ci" : "Shell"}</span>${it.code ? `<span class="dur" style="color:#ff9b9b">exit ${it.code}</span>` : ""}</div><div class="cmd">$ ${esc(it.text)}</div>`;
  } else if (it.kind === "launch") {
    h += `<div class="hd"><span class="tag">Agent started</span></div><div class="cmd">$ ${esc(it.text)}</div>`;
  } else if (it.kind === "files") {
    h += `<div class="hd"><span class="tag"><i style="background:var(--edit)"></i>Files changed</span></div><div class="files-list">${it.changed.map(([s, p]) => `<span class="fl" data-path="${esc(p)}">${s === "A" ? "+ " : s === "D" ? "\u2212 " : ""}${esc(p)}</span>`).join("")}</div>`;
  }
  el.innerHTML = h;
  el.addEventListener("click", (e) => {
    if (e.target.classList.contains("more")) { el.classList.toggle("open"); return; }
    if (e.target.closest(".out")) return;
    const fl = e.target.closest(".fl");
    if (fl) { pinnedFile = fl.dataset.path; }
    seek(it.kind === "files" ? it.t + 0.01 : it.t, true);
  });
  feed.appendChild(el);
  itemEls.push(el);
}
let userScrollAt = -1e9, programScroll = false;
feed.addEventListener("scroll", () => { if (!programScroll) userScrollAt = performance.now(); programScroll = false; }, { passive: true });
let currentIdx = -2;
function feedTo(t) {
  let cur = -1;
  for (let i = 0; i < D.items.length; i++) if (D.items[i].t <= t + 1e-6) cur = i;
  for (let i = 0; i < itemEls.length; i++) {
    const c = itemEls[i].classList;
    c.toggle("future", i > cur);
    c.toggle("current", i === cur);
  }
  if (cur !== currentIdx) {
    currentIdx = cur;
    if (cur >= 0 && performance.now() - userScrollAt > 2500) {
      const el = itemEls[cur];
      const target = el.getBoundingClientRect().top - feed.getBoundingClientRect().top + feed.scrollTop - feed.clientHeight * 0.38;
      programScroll = true;
      feed.scrollTo({ top: Math.max(0, target), behavior: player.playing ? "smooth" : "auto" });
    }
  }
}

// ---------------------------------------------------------------- diff
let pinnedFile = null, shownKey = "";
const diffCache = new Map();
function renderDiff(text) {
  if (diffCache.has(text)) return diffCache.get(text);
  let o = 0, n = 0, out = "";
  for (const l of text.split("\n")) {
    if (/^(diff --git|index |--- |\+\+\+ |new file|deleted file|similarity|rename )/.test(l)) continue;
    const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(l);
    if (m) { o = +m[1]; n = +m[2]; out += `<div class="dl hunk"><span class="ln"></span><span class="ln"></span><span class="c">${esc(m[3].trim() || "\u2026")}</span></div>`; continue; }
    if (l.startsWith("+")) out += `<div class="dl add"><span class="ln"></span><span class="ln">${n++}</span><span class="c">${esc(l.slice(1))}</span></div>`;
    else if (l.startsWith("-")) out += `<div class="dl del"><span class="ln">${o++}</span><span class="ln"></span><span class="c">${esc(l.slice(1))}</span></div>`;
    else if (l.startsWith(" ")) out += `<div class="dl ctx"><span class="ln">${o++}</span><span class="ln">${n++}</span><span class="c">${esc(l.slice(1))}</span></div>`;
  }
  diffCache.set(text, out);
  return out;
}
function counts(text) { let a = 0, d = 0; for (const l of text.split("\n")) { if (l.startsWith("+") && !l.startsWith("+++")) a++; else if (l.startsWith("-") && !l.startsWith("---")) d++; } return [a, d]; }
function diffTo(t) {
  let si = 0;
  for (let i = 0; i < D.snaps.length; i++) if (D.snaps[i].t <= t + 1e-6) si = i;
  const s = D.snaps[si];
  const entries = s ? s.vs_base : [];
  const fresh = new Set((s && si > 0 ? s.changed : []).map((c) => c[1]));
  let sel = entries.find((e) => e[1] === pinnedFile);
  if (!sel) { const recent = entries.filter((e) => fresh.has(e[1])); sel = recent[0] || entries[0]; }
  const key = `${si}|${sel ? sel[1] : ""}`;
  if (key === shownKey) return;
  shownKey = key;
  $("tabs").innerHTML = entries.map((e) => { const [a, d] = counts(D.diffs[e[2]]); return `<button class="tab${sel && e[1] === sel[1] ? " on" : ""}${fresh.has(e[1]) ? " fresh" : ""}" data-path="${esc(e[1])}">${esc(e[1])}<span class="n"><b>+${a}</b> <s>\u2212${d}</s></span></button>`; }).join("");
  let ta = 0, td = 0; for (const e of entries) { const [a, d] = counts(D.diffs[e[2]]); ta += a; td += d; }
  $("diffstat").textContent = entries.length ? `${entries.length} file${entries.length > 1 ? "s" : ""} \u00b7 +${ta} \u2212${td}` : "";
  $("diff").innerHTML = sel ? renderDiff(D.diffs[sel[2]]) : `<div class="empty">No files changed yet. A file shows here from the agent's first edit to it, with the turn's final diff.</div>`;
}
$("tabs").addEventListener("click", (e) => { const b = e.target.closest(".tab"); if (b) { pinnedFile = b.dataset.path; shownKey = ""; diffTo(player.t); } });

// ---------------------------------------------------------------- timeline
const pct = (t) => `${(100 * Math.min(Math.max(t, 0), DUR)) / DUR}%`;
const tipEl = $("tip");
const marks = [];
function addMark(lane, el, t, label, body) { el.dataset.mi = marks.length; marks.push({ t, label, body }); $(lane).appendChild(el); }
(() => { // the agent's working span: from the task to its last word
  const p = D.items.find((i) => i.kind === "prompt"), says = D.items.filter((i) => i.kind === "say" || i.kind === "tool");
  if (!p || !says.length) return;
  const end = Math.max(...says.map((i) => i.te ?? i.t));
  const el = document.createElement("div");
  el.className = "busy";
  el.style.left = pct(p.t); el.style.width = `${(100 * (end - p.t)) / DUR}%`;
  $("lane-agent").appendChild(el);
})();
for (const it of D.items) {
  if (it.kind === "tool") {
    const el = document.createElement("div");
    el.className = "m";
    el.style.left = pct(it.t);
    el.style.width = `max(4px, ${(100 * Math.max(0, (it.te ?? it.t) - it.t)) / DUR}%)`;
    el.style.background = CAT[it.cat];
    addMark("lane-agent", el, it.t, `${it.name} \u00b7 ${fineT(it.t)}`, it.summary);
  } else if (it.kind === "prompt" || it.kind === "say") {
    const el = document.createElement("div");
    el.className = `dot ${it.kind}`;
    el.style.left = pct(it.t);
    addMark("lane-agent", el, it.t, `${it.kind === "prompt" ? "Task" : M.agent || "Agent"} \u00b7 ${fineT(it.t)}`, it.text);
  }
}
for (const p of D.procs) {
  if (p.wrap) continue;
  const el = document.createElement("div");
  el.className = `tick ${p.who}${p.code ? " bad" : ""}`;
  el.style.left = pct(p.t);
  addMark("lane-procs", el, p.t, `${p.who === "tool" ? "Tool process" : "Agent internal"} \u00b7 ${fineT(p.t)}${p.code != null ? ` \u00b7 exit ${p.code}` : p.untraced ? " \u00b7 setuid, untraced" : ""}`, p.cmd);
}
for (const s of D.snaps.slice(1)) {
  if (!s.changed.length) continue;
  const el = document.createElement("div");
  el.className = "tick file";
  el.style.left = pct(s.t);
  addMark("lane-files", el, s.t, `Files changed \u00b7 ${fineT(s.t)}`, s.changed.map((c) => `${c[0]} ${c[1]}`).join("\n"));
}
(() => { // CPU of the session's process tree, % of one vCPU
  const pts = [];
  for (let i = 1; i < D.cpu.length; i++) { const [t0, c0] = D.cpu[i - 1], [t1, c1] = D.cpu[i]; pts.push([t1, Math.max(0, (c1 - c0) / Math.max(0.2, t1 - t0))]); }
  if (!pts.length) return;
  const peak = Math.max(1, ...pts.map((p) => p[1]));
  const W = 1000, H = 18;
  let d = `M0 ${H}`;
  for (const [t, v] of pts) d += ` L${(W * t / DUR).toFixed(1)} ${(H - 1 - (H - 3) * v / peak).toFixed(1)}`;
  d += ` L${(W * pts[pts.length - 1][0] / DUR).toFixed(1)} ${H} Z`;
  $("lane-cpu").innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><path d="${d}" fill="rgba(195,194,183,.22)" stroke="#c3c2b7" stroke-width="1.2" vector-effect="non-scaling-stroke"/></svg>`;
  const lab = document.createElement("div");
  lab.style.cssText = "position:absolute;right:6px;top:2px;font-size:10.5px;color:var(--text-muted)";
  lab.textContent = `peak ${Math.round(peak * 100)}% of 1 vCPU`;
  $("lane-cpu").appendChild(lab);
})();
// idle stretches: no terminal output and no events for 4 s or more
const evTimes = [0, ...logT, ...D.items.map((i) => i.t), ...D.items.filter((i) => i.te != null).map((i) => i.te), ...D.snaps.map((s) => s.t)].sort((a, b) => a - b);
const idles = [];
for (let i = 1; i < evTimes.length; i++) if (evTimes[i] - evTimes[i - 1] >= 4) idles.push([evTimes[i - 1], evTimes[i]]);
if (DUR - evTimes[evTimes.length - 1] >= 4) idles.push([evTimes[evTimes.length - 1], DUR]);
for (const [a, b] of idles) {
  const el = document.createElement("div");
  el.className = "idle";
  el.style.left = pct(a); el.style.width = `${(100 * (b - a)) / DUR}%`;
  el.innerHTML = `<span>idle ${Math.round(b - a)} s</span>`;
  $("lane-cpu").parentElement.insertBefore(el, $("lane-agent"));
  el.style.gridRow = "1 / span 4";
}
const track = $("track");
function tAt(clientX) { const r = track.getBoundingClientRect(); return Math.min(DUR, Math.max(0, ((clientX - r.left) / r.width) * DUR)); }
let dragging = false;
let hoverMark = null;
track.addEventListener("pointerdown", (e) => { dragging = true; track.setPointerCapture(e.pointerId); seek(hoverMark ? hoverMark.t : tAt(e.clientX), true); });
track.addEventListener("pointermove", (e) => {
  const r = track.getBoundingClientRect();
  const hl = $("hoverline");
  hl.style.display = "block"; hl.style.left = `${e.clientX - r.left}px`;
  if (dragging) { hoverMark = null; seek(tAt(e.clientX), true); tipEl.style.display = "none"; return; }
  let mi = e.target.dataset && e.target.dataset.mi;
  if (mi == null) { // thin marks: take the nearest one within 7 px in the lane under the pointer
    const lane = [...track.querySelectorAll(".lane")].find((l) => { const b = l.getBoundingClientRect(); return e.clientY >= b.top - 2 && e.clientY <= b.bottom + 2; });
    let best = 8;
    if (lane) for (const el of lane.querySelectorAll("[data-mi]")) { const b = el.getBoundingClientRect(); const d = Math.max(0, b.left - e.clientX, e.clientX - b.right); if (d < best) { best = d; mi = el.dataset.mi; } }
  }
  if (mi != null) {
    const m = marks[+mi];
    hoverMark = m;
    tipEl.innerHTML = `<div class="tt">${esc(m.label)}</div><div class="tc">${esc(m.body)}</div>`;
  } else {
    hoverMark = null;
    tipEl.innerHTML = `<div class="tt">${fineT(tAt(e.clientX))}</div>`;
  }
  tipEl.style.display = "block";
  const tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
  tipEl.style.left = `${Math.min(window.innerWidth - tw - 8, Math.max(8, e.clientX - tw / 2))}px`;
  tipEl.style.top = `${r.top - th - 10}px`;
});
track.addEventListener("pointerup", () => { dragging = false; });
track.addEventListener("pointerleave", () => { hoverMark = null; $("hoverline").style.display = "none"; tipEl.style.display = "none"; });

// ---------------------------------------------------------------- player
const ICON_PLAY = '<svg viewBox="0 0 14 14"><path d="M3 1.8v10.4c0 .6.6.9 1.1.6l8.2-5.2a.7.7 0 000-1.2L4.1 1.2C3.6.9 3 1.2 3 1.8z" fill="currentColor"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 14 14"><rect x="2.5" y="1.5" width="3.2" height="11" rx="1" fill="currentColor"/><rect x="8.3" y="1.5" width="3.2" height="11" rx="1" fill="currentColor"/></svg>';
const player = { t: 0, playing: false, speed: 2, skip: true, last: 0 };
function render() {
  logTo(player.t);
  feedTo(player.t);
  diffTo(player.t);
  $("head").style.left = pct(player.t);
  $("clock").innerHTML = `<b>${clockT(player.t)}</b> / ${clockT(DUR)}`;
}
function seek(t, user) {
  player.t = Math.min(DUR, Math.max(0, t));
  if (user) userScrollAt = -1e9;
  render();
}
function setPlaying(p) {
  player.playing = p;
  if (p && player.t >= DUR - 0.05) seek(0);
  $("play").innerHTML = p ? ICON_PAUSE : ICON_PLAY;
  $("play").setAttribute("aria-label", p ? "Pause" : "Play");
  document.body.classList.toggle("playing", p);
  player.last = performance.now();
  if (p) requestAnimationFrame(tick);
}
function nextEvent(t) {
  let lo = 0, hi = evTimes.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (evTimes[m] <= t) lo = m + 1; else hi = m; }
  return lo < evTimes.length ? evTimes[lo] : DUR;
}
function tick(now) {
  if (!player.playing) return;
  const dt = Math.min(0.1, (now - player.last) / 1000);
  player.last = now;
  let t = player.t + dt * player.speed;
  if (player.skip) { const n = nextEvent(player.t); if (n - player.t > 1.5 && t < n - 0.4) t = Math.max(t, n - 0.4); }
  if (t >= DUR) { seek(DUR); setPlaying(false); return; }
  seek(t);
  requestAnimationFrame(tick);
}
$("play").addEventListener("click", () => setPlaying(!player.playing));
$("speed").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; player.speed = +b.dataset.s; for (const x of $("speed").children) x.classList.toggle("on", x === b); });
$("skip").addEventListener("click", () => { player.skip = !player.skip; $("skip").classList.toggle("on", player.skip); });
document.addEventListener("keydown", (e) => {
  if (e.key === " ") { e.preventDefault(); setPlaying(!player.playing); }
  else if (e.key === "ArrowRight") seek(player.t + 2, true);
  else if (e.key === "ArrowLeft") seek(player.t - 2, true);
});
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("on"), 1800); }
$("share").addEventListener("click", async () => {
  const url = `${location.href.split("#")[0]}#t=${player.t.toFixed(1)}`;
  history.replaceState(null, "", `#t=${player.t.toFixed(1)}`);
  try { await navigator.clipboard.writeText(url); } catch (_) {
    const ta = document.createElement("textarea"); ta.value = url; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  toast(`Link to ${clockT(player.t)} copied`);
});
window.__replay = { seek: (t) => seek(t, true), play: setPlaying, state: () => ({ t: player.t, playing: player.playing }) };
const m = /t=([\d.]+)/.exec(location.hash);
requestAnimationFrame(() => { seek(m ? +m[1] : 0); setPlaying(false); });
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
