"""Run a command with chown, setuid/setgid mode bits and user switches as no-ops.

Only uid 0 is mapped in the user namespace the task runs in, so chown to any
other user fails (EINVAL), switching to another user (sudo, su, setpriv) fails
the same way, setgroups is refused, and the sandbox refuses setuid/setgid mode
bits (EPERM). Package managers (dpkg), useradd, tar and sudo treat these as
fatal. This seccomp filter makes those calls return success without changing
anything, as fakeroot does, for the command and everything it starts: the
process stays root. Calls that stay on root (uid 0 or -1) run for real. A faked
call also reports success where the real one would have failed for another
reason (a missing file, say).

Setuid/setgid bits are never applied: dpkg creates a file with mode 0 and then
sets its mode, so a setuid file it installs would keep mode 0. The environment
therefore adds a dpkg post-invoke hook that gives such files mode 0755.

PrizedEnvironment uploads this file to the sandbox and runs commands as
``python3 no_chown.py COMMAND [ARGS...]``.
"""

from __future__ import annotations

import ctypes
import os
import signal
import struct
import sys
from typing import Any

# Classic BPF over struct seccomp_data: nr at 0, arch at 4, args[i] at 16 + 8 * i
# (the low 32 bits on little-endian).
_LD_W_ABS, _JEQ_K, _JSET_K, _RET_K = 0x20, 0x15, 0x45, 0x06
_ALLOW, _SUCCEED = 0x7FFF0000, 0x00050000  # SECCOMP_RET_ALLOW, SECCOMP_RET_ERRNO | 0
_SPECIAL_BITS = 0o6000  # S_ISUID | S_ISGID

_ROOT_IDS = (0, 0xFFFFFFFF)  # uid/gid 0, or -1 for "unchanged"

# arch -> (always succeed: chown family and setgroups,
#          {chmod syscall: index of its mode argument},
#          {set*id syscall: how many id arguments it takes})
_SYSCALLS = {
    0xC000003E: (  # x86_64
        [92, 93, 94, 260, 116],
        {90: 1, 91: 1, 268: 2, 452: 2},
        {105: 1, 106: 1, 113: 2, 114: 2, 117: 3, 119: 3, 122: 1, 123: 1},
    ),
    0xC00000B7: (  # aarch64
        [54, 55, 159],
        {52: 1, 53: 2, 452: 2},
        {143: 2, 144: 1, 145: 2, 146: 1, 147: 3, 149: 3, 151: 1, 152: 1},
    ),
}


def build_filter() -> list[tuple[int, int, int, int]]:
    """The filter as (code, jt, jf, k) instructions with relative jumps."""
    # Jump targets are labels until the end; ("label", name) marks a position.
    program: list[tuple[Any, ...]] = [(_LD_W_ABS, 0, 0, 4)]
    for arch in _SYSCALLS:
        program.append((_JEQ_K, f"arch{arch}", 0, arch))
    program.append((_RET_K, 0, 0, _ALLOW))
    for arch, (always, chmods, set_ids) in _SYSCALLS.items():
        program += [("label", f"arch{arch}"), (_LD_W_ABS, 0, 0, 0)]
        program += [(_JEQ_K, "succeed", 0, nr) for nr in always]
        program += [(_JEQ_K, f"mode{arg}", 0, nr) for nr, arg in chmods.items()]
        program += [(_JEQ_K, f"ids{n}_0", 0, nr) for nr, n in set_ids.items()]
        program.append((_RET_K, 0, 0, _ALLOW))
    # A set*id call runs for real if each id it names is 0 or -1, else it succeeds.
    for n in (1, 2, 3):
        for i in range(n):
            done = f"ids{n}_{i + 1}"
            program += [
                ("label", f"ids{n}_{i}"),
                (_LD_W_ABS, 0, 0, 16 + 8 * i),
                (_JEQ_K, done, 0, _ROOT_IDS[0]),
                (_JEQ_K, done, "succeed", _ROOT_IDS[1]),
            ]
        program += [("label", f"ids{n}_{n}"), (_RET_K, 0, 0, _ALLOW)]
    for arg in (1, 2):
        program += [
            ("label", f"mode{arg}"),
            (_LD_W_ABS, 0, 0, 16 + 8 * arg),
            (_JSET_K, "succeed", 0, _SPECIAL_BITS),
            (_RET_K, 0, 0, _ALLOW),
        ]
    program += [("label", "succeed"), (_RET_K, 0, 0, _SUCCEED)]

    labels: dict[str, int] = {}
    code: list[tuple[Any, ...]] = []
    for ins in program:
        if ins[0] == "label":
            labels[ins[1]] = len(code)
        else:
            code.append(ins)

    def jump(target: int | str, at: int) -> int:
        return target if isinstance(target, int) else labels[target] - at - 1

    return [(op, jump(jt, i), jump(jf, i), k) for i, (op, jt, jf, k) in enumerate(code)]


def install() -> None:
    program = build_filter()
    raw = b"".join(struct.pack("HBBI", *ins) for ins in program)
    buffer = ctypes.create_string_buffer(raw)

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    prog = SockFprog(len(program), ctypes.addressof(buffer))
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_NO_NEW_PRIVS (38), then PR_SET_SECCOMP (22) with SECCOMP_MODE_FILTER (2).
    if (
        libc.prctl(38, 1, 0, 0, 0) != 0
        or libc.prctl(22, 2, ctypes.byref(prog), 0, 0) != 0
    ):
        raise OSError(ctypes.get_errno(), "could not install the seccomp filter")


if __name__ == "__main__":
    install()
    # Python ignores SIGPIPE and SIGXFSZ, and an exec keeps ignored signals.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    os.execvp(sys.argv[1], sys.argv[1:])
