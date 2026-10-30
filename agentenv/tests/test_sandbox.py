"""The sandbox handle against the fake SDK, with commands run by this machine's bash under a temporary directory."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shlex
import threading

import pytest
from prized import PrizedError

from agentenv_prized import sandbox as sandbox_module
from agentenv_prized.sandbox import (
    ROOT_ENTER,
    ROOT_HELPER,
    UMASK_PREFIX,
    RootMode,
    start_root_helper_script,
    tunnel_urls_for,
)

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


async def test_commands_run_under_umask_022(box, tmp_path):
    assert await box.exec_with_output("bash", "-c", "umask") == (0, "0022\n", "")
    await box.exec_with_output("bash", "-c", f"umask 077; echo x > {tmp_path}/before")
    code, _, _ = await box.exec_with_output("touch", str(tmp_path / "made"))
    assert code == 0 and (tmp_path / "made").stat().st_mode & 0o777 == 0o644


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


async def test_cut_off_output_is_never_returned_as_whole(box, cloud):
    cloud.answer(lambda command: Result("part", truncated=True, truncated_reason="limit") if "big" in command else None)
    with pytest.raises(RuntimeError, match="passed what an exec returns"):
        await box.exec_with_output("bash", "-c", "echo big")


@pytest.mark.parametrize(
    ("failure", "lost"),
    [
        (error("http_error", 502), True),
        (error("http_error", 503), True),
        (error("rate_limited", 429), True),
        (error("network_error"), True),
        (error("bad_request", 400), False),
        (error("sandbox_not_running", 409), False),
        (error("unauthorized", 401), False),
    ],
)
async def test_which_failures_leave_the_outcome_unknown(box, cloud, failure, lost):
    cloud.exec_error = lambda command: failure if "probe-me" in command else None
    if lost:
        assert (await box.exec_with_output("echo", "probe-me"))[0] == -1
    else:
        with pytest.raises(PrizedError):
            await box.exec_with_output("echo", "probe-me")


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


def helper_starts(cloud) -> list[str]:
    return [c for c in cloud.commands if f"--name {ROOT_HELPER}" in c]


async def test_docker_mode_runs_root_commands_in_the_vms_namespaces(box, cloud):
    root_mode(cloud, "docker")
    cloud.answer(lambda command: Result() if f"--name {ROOT_HELPER}" in command else None)
    cloud.answer(lambda command: Result("root\n") if command.startswith(sent("docker", "exec", ROOT_HELPER)) else None)
    assert await box.exec_script("whoami") == "root\n"
    assert await box.exec_script("whoami") == "root\n"
    assert len(helper_starts(cloud)) == 1
    assert cloud.commands[-1] == sent("docker", "exec", ROOT_HELPER, *ROOT_ENTER, "bash", "-c", "whoami")
    assert ROOT_ENTER[:3] == ("nsenter", "--target", "1")
    assert await box.root_mode() is RootMode.DOCKER


async def test_a_gone_helper_is_started_again_and_the_command_run_once_more(box, cloud):
    root_mode(cloud, "docker")
    cloud.answer(lambda command: Result() if f"--name {ROOT_HELPER}" in command else None)
    answers = iter(
        [
            Result(stderr=f"Error response from daemon: No such container: {ROOT_HELPER}\n", exit_code=1),
            Result("root\n"),
        ]
    )
    cloud.answer(lambda command: next(answers) if command.startswith(sent("docker", "exec", ROOT_HELPER)) else None)
    assert await box.exec_script("whoami") == "root\n"
    assert len(helper_starts(cloud)) == 2


async def test_a_root_command_that_fails_on_its_own_is_not_rerun(box, cloud):
    root_mode(cloud, "docker")
    cloud.answer(lambda command: Result() if f"--name {ROOT_HELPER}" in command else None)
    cloud.answer(
        lambda command: (
            Result(stderr="no such file\n", exit_code=1)
            if command.startswith(sent("docker", "exec", ROOT_HELPER))
            else None
        )
    )
    code, _, err = await box.exec_with_output("sudo", "cat", "/nope")
    assert (code, err) == (1, "no such file\n")
    assert len(helper_starts(cloud)) == 1


def test_root_helper_script_shape():
    script = start_root_helper_script("example.test/debian@sha256:abc")
    assert "docker inspect -f '{{.State.Running}}' agentenv-prized-root" in script
    assert "--privileged --pid=host --net=host --ipc=host" in script
    assert "example.test/debian@sha256:abc sleep infinity" in script
    assert "-v /:/host" not in script
    assert "docker pull -q example.test/debian@sha256:abc" in script
    assert "&& ! running; then docker rm -f" in script


def test_the_default_helper_image_is_pinned():
    assert "@sha256:" in sandbox_module.ROOT_HELPER_IMAGE


async def test_the_helper_script_starts_once_and_waits_for_a_racing_start(box, tmp_path):
    """The real script, against a stand-in docker on PATH: a stopped container is replaced, a running one kept, and a
    run that loses the name to another start waits for that one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    (bin_dir / "docker").write_text(
        "#!/bin/bash\n"
        f"S={state}\n"
        'case "$1" in\n'
        '  inspect) [ -f $S ] || exit 1; [ "$2" = -f ] && cat $S; exit 0 ;;\n'
        "  pull) exit 0 ;;\n"
        "  rm) echo rm >> $S.log; rm -f $S; exit 0 ;;\n"
        '  run) echo run >> $S.log; if [ -n "$RACE" ]; then echo true > $S; exit 125; fi; echo true > $S; exit 0 ;;\n'
        "esac\n"
    )
    (bin_dir / "docker").chmod(0o755)
    script = start_root_helper_script("img@sha256:abc")
    path = f"PATH={bin_dir}:/usr/bin:/bin"
    assert (await box.exec_with_output("env", path, "bash", "-c", script))[0] == 0
    assert (state.parent / "state.log").read_text() == "run\n"
    assert (await box.exec_with_output("env", path, "bash", "-c", script))[0] == 0
    assert (state.parent / "state.log").read_text() == "run\n"
    state.write_text("false\n")
    assert (await box.exec_with_output("env", path, "bash", "-c", script))[0] == 0
    assert (state.parent / "state.log").read_text() == "run\nrm\nrun\n"
    state.unlink()
    assert (await box.exec_with_output("env", path, "RACE=1", "bash", "-c", script))[0] == 0


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
    assert cloud.commands[-1] == sent("bash", "-c", "echo hi")


