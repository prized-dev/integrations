"""The sandbox handle against the fake SDK, with commands run by this machine's bash under a temporary directory."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import threading

import pytest
from prized import PrizedError

from agentenv_prized import sandbox as sandbox_module
from agentenv_prized.sandbox import ROOT_HELPER, RootMode, start_root_helper_script, tunnel_urls_for

from .conftest import root_mode
from .fakes import Result, error


@pytest.fixture
async def box(provider, cloud, monkeypatch, tmp_path):
    """A sandbox whose uploads stage under the test's directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sandbox_module, "HOME", str(home))
    return await provider.create_vm(setup_for_gateway=False)


# --- commands -------------------------------------------------------------------------------------


async def test_exec_returns_exit_code_and_both_streams(box):
    code, out, err = await box.exec_with_output("bash", "-c", "echo out; echo err >&2; exit 7")
    assert (code, out, err) == (7, "out\n", "err\n")


async def test_exec_keeps_each_argument_whole(box):
    tricky = 'it\'s a "quoted" $HOME `x` ; rm -rf /'
    code, out, _ = await box.exec_with_output("printf", "%s", tricky)
    assert (code, out) == (0, tricky)


async def test_exec_reads_no_input(box):
    code, out, _ = await asyncio.wait_for(box.exec_with_output("bash", "-c", "cat; echo done"), 10)
    assert (code, out) == (0, "done\n")


