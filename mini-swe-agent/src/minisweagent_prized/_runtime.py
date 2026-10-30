"""Run one command inside a container image's filesystem, in a Prized sandbox.

minisweagent_prized uploads this file to the sandbox, where it runs with the
sandbox's own python3 (standard library only)::

    python3 runtime.py - BASE64_SCRIPT     # a script passed inline
    python3 runtime.py /SCRIPT/IN/IMAGE    # a script already written into the image
    python3 runtime.py --image-config IMAGE_TAR               # the image's config, one line of JSON
    python3 runtime.py --repair-hardlinks IMAGE_TAR TAR_ERRORS  # see repair_hardlinks

The image is unpacked at ``ROOT``. A Prized sandbox has no Docker and its user
has no root, so a command runs chrooted into ``ROOT`` in an unprivileged user
namespace that maps the sandbox's user to uid 0, as ``docker exec`` runs it as
root in a container.

Every command joins the same namespaces, kept open by a holder process (a copy
of ``sleep`` named ``runtime-ns``), as the processes of one container share
theirs: the sandbox mounts ``/proc`` with ``hidepid=invisible``, so processes in
sibling namespaces can't see each other, and a ``pkill`` in a later command
would miss a server an earlier one started.

Only one id exists in the namespace, so commands run under a seccomp filter
that makes ``chown`` to any user, ``setgroups``, switches to other users and
setuid/setgid mode bits succeed without effect, as fakeroot does; without it
``apt-get``, ``dpkg`` and ``tar`` fail. Calls that stay on root (uid 0 or -1)
run for real. A faked call also reports success where the real one would have
failed for another reason (a missing file, say).

Commands start the way ``docker exec`` starts them as root: ``HOME=/root``,
umask 022, and the open-files limit raised to its hard limit.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import random
import re
import resource
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any

HOME = "/home/prized"
ROOT = f"{HOME}/rootfs"  # the image's filesystem, as seen from the sandbox
XFER = "/.runtime-xfer"  # staging directory inside the image
HOLDER = f"{HOME}/runtime-ns"  # a copy of sleep: holds the namespaces open
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Starts the namespace holder: $0 is ROOT, $1 the file to report its pid in.
_HOLDER_COMMAND = [
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

# -- the seccomp filter --------------------------------------------------------

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
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(prog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not install the seccomp filter")


# -- the namespace holder ------------------------------------------------------


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


def ensure_holder(name: str, command: list[str], comm: str = "runtime-ns", timeout: float = 30) -> int:
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


# -- hard links that ``crane export`` leaves dangling ---------------------------

# GNU tar quotes with ‘’ in a UTF-8 locale and `' in the C locale.
_HARDLINK_ERROR = re.compile(r"^tar: (.+): Cannot hard link to [‘`'](.+)[’']: No such file or directory$")


def failed_hardlinks(errors: str) -> dict[str, str]:
    """{link: target} for each hard link tar could not make, as tar named them."""
    found = {}
    for line in errors.splitlines():
        match = _HARDLINK_ERROR.match(line.strip())
        if match:
            found[match.group(1)] = match.group(2)
    return found


def _member_path(name: str) -> str:
    return os.path.normpath(name.lstrip("/")).removeprefix("./")


def _saved_manifest(image: tarfile.TarFile) -> dict:
    """The entry of a ``crane pull`` (``docker save``) tarball's manifest.json."""
    source = image.extractfile("manifest.json")
    assert source is not None
    return json.load(source)[0]


def image_config(image_tar: str) -> str:
    """The image's config (``Env``, ``WorkingDir``...) from a ``crane pull`` tarball, as one line of JSON."""
    with tarfile.open(image_tar) as image:
        source = image.extractfile(_saved_manifest(image)["Config"])
        assert source is not None
        return json.dumps(json.load(source), separators=(",", ":"))


