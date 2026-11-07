"""The stop script under each shell a sandbox's `sh` may be.

The edge runs it with bash today, but it is written to work under any of them, so a template whose
`sh` is dash or busybox (or a later change to `sh -c`) cannot leave a command running that a
timeout or a cancellation meant to stop. The workload under test runs under bash, as it does in a
sandbox.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from pydantic_ai_prized._backend import _stop_script

SHELLS = {
    'sh': ['sh'],
    'dash': ['dash'],
    'busybox': ['busybox', 'sh'],
    'bash': ['bash'],
    'bash-posix': ['bash', '--posix'],
}


@pytest.fixture(params=list(SHELLS))
def shell(request: pytest.FixtureRequest) -> list[str]:
    argv = SHELLS[request.param]
    if shutil.which(argv[0]) is None or subprocess.run([*argv, '-c', 'true'], check=False).returncode != 0:
        pytest.skip(f'{argv[0]} is not installed')
    return argv


def _alive(pid: int) -> bool:
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return not stat.rsplit(') ', 1)[1].startswith(('Z', 'X'))


def _until(check, seconds: float = 10) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


@pytest.fixture
def group(tmp_path: Path):
    """Start `bash -c SCRIPT` as the leader of its own process group, its pgid in a file as the edge writes it."""
    started: list[subprocess.Popen[bytes]] = []

    def start(script: str) -> tuple[subprocess.Popen[bytes], Path]:
        proc = subprocess.Popen(['bash', '-c', script], cwd=tmp_path, start_new_session=True)
        started.append(proc)
        pid_file = tmp_path / 'run.pid'
        pid_file.write_text(str(proc.pid))
        return proc, pid_file

    yield start
    for proc in started:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _stop(shell: list[str], pid_file: Path) -> None:
    stopped = subprocess.run([*shell, '-c', _stop_script(str(pid_file))], capture_output=True, timeout=30, check=False)
    assert stopped.returncode == 0, stopped.stderr


def test_stop_kills_the_whole_group(shell: list[str], tmp_path: Path, group) -> None:
    proc, pid_file = group('trap "" TERM; sleep 100 & echo $! > child; exec sleep 100')
    assert _until(lambda: (tmp_path / 'child').exists() and (tmp_path / 'child').read_text().strip() != '')
    child = int((tmp_path / 'child').read_text())
    _stop(shell, pid_file)
    proc.wait(timeout=10)
    assert _until(lambda: not _alive(child))
    assert not pid_file.exists()


def test_stop_lets_the_group_finish_after_its_term(shell: list[str], tmp_path: Path, group) -> None:
    # A command that takes half a second to finish on SIGTERM is waited for, not killed: the probe
    # has to see the group alive while it finishes.
    _proc, pid_file = group('trap "sleep 0.5; echo finished > out; exit 0" TERM; while :; do sleep 0.1; done')
    time.sleep(0.2)
    _stop(shell, pid_file)
    assert (tmp_path / 'out').read_text() == 'finished\n'
