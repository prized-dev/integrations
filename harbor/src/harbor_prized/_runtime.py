"""Run one command of a Harbor trial inside the task's image, in a Prized sandbox.

harbor_prized uploads this file to the sandbox, where it runs with the
sandbox's own python3 (3.12 or later; standard library only)::

    python3 runtime.py [--user UID:GID] (- BASE64_SCRIPT | /SCRIPT/IN/IMAGE)

The task's image is unpacked at ``ROOT``. A Prized sandbox has no Docker and its
user has no root, so a command runs chrooted into ``ROOT`` in an unprivileged
user namespace that maps the sandbox's user to uid 0. With ``--user`` it runs in
a namespace nested in that one, which maps the same user to ``UID:GID`` instead,
so the command really runs as that uid, without root's capabilities.

Every command of a trial joins the same namespaces, kept open by holder
processes (copies of ``sleep`` named ``runtime-ns`` for root and
``runtime-nsu`` for each other user), as the processes of one container share theirs: the sandbox mounts
``/proc`` with ``hidepid=invisible``, so processes in sibling namespaces can't
see each other, and ``pgrep`` or ``kill`` in a later command would miss what an
earlier one started.

Only one id exists in these namespaces: files that root owns appear owned by
the user in the user's namespace, and other ids can't be given out. So root
commands run under a seccomp filter that makes ``chown`` to any user,
``setgroups``, switches to other users (``sudo``, ``su``, ``setpriv``) and
setuid/setgid mode bits succeed without effect, as fakeroot does; without it
dpkg, useradd, tar and sudo fail. Calls that stay on root (uid 0 or -1) run for
real. A faked call also reports success where the real one would have failed
for another reason (a missing file, say). dpkg creates a file with mode 0 and
then sets its mode, so a setuid file it installs would keep mode 0: the
environment adds a dpkg post-invoke hook that gives such files mode 0755.
"""

from __future__ import annotations

import base64
import ctypes
import os
import random
import signal
import struct
import subprocess
import sys
import time
from typing import Any

HOME = "/home/prized"
ROOT = f"{HOME}/rootfs"  # the image's filesystem, as seen from the sandbox
XFER = "/.runtime-xfer"  # staging directory inside the image
HOLDER = f"{HOME}/runtime-ns"  # a copy of sleep: holds root's namespaces open
USER_HOLDER = f"{HOME}/runtime-nsu"  # another copy: holds a user's namespace open
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Starts root's namespace holder: $0 is ROOT, $1 the file to report its pid in.
_ROOT_HOLDER = [
    "unshare",
    "-Urm",
    "--fork",
    "/bin/bash",
    "-c",
    (
        'R=$0; mount -t proc proc "$R/proc" 2>/dev/null || mount --rbind /proc "$R/proc"; '
        'mount --rbind /dev "$R/dev"; mount --rbind /sys "$R/sys" 2>/dev/null || true; '
        f'echo $$ > "$1.tmp" && mv "$1.tmp" "$1"; exec {HOLDER} infinity'
    ),
    ROOT,
]

# -- the seccomp filter for root commands --------------------------------------

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


def install_filter() -> None:
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


# -- namespace holders ---------------------------------------------------------


def user_holder_command(root_pid: int, uid: int, gid: int) -> list[str]:
    """Starts a holder for UID:GID, nested in root's namespace (pid file in $1)."""
    return [
        "nsenter",
        f"--target={root_pid}",
        "--user",
        "--mount",
        "--preserve-credentials",
        "--",
        "unshare",
        "--user",
        f"--map-user={uid}",
        f"--map-group={gid}",
        "--fork",
        "/bin/sh",
        "-c",
        f'echo $$ > "$1.tmp" && mv "$1.tmp" "$1"; exec {USER_HOLDER} infinity',
        "sh",
    ]


def _proc_stat(pid: int) -> tuple[str, str, str]:
    """comm, state and start time (clock ticks after boot) of a process."""
    with open(f"/proc/{pid}/stat") as f:
        name, _, rest = f.read().partition("(")[2].rpartition(")")
    fields = rest.split()  # fields from the 3rd on: state is [0], starttime [19]
    return name, fields[0], fields[19]


def _holder_pid(pid_file: str, comm: str) -> int | None:
    """The pid in pid_file, if that process is still the live holder named comm
    that was recorded there (its start time too, when recorded)."""
    try:
        with open(pid_file) as f:
            pid_text, _, started = f.read().strip().partition(" ")
        pid = int(pid_text)
        name, state, start = _proc_stat(pid)
    except (OSError, ValueError, IndexError):
        return None
    if name != comm or state in "ZX" or (started and started != start):
        return None
    return pid