async def test_a_lost_answer_is_exit_minus_one_and_exec_script_retries_it(box, cloud, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    lost = iter([error("network_error"), error("sandbox_unreachable", 502), None])
    cloud.exec_error = lambda command: next(lost) if "marker" in command else None
    code, out, err = await box.exec_with_output("bash", "-c", "echo marker")
    assert code == -1 and out == "" and "network_error" in err
    assert await box.exec_script("echo marker", max_retries=2) == "marker\n"


async def test_a_real_failure_is_not_retried(box, cloud):
    root_mode(cloud, "none")
    with pytest.raises(RuntimeError, match="exit 3"):
        await box.exec_script("echo once >> tries; exit 3", max_retries=3)
    assert (cloud.workdir / "tries").read_text() == "once\n"


async def test_a_gone_sandbox_raises(box, cloud):
    cloud.sandboxes[box.sandbox_id].state = "deleted"
    with pytest.raises(PrizedError) as caught:
        await box.exec("true")
    assert caught.value.code == "sandbox_deleted"


async def test_concurrent_commands_each_finish(box):
    results = await asyncio.wait_for(
        asyncio.gather(*(box.exec_with_output("bash", "-c", f"sleep 0.{i}; echo {i}") for i in range(6))), 20
    )
    assert [(code, out.strip()) for code, out, _ in results] == [(0, str(i)) for i in range(6)]


# --- root -----------------------------------------------------------------------------------------


async def test_sudo_mode_keeps_sudo(box, cloud):
    root_mode(cloud, "sudo")
    cloud.answer(lambda command: Result("root\n") if command.startswith("sudo ") else None)
    assert await box.exec_script("whoami") == "root\n"
    assert cloud.commands[-1] == "sudo bash -c whoami"


async def test_docker_mode_runs_root_commands_in_the_helper(box, cloud):
    root_mode(cloud, "docker")
    cloud.answer(lambda command: Result() if f"--name {ROOT_HELPER}" in command else None)
    cloud.answer(lambda command: Result("root\n") if command.startswith(f"docker exec {ROOT_HELPER} ") else None)
    assert await box.exec_script("whoami") == "root\n"
    assert await box.exec_script("whoami") == "root\n"
    helper_starts = [c for c in cloud.commands if f"--name {ROOT_HELPER}" in c]
    assert len(helper_starts) == 1
    assert cloud.commands[-1] == f"docker exec {ROOT_HELPER} chroot /host bash -c whoami"
    assert await box.root_mode() is RootMode.DOCKER


def test_root_helper_script_shape():
    script = start_root_helper_script("example.test/debian:12")
    assert "docker inspect -f '{{.State.Running}}' agentenv-prized-root" in script
    assert "--privileged --pid=host --net=host --ipc=host" in script
    assert "-v /:/host example.test/debian:12 sleep infinity" in script


async def test_a_helper_that_cannot_start_is_an_error(box, cloud):
    root_mode(cloud, "docker")
    cloud.answer(
        lambda command: Result(stderr="pull access denied", exit_code=1) if f"--name {ROOT_HELPER}" in command else None
    )
    with pytest.raises(RuntimeError, match="pull access denied"):
        await box.exec_script("true")


async def test_none_mode_runs_as_the_user(box, cloud):
    root_mode(cloud, "none")
    assert await box.exec_script("echo hi") == "hi\n"
    assert cloud.commands[-1] == "bash -c 'echo hi'"


async def test_the_root_probe_runs_once_under_concurrency(box, cloud):
    root_mode(cloud, "none")
    await asyncio.gather(*(box.exec_script(f"echo {i}") for i in range(8)))
    assert sum("sudo -n true" in c for c in cloud.commands) == 1


async def test_sudo_options_are_refused_without_sudo(box, cloud):
    root_mode(cloud, "docker")
    cloud.answer(lambda command: Result() if f"--name {ROOT_HELPER}" in command else None)
    with pytest.raises(ValueError, match="sudo options"):
        await box.exec("sudo", "-u", "nobody", "true")


# --- Docker readiness -----------------------------------------------------------------------------


async def test_wait_for_vm_names_the_docker_template(box, cloud):
    cloud.answer(
        lambda command: (
            Result(
                stderr="this sandbox's template has no Docker; use a template with Docker, such as prized-docker-v1\n",
                exit_code=3,
            )
            if "docker compose version" in command
            else None
        )
    )
    with pytest.raises(RuntimeError, match="prized-docker-v1"):
        await box.wait_for_vm()


async def test_the_readiness_script_fails_fast_without_docker(box, cloud):
    """The real script, run by bash with no docker on PATH: exit 3 at once, not 90 polls."""
    code, _, err = await asyncio.wait_for(
        box.exec_with_output("env", "PATH=/nonexistent", "/bin/bash", "-c", sandbox_module.DOCKER_READY), 10
    )
    assert code == 3 and "prized-docker-v1" in err


# --- files ----------------------------------------------------------------------------------------


async def test_small_writes_go_inline(box, cloud, tmp_path):
    root_mode(cloud, "none")
    target = tmp_path / "out" / "small.txt"
    await box.write_host_file(b"hello\n", str(target))
    assert target.read_bytes() == b"hello\n"
    assert cloud.uploads == []


async def test_large_writes_go_through_the_files_api_in_parts(box, cloud, tmp_path, monkeypatch):
    root_mode(cloud, "none")
    monkeypatch.setattr(sandbox_module, "UPLOAD_PART_BYTES", 100_000)
    payload = os.urandom(250_017)
    target = tmp_path / "out" / "large.bin"
    await box.write_host_file(payload, str(target))
    assert target.read_bytes() == payload
    assert len(cloud.uploads) == 3
    assert all(path.startswith(str(tmp_path / "home")) for path in cloud.uploads)
    assert list((tmp_path / "home").iterdir()) == []


async def test_a_corrupted_upload_is_caught_and_cleaned_up(box, cloud, tmp_path, monkeypatch):
    root_mode(cloud, "none")
    monkeypatch.setattr(sandbox_module, "UPLOAD_PART_BYTES", 100_000)
    cloud.write_hook = lambda path, data: data[:-1] + bytes([data[-1] ^ 1]) if path.endswith(".00001") else data
    with pytest.raises(RuntimeError, match="does not match the upload"):
        await box.write_host_file(os.urandom(250_000), str(tmp_path / "large.bin"))


async def test_a_failed_upload_removes_its_staged_parts(box, cloud, tmp_path, monkeypatch):
    root_mode(cloud, "none")
    monkeypatch.setattr(sandbox_module, "UPLOAD_PART_BYTES", 100_000)

    def fail_second(path: str, data: bytes) -> bytes:
        if path.endswith(".00001"):
            raise error("network_error")
        return data

    cloud.write_hook = fail_second
    with pytest.raises(PrizedError):
        await box.write_host_file(os.urandom(250_000), str(tmp_path / "large.bin"))
    assert list((tmp_path / "home").iterdir()) == []


async def test_unsigned_objects_stream_through_the_files_api(box, cloud, tmp_path, monkeypatch):
    root_mode(cloud, "none")
    monkeypatch.setattr(sandbox_module, "UPLOAD_PART_BYTES", 64_000)
    payload = os.urandom(200_000)

    class Store:
        def open(self, url: str):
            assert url == "file:///objects/a"
            return io.BytesIO(payload)

    target = tmp_path / "object.bin"
    await box._write_unsigned_object(Store(), "file:///objects/a", str(target))
    assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()
    assert len(cloud.uploads) == 4


async def test_an_empty_object_makes_an_empty_file(box, cloud, tmp_path):
    root_mode(cloud, "none")

    class Store:
        def open(self, url: str):
            return io.BytesIO(b"")

    target = tmp_path / "empty"
    await box._write_unsigned_object(Store(), "file:///objects/empty", str(target))
    assert target.read_bytes() == b""


# --- handles and loops ----------------------------------------------------------------------------


async def test_another_event_loop_connects_by_id(box, cloud):
    connects = cloud.connects
    outcome: dict[str, object] = {}

    def other_loop():
        outcome["result"] = asyncio.run(box.exec_with_output("echo", "from another loop"))

    thread = threading.Thread(target=other_loop)
    thread.start()
    await asyncio.to_thread(thread.join)
    assert outcome["result"] == (0, "from another loop\n", "")
    assert cloud.connects == connects + 1


def test_port_urls_come_from_the_api_template():
    template = {"previewUrlTemplate": "https://{port}-sbx-ab.p.example.test/"}
    assert tunnel_urls_for(template, [8000]) == {8000: "https://8000-sbx-ab.p.example.test"}
    assert tunnel_urls_for({}, []) == {}
    for document in ({}, {"previewUrlTemplate": "http://{port}.x/"}, {"previewUrlTemplate": "https://x/"}):
        with pytest.raises(ValueError):
            tunnel_urls_for(document, [8000])


async def _no_sleep(_seconds: float, *args, **kwargs) -> None:
    return None
