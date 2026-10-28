"""Build the wheel and sdist and check them as PyPI and a customer get them.

    uv run --group release python tests/package_check.py [--dist DIR]

Builds with `uv build` into DIR (default: a temporary directory), then checks:
- the wheel holds only the `openai_agents_prized` package and its dist-info,
  with `py.typed` and the Apache-2.0 LICENSE under dist-info/licenses;
- METADATA names the distribution `openai-agents-prized`, `License-Expression:
  Apache-2.0`, `License-File: LICENSE`, `Requires-Python: >=3.10,<4.0`, the
  `openai-agents` and `httpx` requirements, and the version in
  openai_agents_prized/_version.py;
- the sdist carries the LICENSE and leaves the tests out;
- `twine check --strict` passes on both;
- the wheel installs into a fresh virtualenv, where tests/wheel_check.py runs
  with `python -I` (version, `py.typed`, the SDK finds the options and state
  classes, no abstract methods, and a scripted agent runs through `Runner`
  with the installed wheel against the tests' fake API).

CI runs it on every change, and a release runs it with --dist dist and
publishes what it built.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = "openai-agents-prized"
PACKAGE = "openai_agents_prized"
REQUIRES_PYTHON = ">=3.10,<4.0"


def run(*cmd: str, cwd: Path = ROOT) -> None:
    """Run a command, failing the check if it fails."""
    subprocess.run(cmd, cwd=cwd, check=True)  # our own fixed commands


def fail(message: str) -> None:
    """Stop with the reason."""
    msg = f"package check: {message}"
    raise SystemExit(msg)


def version() -> str:
    """The version in openai_agents_prized/_version.py."""
    text = (ROOT / PACKAGE / "_version.py").read_text()
    found = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if not found:
        fail(f"no __version__ in {PACKAGE}/_version.py")
        raise AssertionError  # unreachable; fail() raised
    return found.group(1)


def check_wheel(wheel: Path, want_version: str) -> None:  # noqa: C901  # one check per line
    """The wheel's files, LICENSE, and METADATA."""
    dist_info = f"{PACKAGE}-{want_version}.dist-info/"
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        strays = [n for n in names if not n.startswith((f"{PACKAGE}/", dist_info))]
        if strays:
            fail(f"the wheel carries files it should not: {', '.join(strays)}")
        for required in (
            f"{PACKAGE}/__init__.py",
            f"{PACKAGE}/py.typed",
            f"{dist_info}licenses/LICENSE",
        ):
            if required not in names:
                fail(f"the wheel is missing {required}")
        license_text = zf.read(f"{dist_info}licenses/LICENSE").decode()
        metadata = Parser().parsestr(zf.read(f"{dist_info}METADATA").decode())
    if license_text != (ROOT / "LICENSE").read_text():
        fail("the wheel's LICENSE differs from the package's LICENSE")
    if "Apache License" not in license_text or "Version 2.0, January 2004" not in license_text:
        fail("LICENSE is not the Apache License 2.0 text that License-Expression names")
    expected = {
        "Name": DIST,
        "Version": want_version,
        "License-Expression": "Apache-2.0",
        "License-File": "LICENSE",
    }
    for field, value in expected.items():
        if metadata.get(field) != value:
            fail(f"METADATA {field} is {metadata.get(field)!r}, expected {value!r}")
    # The build may reorder the specifiers.
    requires_python = set((metadata.get("Requires-Python") or "").split(","))
    if requires_python != set(REQUIRES_PYTHON.split(",")):
        fail(f"METADATA Requires-Python is {requires_python}, expected {REQUIRES_PYTHON!r}")
    requires = metadata.get_all("Requires-Dist") or []
    for dependency in ("openai-agents", "httpx"):
        if not any(r.startswith(dependency) for r in requires):
            fail(f"METADATA does not require {dependency}: {requires}")


def check_sdist(sdist: Path, want_version: str) -> None:
    """The sdist carries the LICENSE and not the tests."""
    prefix = f"{PACKAGE}-{want_version}/"
    with tarfile.open(sdist) as tf:
        names = tf.getnames()
    if f"{prefix}LICENSE" not in names:
        fail("the sdist is missing LICENSE")
    tests = [n for n in names if n.startswith(f"{prefix}tests")]
    if tests:
        fail(f"the sdist carries the tests: {', '.join(tests[:3])}")


def main() -> int:
    """Build, check, and test-install; return 0 when everything holds."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dist", type=Path, help="build into this directory and keep it")
    args = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        fail("uv is not on the PATH")
        raise AssertionError  # unreachable
    with tempfile.TemporaryDirectory() as tmp:
        dist = args.dist.resolve() if args.dist else Path(tmp) / "dist"
        run(uv, "build", "--out-dir", str(dist), ".")
        want = version()
        wheels = sorted(dist.glob(f"{PACKAGE}-{want}-*.whl"))
        sdists = sorted(dist.glob(f"{PACKAGE}-{want}.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            fail(f"expected one wheel and one sdist for {want} in {dist}, found {wheels + sdists}")
        check_wheel(wheels[0], want)
        check_sdist(sdists[0], want)
        run(sys.executable, "-m", "twine", "check", "--strict", str(wheels[0]), str(sdists[0]))
        venv = Path(tmp) / "consumer"
        run(uv, "venv", "--quiet", "--python", sys.executable, str(venv))
        py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        run(uv, "pip", "install", "--quiet", "--python", str(py), str(wheels[0]), cwd=Path(tmp))
        run(str(py), "-I", str(ROOT / "tests" / "wheel_check.py"), cwd=Path(tmp))
    sys.stdout.write(f"package check: {DIST} {want} ok\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
