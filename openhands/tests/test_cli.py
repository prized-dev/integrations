"""`openhands-prized serve` without a sandbox: a stand-in workspace records what serve does."""

import os
import subprocess
import sys
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


@pytest.fixture
def fake_serve(monkeypatch):
    from openhands_prized import workspace

    FakeWorkspace.instances.clear()
    monkeypatch.setattr(workspace, "PrizedWorkspace", FakeWorkspace)
    monkeypatch.setattr(cli, "_previous_sandbox", lambda: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)

    def ctrl_c(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", ctrl_c)
    return FakeWorkspace.instances


def test_ctrl_c_pauses_the_sandbox_by_default(fake_serve, capsys):
    assert cli.main(["serve"]) == 0
    (ws,) = fake_serve
    assert ws.kwargs["on_exit"] == "pause" and ws.kwargs["labels"] == {"openhands": "canvas"}
    assert ws.cleaned == 1
    out = capsys.readouterr().out
    assert "Host / Base URL:  http://127.0.0.1:8780" in out and "paused (serve again to pick it up)" in out


def test_cleanup_runs_when_stderr_is_gone(fake_serve, monkeypatch):
    # Ctrl-C in `serve | tee log` kills tee too: printing "Stopping..." then fails, and
    # the sandbox must still be paused rather than left running.
    def ctrl_c_kills_tee(_):
        monkeypatch.setattr(sys, "stderr", GoneStream())
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", ctrl_c_kills_tee)
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


def test_main_hides_the_sdk_banner(fake_serve, monkeypatch):
    monkeypatch.delenv("OPENHANDS_SUPPRESS_BANNER", raising=False)
    cli.main(["serve"])
    assert os.environ["OPENHANDS_SUPPRESS_BANNER"] == "1"


def test_package_still_exports_the_workspace():
    import openhands_prized
    from openhands_prized.workspace import PrizedWorkspace

    assert openhands_prized.PrizedWorkspace is PrizedWorkspace
    with pytest.raises(AttributeError):
        openhands_prized.Nope  # noqa: B018
