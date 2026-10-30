"""A fake Prized sandbox: no API, no key. Setup commands get a canned answer, and runtime.py
commands run their script with the local bash (no chroot), so what the environment asks for
(cwd, environment, interpreter, merged output) is really executed."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import ClassVar

import pytest

from minisweagent_prized import environment as environment_module

IMAGE_CONFIG = {
    "config": {
        "Env": ["PATH=/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "TZ=Etc/UTC"],
        "WorkingDir": "/testbed/",
    }
}
UNAME = "Linux box-1 6.1.119 #1 SMP PREEMPT_DYNAMIC Sat Aug 15 04:06:25 UTC 2026 x86_64"


@dataclass
class Result:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False


class FakeFiles:
    def __init__(self, sandbox: FakeSandbox):
        self.sandbox = sandbox

    def write(self, path, data, *, mode=None):
        if isinstance(data, str):
            data = data.encode()
        self.sandbox.files_written[path] = (bytes(data), mode)


class FakeSandbox:
    created: ClassVar[list[FakeSandbox]] = []

    def __init__(self, *, setup_failures: int = 0, setup_error: str = "crane export failed", **kwargs):
        self.id = f"sbx_fake{len(FakeSandbox.created)}"
        self.kwargs = kwargs
        self.calls: list[tuple[str, float | None]] = []
        self.files_written: dict[str, tuple[bytes, int | None]] = {}
        self.files = FakeFiles(self)
        self.setup_failures = setup_failures
        self.setup_error = setup_error
        self.deleted = 0
        self.closed = 0
        self.timeout_next = False
        FakeSandbox.created.append(self)

    def exec(self, cmd, *, timeout=None, full_output=False):
        self.calls.append((cmd, timeout))
        if cmd.startswith("set -e; cd ~;"):
            if self.setup_failures:
                self.setup_failures -= 1
                return Result(stderr=self.setup_error, exit_code=1)
            return Result(stdout=json.dumps(IMAGE_CONFIG) + "\n" + UNAME + "\n")
        prefix = f"python3 {environment_module.RUNTIME} "
        assert cmd.startswith(prefix), cmd
        arg = cmd[len(prefix) :]
        if arg.startswith("- "):
            script = base64.b64decode(arg[2:]).decode()
        else:
            script = self.files_written[environment_module.ROOT + arg][0].decode()
        if self.timeout_next:
            self.timeout_next = False
            raise environment_module.CommandTimeoutError(Result(stdout="partial\n", exit_code=-1), timeout)
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script)
        proc = subprocess.run(
            ["bash", f.name], check=False, capture_output=True, text=True, env={"PATH": os.environ["PATH"]}
        )
        assert not os.path.exists(f.name), "the script removes itself"
        return Result(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)

    def delete(self):
        self.deleted += 1

    def close(self):
        self.closed += 1


@pytest.fixture
def fake_prized(monkeypatch):
    """Sandbox.create returns FakeSandboxes; options for the next ones go in the returned dict."""
    FakeSandbox.created = []
    options: dict = {}

    def create(template, **kwargs):
        if "raise" in options:
            raise options["raise"]
        return FakeSandbox(template=template, **kwargs, **options)

    monkeypatch.setattr(environment_module.Sandbox, "create", staticmethod(create))
    monkeypatch.setattr(environment_module.time, "sleep", lambda s: None)
    return options
