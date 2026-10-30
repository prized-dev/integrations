"""The Prized environment's sandbox-side runtime: its seccomp filter, run through a
classic-BPF interpreter, and its namespace holders."""

import os
import shutil
import struct
import subprocess
import sys
import time

import pytest

from harbor_prized import _environment
from harbor_prized import _runtime as runtime
from harbor_prized._runtime import build_filter

X86_64, AARCH64, I386 = 0xC000003E, 0xC00000B7, 0x40000003
SUCCEED, ALLOW = 0x00050000, 0x7FFF0000


def _run(nr: int, arch: int, mode_arg: int = 0, mode: int = 0, args=None) -> int:
    if args is None:
        args = [0] * 6
        args[mode_arg] = mode
    args = list(args) + [0] * (6 - len(args))
    data = struct.pack("<iIQ6Q", nr, arch, 0, *args)
    program, acc, pc = build_filter(), 0, 0
    while True:
        code, jt, jf, k = program[pc]
        if code == 0x20:  # BPF_LD | BPF_W | BPF_ABS
            acc, pc = struct.unpack_from("<I", data, k)[0], pc + 1
        elif code == 0x15:  # BPF_JMP | BPF_JEQ | BPF_K
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x45:  # BPF_JMP | BPF_JSET | BPF_K
            pc += 1 + (jt if acc & k else jf)
        elif code == 0x06:  # BPF_RET | BPF_K
            return k
        else:
            raise AssertionError(f"unexpected opcode {code:#x}")


def test_jumps_fit_in_a_byte():
    assert all(0 <= jt < 256 and 0 <= jf < 256 for _, jt, jf, _ in build_filter())


@pytest.mark.parametrize("nr", [92, 93, 94, 260])  # chown, fchown, lchown, fchownat
def test_chown_succeeds_without_effect_on_x86_64(nr):
    assert _run(nr, X86_64) == SUCCEED


@pytest.mark.parametrize(
    ("nr", "arg", "mode", "want"),
    [
        (90, 1, 0o4755, SUCCEED),  # chmod u+s
        (90, 1, 0o755, ALLOW),
        (91, 1, 0o2755, SUCCEED),  # fchmod g+s
        (268, 2, 0o6755, SUCCEED),  # fchmodat
        (268, 2, 0o1777, ALLOW),  # the sticky bit is fine
        (452, 2, 0o4000, SUCCEED),  # fchmodat2
    ],
)
def test_only_setuid_and_setgid_modes_are_skipped(nr, arg, mode, want):
    assert _run(nr, X86_64, arg, mode) == want


NOBODY, UNCHANGED = 65534, 0xFFFFFFFF


@pytest.mark.parametrize(
    ("nr", "args", "want"),
    [
        (105, [0], ALLOW),  # setuid(0) stays real
        (105, [NOBODY], SUCCEED),  # setuid to another user: stays root
        (106, [1000], SUCCEED),  # setgid
        (117, [UNCHANGED, 1, UNCHANGED], SUCCEED),  # sudo's setresuid(-1, 1, -1)
        (117, [0, 0, 0], ALLOW),
        (117, [UNCHANGED, 0, UNCHANGED], ALLOW),
        (119, [0, 0, 1000], SUCCEED),  # setresgid, third id
        (113, [UNCHANGED, 1000], SUCCEED),  # setreuid
        (122, [1000], SUCCEED),  # setfsuid
        (116, [0, 0], SUCCEED),  # setgroups is refused in the namespace: always ok
    ],
)
def test_switching_to_another_user_succeeds_without_effect(nr, args, want):
    assert _run(nr, X86_64, args=args) == want


def test_other_syscalls_and_arches_pass_through():
    assert _run(59, X86_64) == ALLOW  # execve
    assert _run(107, X86_64, args=[1000]) == ALLOW  # geteuid
    assert _run(146, AARCH64, args=[1000]) == SUCCEED  # setuid on aarch64
    assert _run(159, AARCH64) == SUCCEED  # setgroups on aarch64
    assert _run(54, AARCH64) == SUCCEED  # fchownat on aarch64
    assert _run(53, AARCH64, 2, 0o4755) == SUCCEED
    assert _run(92, AARCH64) == ALLOW  # 92 is not chown there
    assert _run(182, I386) == ALLOW


def test_the_environment_uploads_this_runtime():
    assert "def build_filter" in _environment._RUNTIME_SCRIPT
    assert (runtime.HOLDER, runtime.USER_HOLDER) == _environment._HOLDERS
    assert (runtime.ROOT, runtime.XFER) == (_environment._ROOT, _environment._XFER)


def test_user_holder_nests_in_roots_namespace():
    command = runtime.user_holder_command(4242, 1001, 1002)
    assert command[:6] == [
        "nsenter",
        "--target=4242",
        "--user",
        "--mount",
        "--preserve-credentials",
        "--",
    ]
    assert command[6:11] == [
        "unshare",
        "--user",
        "--map-user=1001",
        "--map-group=1002",
        "--fork",
    ]
    assert command[-2].endswith(f"exec {runtime.USER_HOLDER} infinity")


linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="namespace holders run on the Linux sandbox"
)


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """runtime's paths under tmp_path, with a holder binary named runtime-ns."""
    sleep = shutil.which("sleep")
    if sleep is None:
        pytest.skip("no sleep binary")
    holder, user_holder = tmp_path / "runtime-ns", tmp_path / "runtime-nsu"
    shutil.copy(sleep, holder)
    shutil.copy(sleep, user_holder)
    monkeypatch.setattr(runtime, "HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "HOLDER", str(holder))
    monkeypatch.setattr(runtime, "USER_HOLDER", str(user_holder))
    yield tmp_path
    for binary in (holder, user_holder):
        subprocess.run(["pkill", "-f", str(binary)], check=False)


def _fake_holder(holder) -> list[str]:
    # Reports its pid in $1, then becomes the holder (as the real commands do).
    return [
        "/bin/sh",
        "-c",
        f'echo $$ > "$1.tmp" && mv "$1.tmp" "$1"; exec {holder} infinity',
        "sh",
    ]


@linux_only
def test_holder_is_started_once_and_reused(sandbox_home):
    command = _fake_holder(runtime.HOLDER)
    first = runtime.ensure_holder("ns", command)
    with open(f"/proc/{first}/comm") as comm:
        assert comm.read().strip() == "runtime-ns"
    assert runtime.ensure_holder("ns", command) == first
    pid, start = (sandbox_home / "ns.pid").read_text().split()
    assert (int(pid), start) == (first, runtime._proc_stat(first)[2])


@linux_only
def test_a_dead_holder_is_replaced(sandbox_home):
    command = _fake_holder(runtime.HOLDER)
    first = runtime.ensure_holder("ns", command)
    os.kill(first, 9)
    deadline = time.monotonic() + 5
    while runtime._holder_pid(str(sandbox_home / "ns.pid"), "runtime-ns") is not None:
        assert time.monotonic() < deadline  # a zombie counts as dead too
        time.sleep(0.02)
    assert runtime.ensure_holder("ns", command) != first


@linux_only
def test_a_holder_with_another_name_is_not_root(sandbox_home):
    # A dead root holder's pid reused by a user holder must not pass as root's.
    user = runtime.ensure_holder(
        "ns-1-1000-1000", _fake_holder(runtime.USER_HOLDER), comm="runtime-nsu"
    )
    (sandbox_home / "ns.pid").write_text(str(user))
    assert runtime._holder_pid(str(sandbox_home / "ns.pid"), "runtime-ns") is None


@linux_only
def test_a_holder_that_never_starts_is_reported_and_killed(sandbox_home):
    marker = sandbox_home / "still-running"
    command = ["/bin/sh", "-c", f"echo boom >&2; sleep 2; touch {marker}", "sh"]
    with pytest.raises(
        RuntimeError, match="could not start the namespace holder ns: boom"
    ):
        runtime.ensure_holder("ns", command, timeout=0.5)
    time.sleep(2.5)
    assert not marker.exists()  # the late attempt was killed, not left behind


@linux_only
def test_user_holders_of_an_earlier_root_holder_are_stopped(sandbox_home):
    stale = runtime.ensure_holder(
        "ns-111-1000-1000", _fake_holder(runtime.USER_HOLDER), comm="runtime-nsu"
    )
    current = runtime.ensure_holder(
        "ns-222-1000-1000", _fake_holder(runtime.USER_HOLDER), comm="runtime-nsu"
    )
    runtime.stop_stale_user_holders(222)
    deadline = time.monotonic() + 5
    while (sandbox_home / "ns-111-1000-1000.pid").exists() or _alive(stale):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert _alive(current)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rpartition(")")[2].split()[0] not in "ZX"
    except OSError:
        return False


@linux_only
def test_the_holder_does_not_keep_the_lock(sandbox_home):
    import fcntl

    with open(sandbox_home / "ns.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        runtime.ensure_holder("ns", _fake_holder(runtime.HOLDER))
    with open(sandbox_home / "ns.lock", "a") as again:
        fcntl.flock(again, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if inherited


def test_an_inline_script_is_staged_inside_the_image(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", str(tmp_path))
    path = runtime._staged_script("-", "ZWNobyBoaQ==")  # "echo hi"
    assert path.startswith(f"{runtime.XFER}/cmd-")
    assert (tmp_path / path.lstrip("/")).read_text() == "echo hi"
    assert (
        runtime._staged_script("/.runtime-xfer/big.sh", None) == "/.runtime-xfer/big.sh"
    )


@linux_only
def test_a_reused_pid_with_another_start_time_is_not_the_holder(sandbox_home):
    pid = runtime.ensure_holder("ns", _fake_holder(runtime.HOLDER))
    (sandbox_home / "ns.pid").write_text(f"{pid} 1")  # recorded start time differs
    assert runtime._holder_pid(str(sandbox_home / "ns.pid"), "runtime-ns") is None


@linux_only
def test_a_command_that_cannot_enter_is_removed_and_reported(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(runtime, "ROOT", str(tmp_path))

    def cannot_enter(user, fcntl):
        raise RuntimeError("could not start the namespace holder ns: boom")

    monkeypatch.setattr(runtime, "_enter", cannot_enter)
    with pytest.raises(SystemExit) as exited:
        runtime.main(["-", "ZWNobyBzZWNyZXQ="])  # "echo secret"
    assert exited.value.code == 125
    assert "prized runtime:" in capsys.readouterr().err
    assert list((tmp_path / runtime.XFER.lstrip("/")).iterdir()) == []
