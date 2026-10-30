"""The sandbox-side runtime: its seccomp filter, run through a classic-BPF interpreter, its
namespace holder, and what it does when it can't enter."""

import json
import os
import resource
import shutil
import struct
import subprocess
import sys
import time

import pytest

from minisweagent_prized import _runtime as runtime
from minisweagent_prized import environment
from minisweagent_prized._runtime import build_filter

X86_64, AARCH64, I386 = 0xC000003E, 0xC00000B7, 0x40000003
SUCCEED, ALLOW = 0x00050000, 0x7FFF0000
NOBODY, UNCHANGED = 65534, 0xFFFFFFFF


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


@pytest.mark.parametrize(
    ("nr", "args", "want"),
    [
        (105, [0], ALLOW),  # setuid(0) stays real
        (105, [NOBODY], SUCCEED),  # setuid to another user: stays root
        (106, [1000], SUCCEED),  # setgid
        (117, [UNCHANGED, 1, UNCHANGED], SUCCEED),  # sudo's setresuid(-1, 1, -1)
        (117, [0, 0, 0], ALLOW),
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
    assert _run(54, AARCH64) == SUCCEED  # fchownat on aarch64
    assert _run(92, AARCH64) == ALLOW  # 92 is not chown there
    assert _run(182, I386) == ALLOW


def test_the_environment_uploads_this_runtime():
    assert "def build_filter" in environment._RUNTIME_SCRIPT
    assert (runtime.HOME, runtime.ROOT, runtime.XFER, runtime.HOLDER) == (
        environment.HOME,
        environment.ROOT,
        environment.XFER,
        environment.HOLDER,
    )


def test_the_runtime_needs_only_the_standard_library():
    # It runs with the sandbox's python3, outside any virtualenv.
    import ast

    tree = ast.parse(environment._RUNTIME_SCRIPT)
    modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert modules <= set(sys.stdlib_module_names)


def test_an_inline_script_is_staged_inside_the_image(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", str(tmp_path))
    path = runtime._staged_script("-", "ZWNobyBoaQ==")  # "echo hi"
    assert path.startswith(f"{runtime.XFER}/cmd-")
    assert (tmp_path / path.lstrip("/")).read_text() == "echo hi"
    assert runtime._staged_script("/.runtime-xfer/big.sh", None) == "/.runtime-xfer/big.sh"


linux_only = pytest.mark.skipif(sys.platform != "linux", reason="the holder runs on the Linux sandbox")


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    sleep = shutil.which("sleep")
    if sleep is None:
        pytest.skip("no sleep binary")
    holder = tmp_path / "runtime-ns"
    shutil.copy(sleep, holder)
    monkeypatch.setattr(runtime, "HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "HOLDER", str(holder))
    yield tmp_path
    subprocess.run(["pkill", "-f", str(holder)], check=False)


def _fake_holder(holder) -> list[str]:
    # Reports its pid in $1, then becomes the holder (as the real command does).
    return ["/bin/sh", "-c", f'echo $$ > "$1.tmp" && mv "$1.tmp" "$1"; exec {holder} infinity', "sh"]


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
def test_a_reused_pid_with_another_start_time_is_not_the_holder(sandbox_home):
    pid = runtime.ensure_holder("ns", _fake_holder(runtime.HOLDER))
    (sandbox_home / "ns.pid").write_text(f"{pid} 1")
    assert runtime._holder_pid(str(sandbox_home / "ns.pid"), "runtime-ns") is None


@linux_only
def test_a_holder_that_never_starts_is_reported_and_killed(sandbox_home):
    marker = sandbox_home / "still-running"
    command = ["/bin/sh", "-c", f"echo boom >&2; sleep 2; touch {marker}", "sh"]
    with pytest.raises(RuntimeError, match="could not start the namespace holder ns: boom"):
        runtime.ensure_holder("ns", command, timeout=0.5)
    time.sleep(2.5)
    assert not marker.exists()


@linux_only
def test_a_command_that_cannot_enter_is_removed_and_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "ROOT", str(tmp_path))

    def cannot_enter(fcntl):
        raise RuntimeError("could not start the namespace holder ns: boom")

    monkeypatch.setattr(runtime, "_enter", cannot_enter)
    with pytest.raises(SystemExit) as exited:
        runtime.main(["-", "ZWNobyBzZWNyZXQ="])  # "echo secret"
    assert exited.value.code == environment.RUNTIME_FAILED
    assert "prized runtime:" in capsys.readouterr().err
    assert list((tmp_path / runtime.XFER.lstrip("/")).iterdir()) == []


def test_open_files_limit_is_raised_to_the_hard_limit():
    # In a child process, as runtime.py runs: lower the soft limit, then let the runtime raise it.
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    low = 256 if hard == resource.RLIM_INFINITY else min(256, hard)
    code = (
        "import importlib.util, resource, sys\n"
        f"resource.setrlimit(resource.RLIMIT_NOFILE, ({low}, {hard}))\n"
        f"spec = importlib.util.spec_from_file_location('rt', {runtime.__file__!r})\n"
        "rt = importlib.util.module_from_spec(spec); spec.loader.exec_module(rt)\n"
        "rt._raise_open_files_limit()\n"
        "s, h = resource.getrlimit(resource.RLIMIT_NOFILE); print(s == h)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True"


def test_failed_hardlinks_are_read_in_both_quote_styles():
    errors = (
        "tar: usr/sbin/unix_chkpwd: Cannot change mode to rwxr-sr-x: Operation not permitted\n"
        "tar: opt/pkgs/x/a.py: Cannot hard link to ‘opt/envs/t/x/a.py’: No such file or directory\n"
        "tar: opt/pkgs/x/b.py: Cannot hard link to 'opt/envs/t/x/b.py': No such file or directory\n"
    )
    assert runtime.failed_hardlinks(errors) == {
        "opt/pkgs/x/a.py": "opt/envs/t/x/a.py",
        "opt/pkgs/x/b.py": "opt/envs/t/x/b.py",
    }


def _layer(path, entries):
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as tar:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
            elif kind == "link":
                info.type, info.linkname, info.mode = tarfile.LNKTYPE, data, 0o644
                tar.addfile(info)
            elif kind == "whiteout":
                tar.addfile(info, io.BytesIO(b""))


def _saved_image(tmp_path, layers):
    """A ``crane pull`` (docker save) tarball with the given layers, like the sandbox pulls."""
    import tarfile

    names = []
    for i, entries in enumerate(layers):
        _layer(tmp_path / f"l{i}.tar.gz", entries)
        names.append(f"l{i}.tar.gz")
    (tmp_path / "cfg").write_text(json.dumps({"config": {"Env": ["A=1"], "WorkingDir": "/testbed/"}}))
    (tmp_path / "manifest.json").write_text(json.dumps([{"Config": "cfg", "Layers": names}]))
    image = tmp_path / "image.tar"
    with tarfile.open(image, "w") as tar:
        for name in ["manifest.json", "cfg", *names]:
            tar.add(tmp_path / name, arcname=name)
    return str(image)


def test_image_config_is_read_from_the_tarball(tmp_path):
    image = _saved_image(tmp_path, [[("a", "file", b"x")]])
    assert json.loads(runtime.image_config(image)) == {"config": {"Env": ["A=1"], "WorkingDir": "/testbed/"}}


@linux_only
def test_dangling_hardlinks_are_written_from_their_layer(tmp_path):
    # Layer 0 installs a package into the env and links it from the package cache; layer 1
    # deletes it from the env (pip replaced it). Docker keeps the cache's copy; crane export
    # can't, so tar reports the link and the repair writes it from layer 0.
    image = _saved_image(
        tmp_path,
        [
            [
                ("opt/envs/t/x/a.py", "file", b"old a"),
                ("opt/pkgs/x/a.py", "link", "opt/envs/t/x/a.py"),
                ("opt/envs/t/x/b.py", "file", b"b v1"),
                ("opt/pkgs/x/b.py", "link", "opt/envs/t/x/b.py"),
            ],
            [("opt/envs/t/x/.wh.a.py", "whiteout", b""), ("opt/envs/t/x/b.py", "file", b"b v2")],
            [("./opt/envs/t/x/.wh.b.py", "whiteout", b"")],
        ],
    )
    root = tmp_path / "rootfs"
    root.mkdir()
    errors = (
        "tar: opt/pkgs/x/a.py: Cannot hard link to ‘opt/envs/t/x/a.py’: No such file or directory\n"
        "tar: opt/pkgs/x/b.py: Cannot hard link to ‘opt/envs/t/x/b.py’: No such file or directory\n"
    )
    assert runtime.repair_hardlinks(image, errors, root=str(root)) == 2
    assert (root / "opt/pkgs/x/a.py").read_bytes() == b"old a"
    assert (root / "opt/pkgs/x/b.py").read_bytes() == b"b v1"  # what the link pointed to in its layer
    assert not (root / "opt/envs").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("hardlinks-")] == []
    with pytest.raises(RuntimeError, match="no file in any layer"):
        runtime.repair_hardlinks(
            image, "tar: q: Cannot hard link to ‘missing’: No such file or directory\n", root=str(root)
        )
