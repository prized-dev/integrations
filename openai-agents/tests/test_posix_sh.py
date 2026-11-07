"""The background-process scripts under each shell a sandbox's `sh` may be.

The session runs `PTY_START`, `PTY_POLL` and `PTY_KILL` with bash and
`PTY_STDIN` with `sh`, but each is written to work under any of them, so a
template whose `sh` is dash or busybox (or a later change to `sh -c`) cannot
leave a process running that the session believes it killed. The commands the
scripts start still run under bash, as they do in a sandbox.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from openai_agents_prized import _scripts
from openai_agents_prized._scripts import PTY_KILL, PTY_POLL, PTY_START, PTY_STDIN, PTY_STDIN_ENV

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

SHELLS = {
    "sh": ["sh"],
    "dash": ["dash"],
    "busybox": ["busybox", "sh"],
    "bash": ["bash"],
    "bash-posix": ["bash", "--posix"],
}


def _available(name: str) -> bool:
    argv = SHELLS[name]
    if shutil.which(argv[0]) is None:
        return False
    return subprocess.run([*argv, "-c", "true"], capture_output=True, check=False).returncode == 0


@pytest.fixture(params=list(SHELLS))
def shell(request: pytest.FixtureRequest) -> list[str]:
    if not _available(request.param):
        pytest.skip(f"{SHELLS[request.param][0]} is not installed")
    return SHELLS[request.param]


@pytest.fixture
def pids() -> Iterator[list[int]]:
    """Pids a test started; whatever is left of them is killed after it."""
    started: list[int] = []
    yield started
    for pid in started:
        if _alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


def _sh(
    shell: list[str], script: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*shell, "-c", script, "sh", *args],
        capture_output=True,
        timeout=30,
        check=False,
        env=None if env is None else {**os.environ, **env},
    )


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return not stat.rsplit(") ", 1)[1].startswith(("Z", "X"))


def _until(check: Callable[[], bool], seconds: float = 10) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


def _pid_in(path: Path, pids: list[int]) -> int:
    assert _until(lambda: path.exists() and path.read_text().strip() != ""), path
    pid = int(path.read_text())
    pids.append(pid)
    return pid


def _start(shell: list[str], process: Path, command: str, pids: list[int], *, tty: bool) -> int:
    started = _sh(shell, PTY_START, str(process), command, str(int(tty)))
    assert started.returncode == 0, started.stderr
    return _pid_in(process / "pid", pids)


def _poll(shell: list[str], process: Path, wait_ms: int = 5000) -> tuple[str, str, bytes]:
    polled = _sh(shell, PTY_POLL, str(process), "0", str(wait_ms), str(1 << 20))
    assert polled.returncode == 0, polled.stderr
    header, _, body = polled.stdout.partition(b"\n")
    done, code, _offset = header.decode().split()
    return done, code, base64.b64decode(body)


def _kill(shell: list[str], *processes: Path) -> None:
    killed = _sh(shell, PTY_KILL, *map(str, processes))
    assert killed.returncode == 0, killed.stderr


def test_a_command_runs_and_its_exit_is_read(
    shell: list[str], tmp_path: Path, pids: list[int]
) -> None:
    process = tmp_path / "pty" / "1"
    _start(shell, process, "echo out; echo err >&2; exit 3", pids, tty=False)
    assert _poll(shell, process) == ("1", "3", b"out\nerr\n")


def test_input_reaches_a_tty(shell: list[str], tmp_path: Path, pids: list[int]) -> None:
    process = tmp_path / "pty" / "1"
    loop = "printf 'ready\\n'; while IFS= read -r line; do printf 'got:%s\\n' \"$line\"; done"
    leader = _start(shell, process, loop, pids, tty=True)
    assert _until(lambda: b"ready" in (process / "out").read_bytes())
    typed = subprocess.run(
        [*shell, "-c", PTY_STDIN, "sh", str(process)],
        capture_output=True,
        timeout=30,
        check=False,
        env={**os.environ, PTY_STDIN_ENV: "hi\n"},
    )
    assert typed.returncode == 0, typed.stderr
    assert _until(lambda: b"got:hi" in (process / "out").read_bytes())
    _kill(shell, process)
    assert _until(lambda: not _alive(leader))
    assert not process.exists()


def test_kill_ends_the_group_after_its_leader_exits(
    shell: list[str], tmp_path: Path, pids: list[int]
) -> None:
    # Once the exit file is there the leader's pid may be reused, so only the
    # group kill reaches what it left behind.
    process = tmp_path / "pty" / "1"
    child = tmp_path / "child"
    _start(shell, process, f"sleep 100 & echo $! > {shlex.quote(str(child))}", pids, tty=False)
    left = _pid_in(child, pids)
    assert _until((process / "exit").exists)
    assert _alive(left)
    _kill(shell, process)
    assert _until(lambda: not _alive(left))
    assert not process.exists()


def test_kill_ends_a_process_that_left_the_tree(
    shell: list[str], tmp_path: Path, pids: list[int]
) -> None:
    # A subshell's background child is handed to init when the subshell exits:
    # out of the leader's tree, still in its group.
    process = tmp_path / "pty" / "1"
    orphan = tmp_path / "orphan"
    command = f"(sleep 100 & echo $! > {shlex.quote(str(orphan))}); sleep 100"
    leader = _start(shell, process, command, pids, tty=False)
    left = _pid_in(orphan, pids)
    _kill(shell, process)
    assert _until(lambda: not _alive(leader))
    assert _until(lambda: not _alive(left))
    assert not process.exists()


def test_a_kill_before_the_start_keeps_it_from_running(shell: list[str], tmp_path: Path) -> None:
    process = tmp_path / "pty" / "1"
    _kill(shell, process)
    assert (process / "cancelled").exists()
    started = _sh(
        shell, PTY_START, str(process), f"touch {shlex.quote(str(tmp_path / 'ran'))}", "0"
    )
    assert started.returncode != 0
    time.sleep(0.3)
    assert not (tmp_path / "ran").exists()


def test_no_script_puts_dashes_before_a_process_group() -> None:
    scripts = {name: value for name, value in vars(_scripts).items() if isinstance(value, str)}
    for name, script in scripts.items():
        assert not re.search(r"\bkill\s+-[A-Za-z0-9]+\s+--\s", script), name
