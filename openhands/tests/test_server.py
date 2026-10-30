"""The in-sandbox scripts: valid shell, quoted, and parsed back right."""

import base64
import json
import os
import subprocess
import time

import pytest

from openhands_prized import _server
from openhands_prized.ws_relay import close_code_for_status


@pytest.mark.parametrize(
    "script",
    [
        _server.install_script("1.59.0", ["openhands-sdk[boto3]==1.59.0", "x; rm -rf /"]),
        _server.probe_script(8000),
        _server.stop_script(),
        _server.health_script(8000),
        _server.start_command(8000, ["--import-modules", "a b'c"]),
    ],
)
def test_scripts_are_valid_shell(script):
    subprocess.run(["bash", "-n", "-c", script], check=True)


def test_install_pins_every_openhands_package_and_quotes_extras():
    s = _server.install_script("1.59.0", ["x; rm -rf /"])
    for pkg in ("openhands-agent-server", "openhands-sdk", "openhands-tools", "openhands-workspace"):
        assert f"{pkg}==1.59.0" in s
    assert "--with 'x; rm -rf /'" in s


@pytest.mark.parametrize("bad", ["1.0; rm -rf /", "", "1.0 1.1", "$(id)", "a" * 65])
def test_versions_are_checked(bad):
    with pytest.raises(ValueError):
        _server.install_script(bad)


def test_start_command_quotes_args():
    cmd = _server.start_command(8123, ["--import-modules", "a b'c"])
    assert "--port 8123" in cmd and "--host 127.0.0.1" in cmd
    assert "'a b'\"'\"'c'" in cmd


def test_probe_parsing_and_reuse_rule():
    state = base64.b64encode(b'{"pid": 12, "port": 8000, "version": "1.59.0", "key": "k", "config": "c"}').decode()
    out = f"STATE {state}\nALIVE=1\nHEALTH=1\n"
    p = _server.parse_probe(out)
    assert p.state["pid"] == 12
    assert p.reusable("1.59.0", 8000)
    assert not p.reusable("1.60.0", 8000)
    assert not p.reusable("1.59.0", 9000)
    assert not _server.parse_probe(out.replace("HEALTH=1", "HEALTH=0")).reusable("1.59.0", 8000)
    assert not _server.parse_probe("STATE \nALIVE=0\nHEALTH=0\n").reusable("1.59.0", 8000)
    assert _server.parse_probe("STATE bm90IGpzb24=\nALIVE=1\n").state == {}


def test_probe_script_checks_the_pid_is_an_agent_server(tmp_path):
    state_dir = tmp_path / ".openhands-prized"
    state_dir.mkdir()
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}

    def probe(pid):
        state = {"pid": pid, "port": 1, "version": "1.59.0", "key": "k"}
        (state_dir / "server.json").write_text(json.dumps(state))
        run = subprocess.run(
            ["bash", "-c", _server.probe_script(1)], capture_output=True, text=True, env=env, check=False
        )
        return _server.parse_probe(run.stdout)

    p = probe(os.getpid())
    assert p.state["key"] == "k"
    assert not p.alive, "a live pid that is not an agent-server (pid reuse) does not count"
    server = subprocess.Popen(["bash", "-c", "exec -a agent-server sleep 30"])
    try:
        time.sleep(0.2)
        p = probe(server.pid)
        assert p.alive and not p.healthy  # nothing answers on port 1
        subprocess.run(["bash", "-c", _server.stop_script()], env=env, check=True)
        assert server.wait(5) is not None
        assert not (state_dir / "server.json").exists()
    finally:
        server.kill()


def test_install_output_needs_paths():
    assert _server.parse_install("noise\nHOME=/home/prized\nPY=/p/python\nINSTALLED=0\n")["PY"] == "/p/python"
    with pytest.raises(RuntimeError):
        _server.parse_install("HOME=/home/prized\n")


def test_close_codes_for_refused_handshakes():
    assert close_code_for_status(401) == 4001
    assert close_code_for_status(403) == 4001
    assert close_code_for_status(404) == 4004
    assert close_code_for_status(502) == 4502
    assert close_code_for_status(None) == 1011
