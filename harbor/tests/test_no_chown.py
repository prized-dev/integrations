"""The seccomp filter in _no_chown.py, run through a small classic-BPF interpreter."""

import struct

import pytest

from harbor_prized import _environment
from harbor_prized._no_chown import build_filter

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


def test_enter_script_runs_commands_under_the_filter():
    script = _environment._ENTER_SCRIPT
    assert f"exec python3 {_environment._NO_CHOWN} nsenter --target" in script
    assert "def build_filter" in _environment._NO_CHOWN_SCRIPT


def test_commands_share_one_namespace_holder():
    script = _environment._ENTER_SCRIPT
    assert "--user --mount --preserve-credentials" in script
    # The holder must not inherit the lock, or the next command waits forever.
    assert "9>&- &" in script
    assert f"exec {_environment._HOLDER} infinity" in script