def ensure_holder(
    name: str, command: list[str], comm: str = "runtime-ns", timeout: float = 30
) -> int:
    """The pid of holder ``name``, started with ``command`` if it isn't running.

    Called with the namespace lock held. Each attempt reports in its own file,
    so a slow earlier holder can't overwrite a later one's pid, and an attempt
    that doesn't come up in time is killed.
    """
    pid_file = f"{HOME}/{name}.pid"
    pid = _holder_pid(pid_file, comm)
    if pid is not None:
        return pid
    attempt, err_file = f"{pid_file}.{os.getpid()}", f"{HOME}/{name}.err"
    for stale in (pid_file, attempt):
        if os.path.exists(stale):
            os.remove(stale)
    with open(err_file, "wb") as err:
        process = subprocess.Popen(
            [*command, attempt],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = _holder_pid(attempt, comm)
        if pid is not None:
            with open(attempt, "w") as f:  # pid and start time, against pid reuse
                f.write(f"{pid} {_proc_stat(pid)[2]}")
            os.replace(attempt, pid_file)
            return pid
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    with open(err_file, errors="replace") as f:
        raise RuntimeError(f"could not start the namespace holder {name}: {f.read()}")


def stop_stale_user_holders(root_pid: int) -> None:
    """Stops user holders nested in an earlier root holder (one that died)."""
    for entry in os.listdir(HOME):
        if not (entry.startswith("ns-") and entry.endswith(".pid")):
            continue
        if entry.startswith(f"ns-{root_pid}-"):
            continue
        pid = _holder_pid(f"{HOME}/{entry}", "runtime-nsu")
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        os.remove(f"{HOME}/{entry}")


# -- running a command ---------------------------------------------------------


def _staged_script(arg: str, encoded: str | None) -> str:
    """The script's path inside the image, writing an inline script there first."""
    if encoded is None:
        return arg
    os.makedirs(ROOT + XFER, exist_ok=True)
    path = f"{XFER}/cmd-{os.getpid()}-{random.randrange(1 << 32):08x}.sh"
    with open(ROOT + path, "wb") as f:
        f.write(base64.b64decode(encoded))
    return path


def _terminated(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)


def main(argv: list[str]) -> None:
    import fcntl  # Linux only, like everything below; imported here for the tests

    # A timeout kills this process with SIGTERM: clean up on the way out.
    signal.signal(signal.SIGTERM, _terminated)
    user: tuple[int, int] | None = None
    if argv[:1] == ["--user"]:
        uid, gid = (int(part) for part in argv[1].split(":"))
        user = None if uid == 0 else (uid, gid)
        argv = argv[2:]
    script, entered = None, False
    try:
        script = _staged_script(argv[0], argv[1] if argv[0] == "-" else None)
        _enter(user, fcntl)
        entered = True
        env = {"PATH": _PATH, "TERM": "xterm"}
        if user is None:
            install_filter()
            env["TAR_OPTIONS"] = "--no-same-owner"  # only uid 0 exists
        # Python ignores SIGPIPE and SIGXFSZ, and an exec keeps ignored signals.
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        os.execve(shell, [shell, script], env)
    except BaseException as exc:
        # The script never ran, so it never removed itself; it holds the env.
        if script is not None:
            try:
                os.remove(script if entered else ROOT + script)
            except OSError:
                pass
        if isinstance(exc, SystemExit):
            raise
        print(f"prized runtime: {exc!r}", file=sys.stderr)
        sys.exit(125)


def _enter(user: tuple[int, int] | None, fcntl: Any) -> None:
    """Joins the trial's namespaces (starting holders as needed) and chroots."""
    with open(f"{HOME}/ns.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        known = _holder_pid(f"{HOME}/ns.pid", "runtime-ns")
        root_pid = ensure_holder("ns", _ROOT_HOLDER)
        if root_pid != known:
            stop_stale_user_holders(root_pid)
        user_pid = None
        if user is not None:
            user_pid = ensure_holder(
                f"ns-{root_pid}-{user[0]}-{user[1]}",
                user_holder_command(root_pid, *user),
                comm="runtime-nsu",
            )

    # One thread only from here: setns into a user namespace requires it.
    namespaces = [
        (root_pid, "user", os.CLONE_NEWUSER),
        (root_pid, "mnt", os.CLONE_NEWNS),
    ]
    if user_pid is not None:
        namespaces.append((user_pid, "user", os.CLONE_NEWUSER))
    fds = [
        (os.open(f"/proc/{pid}/ns/{ns}", os.O_RDONLY), kind)
        for pid, ns, kind in namespaces
    ]
    for fd, kind in fds:
        os.setns(fd, kind)
        os.close(fd)
    os.chroot(ROOT)
    os.chdir("/")


if __name__ == "__main__":
    main(sys.argv[1:])
