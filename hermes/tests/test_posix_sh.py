"""The cancel script under each shell a sandbox's `sh` may be.

The edge runs it with bash today, but it is written to work under any of them, so a template whose
`sh` is dash or busybox (or a later change to `sh -c`) cannot leave a cancelled command running.
`_cancel_script` is read from the plugin's source, so this test needs no hermes-agent checkout.
The workload under test runs under bash, as it does in a sandbox.
"""

import ast
import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parent.parent / "prized_environment.py"
_tree = ast.parse(_SOURCE.read_text())
_fn = next(n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == "_cancel_script")
_ns: dict = {"shlex": shlex}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), str(_SOURCE), "exec"), _ns)
_cancel_script = _ns["_cancel_script"]

SHELLS = {
    "sh": ["sh"],
    "dash": ["dash"],
    "busybox": ["busybox", "sh"],
    "bash": ["bash"],
    "bash-posix": ["bash", "--posix"],
}


@pytest.fixture(params=list(SHELLS))
def shell(request):
    argv = SHELLS[request.param]
    if shutil.which(argv[0]) is None or subprocess.run([*argv, "-c", "true"], check=False).returncode != 0:
        pytest.skip(f"{argv[0]} is not installed")
    return argv


def _alive(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return not stat.rsplit(") ", 1)[1].startswith(("Z", "X"))


def _until(check, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


@pytest.fixture
def group(tmp_path):
    """Start `bash -c SCRIPT` as the leader of its own process group, its pid in a file as the wrapper writes it."""
    started = []

    def start(script):
        proc = subprocess.Popen(["bash", "-c", script], cwd=tmp_path, start_new_session=True)
        started.append(proc)
        pid_path = tmp_path / "cmd.pid"
        pid_path.write_text(f"{proc.pid}\n")
        return proc, pid_path

    yield start
    for proc in started:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _cancel(shell, pid_path):
    done = subprocess.run([*shell, "-c", _cancel_script(str(pid_path))], capture_output=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr


def test_cancel_kills_the_whole_group(shell, tmp_path, group):
    proc, pid_path = group('trap "" TERM; sleep 100 & echo $! > child; exec sleep 100')
    assert _until(lambda: (tmp_path / "child").exists() and (tmp_path / "child").read_text().strip() != "")
    child = int((tmp_path / "child").read_text())
    _cancel(shell, pid_path)
    proc.wait(timeout=10)
    assert _until(lambda: not _alive(child))
    assert not pid_path.exists()


def test_cancel_sends_term_first(shell, tmp_path, group):
    _proc, pid_path = group('trap "echo terminated > out; exit 0" TERM; while :; do sleep 0.1; done')
    time.sleep(0.2)
    _cancel(shell, pid_path)
    assert (tmp_path / "out").read_text() == "terminated\n"
