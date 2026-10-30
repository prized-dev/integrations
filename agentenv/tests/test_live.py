"""Live checks against the real Prized API. They create real sandboxes, billed to the key's workspace, and delete them.

Opt in with ``PRIZED_LIVE=1`` and ``PRIZED_API_KEY``. ``PRIZED_TEMPLATE`` picks the template (default: the provider's,
``prized-docker-v1``). Checks that need Docker in the template, or public port URLs, skip with the capability they
lack when the workspace or template doesn't have it, in agent-env's ``agentenv-capability-missing: <name>`` form.
Every sandbox carries the label ``agentenv-live-run=<this run>``, and the module's last step deletes any left.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import statistics
import time
import uuid

import pytest
from agent_env.providers.sandbox_providers.sandbox import NetworkPolicy, read_vm_file
from agent_env.providers.sandbox_providers.sandbox_provider import SandboxNotFoundError
from prized import AsyncSandbox

from agentenv_prized.provider import MARK_LABEL, PrizedSandboxProvider
from agentenv_prized.sandbox import RootMode

LIVE = os.environ.get("PRIZED_LIVE") == "1" and bool(os.environ.get("PRIZED_API_KEY"))
TEMPLATE = os.environ.get("PRIZED_TEMPLATE") or "prized-docker-v1"
RUN = f"live-{uuid.uuid4().hex[:10]}"
ATTRIBUTION = {"live-run": RUN, "task": os.environ.get("PRIZED_LIVE_TASK", "agentenv-prized")}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="agentenv-capability-missing: prized_live (PRIZED_LIVE=1 and PRIZED_API_KEY)"),
]


def provider() -> PrizedSandboxProvider:
    return PrizedSandboxProvider(api_key=os.environ["PRIZED_API_KEY"], template=TEMPLATE)


async def _template_has_docker(sandbox) -> bool:
    code, _, _ = await sandbox.exec_with_output("bash", "-c", "command -v docker && docker info")
    return code == 0


@pytest.fixture(scope="module")
def run_label():
    yield RUN
    if LIVE:
        left = asyncio.run(AsyncSandbox.delete_by_labels({"agentenv-live-run": RUN}))
        assert left == [], f"sandboxes left by the live run were deleted: {left}"


async def test_lifecycle_commands_files_reconnect_and_teardown(run_label, tmp_path):
    """Create, run commands (exit codes, quoting, concurrency, a command past the 240 s blocking limit), write and read
    files both ways, reconnect from a fresh provider, then terminate twice and get SandboxNotFoundError."""
    made = await provider().create_vm(
        cpu=1, memory=2048, disk_size_gb=10, timeout=1200, setup_for_gateway=False, attribution=ATTRIBUTION
    )
    try:
        assert (made.type, made.mode, made.tunnel_urls) == ("prized", "vm", {})
        assert made.network_policy == NetworkPolicy()
        sdk = await AsyncSandbox.connect(made.sandbox_id)
        assert sdk.labels[MARK_LABEL] == "true" and sdk.labels["agentenv-live-run"] == RUN
        assert sdk.template == TEMPLATE
        await sdk.close()

        assert await made.exec_with_output("bash", "-c", "echo out; echo err >&2; exit 7") == (7, "out\n", "err\n")
        tricky = 'it\'s a "quoted" $HOME `x` ; rm -rf /'
        assert await made.exec_with_output("printf", "%s", tricky) == (0, tricky, "")
        results = await asyncio.gather(*(made.exec_with_output("bash", "-c", f"cat; echo {i}") for i in range(6)))
        assert [(code, out.strip()) for code, out, _ in results] == [(0, str(i)) for i in range(6)]

        timings = []
        for _ in range(10):
            started = time.monotonic()
            assert (await made.exec_with_output("true"))[0] == 0
            timings.append(time.monotonic() - started)
        print(f"\nexec round trip: p50 {statistics.median(timings):.3f}s max {max(timings):.3f}s")

        mode = await made.root_mode()
        print(f"root mode on {TEMPLATE}: {mode.value}")
        home = "/home/prized/agentenv-live"
        small, large = os.urandom(4096), os.urandom(5 * 1024 * 1024 + 17)
        await made.write_host_file(small, f"{home}/small.bin")
        await made.write_host_file(large, f"{home}/large.bin")
        for name, payload in (("small.bin", small), ("large.bin", large)):
            remote = (await made.exec_script(f"sha256sum {home}/{name}")).split()[0]
            assert remote == hashlib.sha256(payload).hexdigest()
        await made.exec_script(f"head -c 3145739 /dev/urandom > {home}/back.bin")
        remote = (await made.exec_script(f"sha256sum {home}/back.bin")).split()[0]
        await read_vm_file(made, f"{home}/back.bin", str(tmp_path / "back.bin"))
        assert hashlib.sha256((tmp_path / "back.bin").read_bytes()).hexdigest() == remote
        if mode is not RootMode.NONE:
            await made.write_host_file(small, "/opt/agentenv-live/small.bin")
            owner = await made.exec_script("stat -c %U /opt/agentenv-live/small.bin")
            assert owner.strip() == "root"

        started = time.monotonic()
        assert await made.exec_with_output("bash", "-c", "sleep 250; echo still here") == (0, "still here\n", "")
        print(f"a 250 s command returned after {time.monotonic() - started:.0f}s")

        again = await provider().get_sandbox(made.sandbox_id)
        assert (again.tunnel_urls, again.network_policy) == ({}, NetworkPolicy())
        code, out, _ = await again.exec_with_output("sha256sum", f"{home}/small.bin")
        assert (code, out.split()[0]) == (0, hashlib.sha256(small).hexdigest())
        await again.close()
    finally:
        await made.terminate()
    await made.terminate()
    with pytest.raises(SandboxNotFoundError):
        await provider().get_sandbox(made.sandbox_id)
    await provider().terminate_sandbox(made.sandbox_id)


async def test_setup_without_docker_fails_and_deletes_its_sandbox(run_label):
    """On a template without Docker, the gateway setup fails with the template named, and its sandbox is deleted."""
    probe = await provider().create_vm(
        cpu=1, memory=1024, timeout=600, setup_for_gateway=False, attribution=ATTRIBUTION
    )
    has_docker = await _template_has_docker(probe)
    await probe.terminate()
    if has_docker:
        pytest.skip(f"agentenv-capability-missing: a template without Docker ({TEMPLATE} has it)")
    marker = {**ATTRIBUTION, "case": "no-docker"}
    with pytest.raises(RuntimeError, match="prized-docker-v1"):
        await provider().create_vm(cpu=1, memory=1024, timeout=600, attribution=marker)
    alive = await AsyncSandbox.list(
        labels={"agentenv-live-run": RUN, "agentenv-case": "no-docker"},
        state=["starting", "running", "pausing", "paused"],
    )
    assert alive == []


async def test_exposed_ports_need_port_urls(run_label):
    """Without public port URLs on the workspace, exposed ports are refused and the sandbox deleted; with them, a
    container's port answers at its URL."""
    try:
        made = await provider().create_vm(
            cpu=1,
            memory=1024,
            timeout=600,
            exposed_ports=[8080],
            setup_for_gateway=False,
            attribution={**ATTRIBUTION, "case": "ports"},
        )
    except ValueError as exc:
        assert "no public port URLs" in str(exc)
        alive = await AsyncSandbox.list(
            labels={"agentenv-live-run": RUN, "agentenv-case": "ports"},
            state=["starting", "running", "pausing", "paused"],
        )
        assert alive == []
        pytest.skip("agentenv-capability-missing: prized_public_ports (the workspace has no sandbox port URLs)")
    try:
        assert list(made.tunnel_urls) == [8080]
    finally:
        await made.terminate()


async def test_docker_compose_and_root_on_a_docker_template(run_label):
    """With Docker: Compose v2 is there, a container runs, and a root write lands root-owned."""
    probe = await provider().create_vm(
        cpu=2, memory=4096, timeout=900, setup_for_gateway=False, attribution=ATTRIBUTION
    )
    try:
        if not await _template_has_docker(probe):
            pytest.skip(f"agentenv-capability-missing: prized_docker_template ({TEMPLATE} has no Docker)")
        await probe.setup_vm_for_gateway([])
        out = await probe.exec_script("docker run --rm public.ecr.aws/docker/library/busybox:1.37 echo hi")
        assert out.strip() == "hi"
        await probe.exec_script("mkdir -p /app/greeting && echo hello > /app/greeting/hello.txt")
        assert (await probe.exec_script("stat -c %U /app/greeting/hello.txt")).strip() == "root"
    finally:
        await probe.terminate()
