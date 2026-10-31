# Copyright 2026 Prized
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests never reach Prized, and don't need Stirrup installed.

``tests/stub`` holds a stand-in for the slice of Stirrup the backend imports (see its
docstring); it goes first on sys.path unless STIRRUP_PRIZED_REAL_STIRRUP=1, which runs the
same tests against an installed Stirrup instead. Two guards stop any call a test forgets to
fake: the key is fake and the API URL a dead local port, and httpx refuses to send anything.
"""

import os
import sys
from pathlib import Path

import httpx
import pytest

if os.environ.get("STIRRUP_PRIZED_REAL_STIRRUP") != "1":
    sys.path.insert(0, str(Path(__file__).parent / "stub"))

from tests import fakes  # noqa: E402

BLOCKED: list[str] = []


def _refuse(self, request, *args, **kwargs):
    BLOCKED.append(str(request.url))
    raise httpx.ConnectError(f"unit tests do not send requests: {request.url}", request=request)


async def _refuse_async(self, request, *args, **kwargs):
    return _refuse(self, request)


@pytest.fixture(autouse=True)
def _offline(request, monkeypatch):
    if request.node.get_closest_marker("live") is not None:
        return
    monkeypatch.setenv("PRIZED_API_KEY", "dcp_offline_unit_test_key")
    monkeypatch.setattr("prized._core.DEFAULT_BASE_URL", "http://127.0.0.1:9", raising=False)
    monkeypatch.setattr(httpx.Client, "send", _refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", _refuse_async)


@pytest.fixture
def world(monkeypatch):
    """Fake sandboxes behind the backend's prized.AsyncSandbox."""
    w = fakes.World()
    monkeypatch.setattr(fakes.FakeAsyncSandbox, "world", w, raising=False)
    monkeypatch.setattr("stirrup_prized._provider.AsyncSandbox", fakes.FakeAsyncSandbox)
    yield w
    w.cleanup()