def probes(cloud) -> int:
    return sum("echo none" in c and "docker info" in c for c in cloud.commands)


async def test_the_root_probe_runs_once_under_concurrency(box, cloud):
    root_mode(cloud, "none")
    await asyncio.gather(*(box.exec_script(f"echo {i}") for i in range(8)))
    assert probes(cloud) == 1


async def test_a_lost_probe_answer_is_asked_again(box, cloud):
    lost = iter([error("network_error")])
    cloud.exec_error = lambda command: next(lost, None) if "echo none" in command else None
    root_mode(cloud, "none")
    assert await box.exec_script("echo hi") == "hi\n"
    assert probes(cloud) == 2


async def test_sudo_options_are_refused(box, cloud):
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
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["large.bin"]


async def test_an_upload_holds_one_part_at_a_time(box, cloud, tmp_path, monkeypatch):
    """Each part is joined and removed before the next is uploaded, so the disk never holds all of them twice."""
    root_mode(cloud, "none")
    monkeypatch.setattr(sandbox_module, "UPLOAD_PART_BYTES", 100_000)
    seen = []

    def count_staged(path: str, data: bytes) -> bytes:
        seen.append(len(list((tmp_path / "home").iterdir())))
        return data

    cloud.write_hook = count_staged
    await box.write_host_file(os.urandom(450_000), str(tmp_path / "big.bin"))
    assert seen == [0, 0, 0, 0, 0]


async def test_a_corrupted_upload_is_caught_and_cleaned_up(box, cloud, tmp_path, monkeypatch):
    root_mode(cloud, "none")
    monkeypatch.setattr(sandbox_module, "UPLOAD_PART_BYTES", 100_000)
    cloud.write_hook = lambda path, data: data[:-1] + bytes([data[-1] ^ 1]) if path.endswith(".00001") else data
    with pytest.raises(RuntimeError, match="does not match the upload"):
        await box.write_host_file(os.urandom(250_000), str(tmp_path / "large.bin"))
    assert list((tmp_path / "home").iterdir()) == []


async def test_a_failed_upload_removes_its_staged_parts(box, cloud, tmp_path, monkeypatch):
    root_mode(cloud, "none")
    monkeypatch.setattr(sandbox_module, "UPLOAD_PART_BYTES", 100_000)

    def fail_second(path: str, data: bytes) -> bytes:
        if path.endswith(".00001"):
            raise error("network_error")
        return data

    cloud.write_hook = fail_second
    with pytest.raises(PrizedError):
        await box.write_host_file(os.urandom(250_000), str(tmp_path / "out" / "large.bin"))
    assert list((tmp_path / "home").iterdir()) == []
    assert list((tmp_path / "out").iterdir()) == []


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


async def test_a_reader_that_returns_short_reads_still_makes_full_parts(box, cloud, tmp_path, monkeypatch):
    root_mode(cloud, "none")
    monkeypatch.setattr(sandbox_module, "UPLOAD_PART_BYTES", 64_000)
    payload = os.urandom(200_000)

    class Trickle(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 1000) if size and size > 0 else 1000)

    class Store:
        def open(self, url: str):
            return Trickle(payload)

    target = tmp_path / "object.bin"
    await box._write_unsigned_object(Store(), "file:///objects/a", str(target))
    assert target.read_bytes() == payload
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


def test_port_urls_are_refused_until_prized_serves_public_ones():
    assert tunnel_urls_for({}, []) == {}
    with pytest.raises(ValueError, match="carry a Prized API key"):
        tunnel_urls_for({"previewUrlTemplate": "https://{port}-sbx-ab.p.example.test/"}, [8000])
    with pytest.raises(ValueError, match="no port URLs"):
        tunnel_urls_for({}, [8000])


async def test_terminate_takes_any_404_as_gone(box, cloud):
    class Gone:
        async def delete(self):
            raise error("http_error", 404)

        async def close(self):
            pass

    box._sdks[asyncio.get_running_loop()] = Gone()
    await box.terminate()


async def _no_sleep(_seconds: float, *args, **kwargs) -> None:
    return None


def sent(*argv: str) -> str:
    """The command text the provider sends for ``argv``."""
    return shlex.join([*UMASK_PREFIX, *argv])
