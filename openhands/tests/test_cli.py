"""`openhands-prized serve` without a sandbox: a stand-in workspace records what serve does."""

import os
import signal
import subprocess
import sys
import threading
from typing import ClassVar

import pytest

from openhands_prized import cli


class FakeWorkspace:
    instances: ClassVar[list["FakeWorkspace"]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cleaned = 0
        self.host = "http://127.0.0.1:8780"
        self.api_key = "k" * 43
        self.transport = "exec"
        self.sandbox = type("S", (), {"id": "sbx_test"})()
        FakeWorkspace.instances.append(self)

    def cleanup(self):
        self.cleaned += 1


class GoneStream:
    """stderr after the terminal or pipe it wrote to has closed."""

    def write(self, _):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


@pytest.fixture(autouse=True)
def _no_banner_leak(monkeypatch):
    # cli.main() sets OPENHANDS_SUPPRESS_BANNER for the whole process. Set then delete, so monkeypatch
    # puts the variable back to how this run found it (unset or not) after each test.
    monkeypatch.setenv("OPENHANDS_SUPPRESS_BANNER", "x")
    monkeypatch.delenv("OPENHANDS_SUPPRESS_BANNER")


def _ctrl_c():
    raise KeyboardInterrupt


@pytest.fixture
def fake_serve(monkeypatch):
    from openhands_prized import workspace

    FakeWorkspace.instances.clear()
    monkeypatch.setattr(workspace, "PrizedWorkspace", FakeWorkspace)
    monkeypatch.setattr(cli, "_previous_sandbox", lambda: None)
    monkeypatch.setattr(cli, "_stop_on_signals", lambda: None)
    monkeypatch.setattr(cli, "_wait_for_interrupt", _ctrl_c)
    return FakeWorkspace.instances


def test_ctrl_c_pauses_the_sandbox_by_default(fake_serve, capsys):
    assert cli.main(["serve"]) == 0
    (ws,) = fake_serve
    assert ws.kwargs["on_exit"] == "pause" and ws.kwargs["labels"] == {"openhands": "canvas"}
    assert ws.cleaned == 1
    out = capsys.readouterr().out
    assert "Host / Base URL:  http://127.0.0.1:8780" in out and "paused (serve again to pick it up)" in out
    assert "Add Backend > Agent-server > Local" in out


def test_cleanup_runs_when_stderr_is_gone(fake_serve, monkeypatch):
    # Ctrl-C in `serve | tee log` kills tee too: printing "Stopping..." then fails, and
    # the sandbox must still be paused rather than left running.
    def ctrl_c_kills_tee():
        monkeypatch.setattr(sys, "stderr", GoneStream())
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_wait_for_interrupt", ctrl_c_kills_tee)
    cli.serve(cli.build_parser().parse_args(["serve", "--new"]))
    assert fake_serve[0].cleaned == 1


def test_cli_does_not_import_the_sdk_until_it_serves():
    code = (
        "import sys, openhands_prized.cli, openhands_prized;"
        "print('openhands.sdk' in sys.modules, openhands_prized.__version__ != '')"
    )
    env = {k: v for k, v in os.environ.items() if k != "OPENHANDS_SUPPRESS_BANNER"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.split() == ["False", "True"], out.stdout + out.stderr


def test_main_hides_the_sdk_banner(fake_serve):
    assert "OPENHANDS_SUPPRESS_BANNER" not in os.environ
    cli.main(["serve"])
    assert os.environ["OPENHANDS_SUPPRESS_BANNER"] == "1"


@pytest.fixture
def restore_signals():
    saved = {
        getattr(signal, n): signal.getsignal(getattr(signal, n)) for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n)
    }
    for sig in saved:  # start from the defaults, even if the test runner itself was started with SIGHUP ignored
        signal.signal(sig, signal.SIG_DFL)
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_sighup_and_sigterm_stop_serve_like_ctrl_c(restore_signals):
    cli._stop_on_signals()
    assert signal.getsignal(signal.SIGHUP) is cli._interrupt
    assert signal.getsignal(signal.SIGTERM) is cli._interrupt


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_closing_the_terminal_still_pauses_the_sandbox(monkeypatch, restore_signals):
    from openhands_prized import workspace

    FakeWorkspace.instances.clear()
    monkeypatch.setattr(workspace, "PrizedWorkspace", FakeWorkspace)
    monkeypatch.setattr(cli, "_previous_sandbox", lambda: None)

    def terminal_closes():
        os.kill(os.getpid(), signal.SIGHUP)
        _ctrl_c()  # not reached: the handler raises first

    monkeypatch.setattr(cli, "_wait_for_interrupt", terminal_closes)
    assert cli.serve(cli.build_parser().parse_args(["serve", "--new"])) == 0
    assert FakeWorkspace.instances[0].cleaned == 1


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_nohup_keeps_sighup_ignored(restore_signals):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    cli._stop_on_signals()
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    assert signal.getsignal(signal.SIGTERM) is cli._interrupt


def test_sigterm_is_always_taken_over(restore_signals):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    cli._stop_on_signals()
    assert signal.getsignal(signal.SIGTERM) is cli._interrupt


def test_no_sighup_on_this_platform_is_fine(monkeypatch, restore_signals):
    monkeypatch.delattr(cli.signal, "SIGHUP", raising=False)
    cli._stop_on_signals()
    assert signal.getsignal(signal.SIGTERM) is cli._interrupt


def test_serve_off_the_main_thread_does_not_crash(restore_signals):
    # signal.signal raises ValueError there; embedding serve in a thread must not die on it.
    errors = []

    def run():
        try:
            cli._stop_on_signals()
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert errors == []


def test_package_still_exports_the_workspace():
    import openhands_prized
    from openhands_prized.workspace import PrizedWorkspace

    assert openhands_prized.PrizedWorkspace is PrizedWorkspace
    with pytest.raises(AttributeError):
        openhands_prized.Nope  # noqa: B018
