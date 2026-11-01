from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from fake_prized import BIN, World  # noqa: E402

from improvements_box import Box, Config  # noqa: E402

DEMO = Path(__file__).parent.parent / "demo-app"
TEST = "python3 -m unittest -v"
# What the host has: a model key, and keys that must never reach a sandbox.
ENVIRON = {"ANTHROPIC_API_KEY": "sk-ant-fake", "PRIZED_API_KEY": "dcp_000fake", "GH_TOKEN": "ghp_000fake"}


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "test@example.com")


@pytest.fixture
def world() -> Iterator[World]:
    w = World()
    yield w
    w.close()


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    """The demo app as a git repository on branch main."""
    repo = tmp_path / "temps"
    shutil.copytree(DEMO, repo, ignore=shutil.ignore_patterns("__pycache__"))
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "temps", cwd=repo)
    return repo


@pytest.fixture
def make(world: World, demo: Path, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    boxes: list[Box] = []

    def build(**overrides: Any) -> Box:
        cfg = Config(
            repo=str(demo),
            test=TEST,
            out=str(tmp_path / "out"),
            labels={"task": "improvements-box-test"},
            gh=str(Path(BIN) / "gh"),
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        box = Box(cfg, sandbox_api=world.api(), environ=ENVIRON, log=lambda line: None)
        boxes.append(box)
        return box

    yield build
    for box in boxes:
        box.close()
