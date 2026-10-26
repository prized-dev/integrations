"""Inspect's portable sandbox conformance checks, run against a real Prized sandbox."""

import os

import pytest
import pytest_asyncio
from inspect_ai.util._sandbox.self_check import *  # noqa: F403

from inspect_prized import PrizedSandboxConfig, PrizedSandboxEnvironment

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(os.environ.get("PRIZED_LIVE") != "1", reason="PRIZED_LIVE=1 not set"),
]

# Prized runs commands as the unprivileged user `prized` (no root, no sudo).
XFAIL = {"test_exec_as_user": "no root in Prized sandboxes"}
LABELS = {"test": "inspect-prized"}
if os.environ.get("PRIZED_TEST_TASK"):
    LABELS["task"] = os.environ["PRIZED_TEST_TASK"]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def _shared_env():
    config = PrizedSandboxConfig(vcpu=1, memory_gb=2, labels=LABELS, max_runtime_min=30)
    envs = await PrizedSandboxEnvironment.sample_init("self-check", config, {})
    yield envs["default"]
    await PrizedSandboxEnvironment.sample_cleanup("self-check", config, envs, False)


@pytest_asyncio.fixture(loop_scope="module")
async def sandbox_env(request, _shared_env):
    reason = XFAIL.get(request.node.originalname)
    if reason:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))
    return _shared_env
