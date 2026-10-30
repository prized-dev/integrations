import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).parent
RELAY = HERE.parent / "src" / "openhands_prized" / "ws_relay.py"
SESSION_KEY = "sess-test-key"
PRIZED_KEY = "dcp_0000000000000000000000000000000000000000000000000"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{url} never answered")


@pytest.fixture
def agent_server():
    """A fake Agent Server on a free loopback port: (port, session key)."""
    port = free_port()
    env = {**os.environ, "OH_SESSION_API_KEYS_0": SESSION_KEY}
    proc = subprocess.Popen([sys.executable, str(HERE / "fake_agent_server.py"), "--port", str(port)], env=env)
    try:
        wait_http(f"http://127.0.0.1:{port}/health")
        yield port, SESSION_KEY
    finally:
        proc.terminate()
        proc.wait(10)