def repair_hardlinks(image_tar: str, errors: str, root: str = ROOT) -> int:
    """Writes the files of hard links that ``crane export`` left dangling.

    ``crane export`` flattens the image's layers into one tar stream. When a
    file and a hard link to it are in one layer and a later layer deletes the
    file, the flattened stream keeps the link but not the file it points to,
    so tar can't make it (conda's package cache in some SWE-bench images:
    ``pip`` replaced the package that ``pkgs/`` links into). Docker applies
    layers one by one, so there the link keeps the file's contents. This reads
    the layers of the pulled tarball in order and writes each such link from
    the file it pointed to in its own layer, as Docker sees it. Returns how
    many it wrote.
    """
    pending = failed_hardlinks(errors)
    if not pending:
        return 0
    targets = set(pending.values())
    work = tempfile.mkdtemp(prefix="hardlinks-", dir=os.path.dirname(root))
    stash: dict[str, str] = {}  # target -> its latest contents so far
    found: dict[str, tuple[str, int]] = {}  # link -> (contents, mode) as of its last layer
    try:
        with tarfile.open(image_tar) as image:
            for n, name in enumerate(_saved_manifest(image)["Layers"]):
                blob = image.extractfile(name)
                assert blob is not None
                with tarfile.open(fileobj=blob, mode="r|*") as layer:
                    for member in layer:
                        path = _member_path(member.name)
                        if member.isfile() and path in targets:
                            source = layer.extractfile(member)
                            assert source is not None
                            copy = os.path.join(work, f"{n}-{len(stash)}")
                            with open(copy, "wb") as out:
                                shutil.copyfileobj(source, out)
                            stash[path] = copy
                        elif member.islnk() and path in pending:
                            target = _member_path(member.linkname)
                            if target in stash:
                                found[path] = (stash[target], member.mode)
        missing = sorted(set(pending) - set(found))
        if missing:
            raise RuntimeError(f"{len(missing)} hard links have no file in any layer, e.g. {missing[0]}")
        for link, (contents, mode) in found.items():
            path = os.path.join(root, link)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copyfile(contents, path)
            os.chmod(path, mode & 0o1777)  # no setuid/setgid: the sandbox refuses them
        return len(found)
    finally:
        shutil.rmtree(work, ignore_errors=True)


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


def _raise_open_files_limit() -> None:
    """Soft limit up to the hard one, as Docker's containers get it."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard == resource.RLIM_INFINITY or soft < hard:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        except (OSError, ValueError):
            pass


def _terminated(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)


def main(argv: list[str]) -> None:
    import fcntl  # Linux only, like everything below; imported here for the tests

    # A timeout kills this process with SIGTERM: clean up on the way out.
    signal.signal(signal.SIGTERM, _terminated)
    script, entered = None, False
    try:
        script = _staged_script(argv[0], argv[1] if argv[0] == "-" else None)
        _enter(fcntl)
        entered = True
        install_filter()
        _raise_open_files_limit()
        os.umask(0o022)
        env = {"PATH": _PATH, "HOME": "/root", "TAR_OPTIONS": "--no-same-owner"}
        # Python ignores SIGPIPE and SIGXFSZ, and an exec keeps ignored signals.
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        os.execve(shell, [shell, script], env)
    except BaseException as exc:
        # The script never ran, so it never removed itself.
        if script is not None:
            try:
                os.remove(script if entered else ROOT + script)
            except OSError:
                pass
        if isinstance(exc, SystemExit):
            raise
        print(f"prized runtime: {exc!r}", file=sys.stderr)
        sys.exit(125)


def _enter(fcntl: Any) -> None:
    """Joins the namespaces (starting the holder if needed) and chroots."""
    with open(f"{HOME}/ns.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        root_pid = ensure_holder("ns", _HOLDER_COMMAND)
    # One thread only from here: setns into a user namespace requires it.
    fds = [
        (os.open(f"/proc/{root_pid}/ns/{ns}", os.O_RDONLY), kind)
        for ns, kind in (("user", os.CLONE_NEWUSER), ("mnt", os.CLONE_NEWNS))
    ]
    for fd, kind in fds:
        os.setns(fd, kind)
        os.close(fd)
    os.chroot(ROOT)
    os.chdir("/")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--image-config"]:
        print(image_config(sys.argv[2]))
    elif sys.argv[1:2] == ["--repair-hardlinks"]:
        with open(sys.argv[3], errors="replace") as f:
            print(f"repaired {repair_hardlinks(sys.argv[2], f.read())} hard links", file=sys.stderr)
    else:
        main(sys.argv[1:])
