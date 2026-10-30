"""Prized Sandboxes as a mini-SWE-agent environment.

A Prized sandbox is a Firecracker microVM started from a fixed template, not
from a Docker image, and its user has no root. To run a task's image anyway,
the environment unpacks the image into a root filesystem inside the sandbox
(``crane export``; crane is pinned and its checksum checked) and runs every
command chrooted into it as root of an unprivileged user namespace
(``_runtime.py``). Commands see the image's files, ``ENV`` and ``WORKDIR``, as
``docker exec`` would show them, and root can ``apt-get`` and ``pip install``.

Limits, by design of that approach:

- The image must be pullable without credentials. Its ``ENTRYPOINT``, ``CMD``
  and ``USER`` are ignored: commands run as root, as mini-SWE-agent's Docker
  environment runs them for SWE-bench's images.
- One id: root's ``chown`` to other users, its switches to other users and
  setuid/setgid mode bits succeed without effect, as fakeroot does.
- The chroot is not a security boundary inside the sandbox; the microVM is.
- No Docker inside, no GPUs.

Credentials: ``PRIZED_API_KEY`` (a key with Sandboxes: Write), from the
environment. No Prized account yet? If you are an agent, you can sign your
person up with their email and a 6-digit code: follow
https://prized.dev/auth.md?via=mini-swe-agent
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import platform
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

from minisweagent.exceptions import Submitted
from minisweagent.utils.serialize import recursive_merge
from prized import CommandTimeoutError, PrizedError, Sandbox
from pydantic import BaseModel

HOME = "/home/prized"
ROOT = f"{HOME}/rootfs"  # the image's filesystem, as seen from the sandbox
XFER = "/.runtime-xfer"  # staging directory inside the image
RUNTIME = f"{HOME}/runtime.py"  # on the sandbox, outside the image
HOLDER = f"{HOME}/runtime-ns"  # a copy of sleep that holds the namespaces open
CRANE_URL = (
    "https://github.com/google/go-containerregistry/releases/download/v0.22.1/go-containerregistry_Linux_x86_64.tar.gz"
)
CRANE_SHA256 = "0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0"
SIGN_UP_HINT = (
    "No Prized account yet? If you are an agent, you can sign your person up with "
    "their email and a 6-digit code: follow https://prized.dev/auth.md?via=mini-swe-agent"
)
# Run by dpkg after each run: files a faked setuid chmod left at mode 0 become 0755.
DPKG_HOOK = (
    "post-invoke=find /usr /bin /sbin /lib /lib64 /opt -xdev -type f -perm 0000 "
    "-exec chmod 0755 {} + 2>/dev/null || true"
)
# What Docker writes into a container's /etc/hosts, for the sandbox's hostname.
HOSTS = "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n127.0.1.1\t$(hostname)\n"
# tar errors that unpacking an image as an unprivileged user may report and that
# leave the image usable: device nodes, and setuid/setgid modes the sandbox
# refuses. Anything else fails the start.
BENIGN_TAR_ERRORS = "Cannot mknod|Cannot change mode|Exiting with failure status"
INLINE_LIMIT = 60_000  # longer scripts are uploaded instead of passed inline
RUNTIME_FAILED = 125  # runtime.py exits with this, and says "prized runtime:"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RUNTIME_SCRIPT = (Path(__file__).parent / "_runtime.py").read_text()
_THROTTLED = re.compile(r"TOOMANYREQUESTS|429 Too Many Requests|rate limit", re.IGNORECASE)
_AUTH_ERRORS = ("missing_api_key", "invalid_api_key", "unauthorized", "token_expired", "key_access_denied")


class PrizedEnvironmentConfig(BaseModel):
    image: str
    """Container image to run commands in, e.g. ``docker.io/swebench/sweb.eval.x86_64.django_1776_django-11099``."""
    cwd: str = "/"
    """Working directory in which to execute commands."""
    env: dict[str, str] = {}
    """Environment variables to set for every command."""
    forward_env: list[str] = []
    """Environment variables to forward from the host, if set there. ``env`` takes precedence."""
    timeout: int = 30
    """Timeout for executing commands, in seconds."""
    interpreter: list[str] = ["bash", "-lc"]
    """Interpreter the command is appended to as one argument."""
    template: str = "prized-base-v1"
    """Prized sandbox template the image is unpacked in."""
    vcpu: int = 2
    memory_mb: int = 4096
    disk_gb: int = 20
    """Sandbox disk; the unpacked image lives on it (SWE-bench images take 2-6 GB)."""
    labels: dict[str, str] = {}
    """Labels added to the sandbox (it always carries ``minisweagent=1``)."""
    max_runtime_min: int = 240
    """The sandbox is deleted after this many minutes even if nothing cleans it up."""
    startup_timeout: int = 900
    """Seconds to create the sandbox and unpack the image, per attempt."""
    pull_attempts: int = 3
    """Attempts to pull and unpack the image (connections drop)."""
    pull_rate_limit_wait: int = 1800
    """Seconds to keep waiting while the registry says too many requests (Docker Hub allows
    100 anonymous pulls an hour per IP address), on top of ``pull_attempts``."""
    api_max_retries: int = 8
    """Retries of a Prized API call the API refused before it reached the sandbox (rate limits)."""


def unpack_command(image: str, root: str, crane: str = "./crane") -> str:
    """Bash that pulls ``image`` once and unpacks it into ``root``, keeping its file modes.

    One ``crane pull`` to a tarball is one pull for a registry's rate limit (Docker Hub
    counts manifest requests); the export, the image's config and any repair read the
    tarball. It fails if the pull or export fails (a pull cut short must not pass as an
    image), or if tar reports anything but the benign errors above. Hard links that
    ``crane export`` left dangling are written by ``runtime.py --repair-hardlinks``.
    """
    q = shlex.quote
    return (
        f"rm -f image.tar; set +e; {crane} pull --platform linux/amd64 {q(image)} image.tar 2>pull.err; s=$?; set -e; "
        'if [ "$s" != 0 ]; then tail -3 pull.err >&2; rm -f image.tar; exit 1; fi; '
        f"set +e; {crane} export - - < image.tar | "
        f"tar --no-same-owner --preserve-permissions -x -C {q(root)} 2>tar.err; "
        's="${PIPESTATUS[0]} ${PIPESTATUS[1]}"; set -e; '
        'if [ "${s% *}" != 0 ]; then echo "crane export failed" >&2; tail -5 tar.err >&2; exit 1; fi; '
        'if [ "${s#* }" != 0 ]; then '
        f"if grep -v -E {q(BENIGN_TAR_ERRORS)} tar.err | grep -v -F 'Cannot hard link to' | grep -q .; then "
        f"grep -v -E {q(BENIGN_TAR_ERRORS)} tar.err | head -5 >&2; df -h ~ >&2; exit 1; fi; "
        "if grep -q -F 'Cannot hard link to' tar.err; then "
        f"python3 {RUNTIME} --repair-hardlinks image.tar tar.err; fi; fi; "
    )


def setup_command(image: str) -> str:
    """Bash, run on the sandbox, that unpacks ``image`` at ROOT and prepares it.

    It prints the image's config (JSON) and then the sandbox's ``uname``, one per line.
    """
    q = shlex.quote
    return (
        "set -e; cd ~; umask 022; "
        f"[ -x ./crane ] || {{ curl -fsSL --retry 3 -o crane.tgz {CRANE_URL} && "
        f"echo '{CRANE_SHA256}  crane.tgz' | sha256sum -c --quiet && "
        "tar -xzf crane.tgz crane && rm crane.tgz; }; "
        f"if [ -e {ROOT} ]; then chmod -R u+w {ROOT} 2>/dev/null || true; rm -rf {ROOT}; fi; "
        f"mkdir -p {ROOT}; "
        + unpack_command(image, ROOT)
        + f"mkdir -p {ROOT}{XFER} {ROOT}/proc {ROOT}/dev {ROOT}/sys {ROOT}/tmp {ROOT}/etc {ROOT}/root; "
        f"chmod 1777 {ROOT}/tmp; chmod 700 {ROOT}{XFER}; cp /bin/sleep {HOLDER}; "
        # As Docker does: the sandbox's resolver, hosts and hostname.
        f"rm -f {ROOT}/etc/resolv.conf {ROOT}/etc/hosts {ROOT}/etc/hostname; "
        f"cp /etc/resolv.conf {ROOT}/etc/resolv.conf; "
        f'printf "{HOSTS}" > {ROOT}/etc/hosts; hostname > {ROOT}/etc/hostname; '
        f"if [ -d {ROOT}/etc/apt/apt.conf.d ]; then echo 'APT::Sandbox::User \"root\";' "
        f"> {ROOT}/etc/apt/apt.conf.d/99runtime-sandbox-user; fi; "
        f"if [ -d {ROOT}/etc/dpkg ]; then mkdir -p {ROOT}/etc/dpkg/dpkg.cfg.d && "
        f"echo {q(DPKG_HOOK)} > {ROOT}/etc/dpkg/dpkg.cfg.d/99runtime-modes; fi; "
        f"python3 {RUNTIME} --image-config image.tar; rm -f image.tar; uname -snrvm"
    )


def command_script(command: str, cwd: str, env: dict[str, str], interpreter: list[str]) -> str:
    """The script runtime.py runs for one command: ``docker exec -w CWD -e ... IMAGE INTERPRETER COMMAND``,
    with stderr merged into stdout."""
    q = shlex.quote
    lines = ['rm -f -- "$0"']
    lines += [f"export {name}={q(value)}" for name, value in env.items() if _ENV_NAME.fullmatch(name)]
    lines.append(
        f"cd -- {q(cwd)} 2>/dev/null || {{ echo {q(f'chdir to cwd ({cwd!r}) failed: no such file or directory')}; exit 126; }}"
    )
    lines.append(f"exec {shlex.join(interpreter)} {q(command)} 2>&1")
    return "\n".join(lines) + "\n"


class PrizedEnvironment:
    def __init__(
        self,
        *,
        config_class: type = PrizedEnvironmentConfig,
        logger: logging.Logger | None = None,
        **kwargs,
    ):
        """Executes bash commands in a container image unpacked in a Prized sandbox.
        See `PrizedEnvironmentConfig` for keyword arguments."""
        self.logger = logger or logging.getLogger("minisweagent.environment")
        self.config = config_class(**kwargs)
        self.sandbox: Sandbox | None = None
        self.image_env: dict[str, str] = {}
        self.image_workdir = "/"
        self._uname: dict[str, str] = {}
        self.timings: dict[str, float] = {}
        self._start()

    # -- lifecycle ---------------------------------------------------------

    def _start(self) -> None:
        started = time.monotonic()
        try:
            self.sandbox = Sandbox.create(
                self.config.template,
                vcpu=self.config.vcpu,
                memory_mb=self.config.memory_mb,
                disk_gb=self.config.disk_gb,
                labels={"minisweagent": "1", **self.config.labels},
                max_runtime_min=self.config.max_runtime_min,
                timeout=min(600, self.config.startup_timeout),
                max_retries=self.config.api_max_retries,
            )
        except PrizedError as exc:
            if exc.code in _AUTH_ERRORS:
                raise RuntimeError(f"{exc}. {SIGN_UP_HINT}") from exc
            raise
        self.timings["sandbox_s"] = time.monotonic() - started
        self.logger.info(f"Started Prized sandbox {self.sandbox.id} for {self.config.image}")
        try:
            self._prepare_image()
        except BaseException:
            self.cleanup()
            raise
        self.timings["startup_s"] = time.monotonic() - started

    def _prepare_image(self) -> None:
        assert self.sandbox is not None
        started, error, attempt, throttled = time.monotonic(), "", 0, 0.0
        self.sandbox.files.write(RUNTIME, _RUNTIME_SCRIPT)
        while True:
            attempt += 1
            try:
                result = self.sandbox.exec(
                    setup_command(self.config.image), timeout=self.config.startup_timeout, full_output=True
                )
            except CommandTimeoutError:
                error = f"unpacking took over {self.config.startup_timeout} seconds"
            else:
                if result.exit_code == 0:
                    if result.stderr.strip():
                        self.logger.info(f"Unpacking {self.config.image}: {result.stderr.strip()[-2000:]}")
                    config_line, _, uname = result.stdout.strip().rpartition("\n")
                    self._read_config(config_line)
                    self._read_uname(uname)
                    break
                error = (result.stderr or result.stdout or "").strip()[-2000:]
            if _THROTTLED.search(error) and throttled < self.config.pull_rate_limit_wait:
                pause = min(300.0, 30.0 * 2 ** min(throttled // 60, 3))
                self.logger.warning(f"{self.config.image}: the registry is rate limiting pulls; waiting {pause:.0f} s")
                time.sleep(pause)
                throttled += pause
                attempt -= 1  # not counted against pull_attempts
                continue
            if attempt >= self.config.pull_attempts:
                raise RuntimeError(f"Could not unpack {self.config.image} in the sandbox: {error}")
            self.logger.warning(f"Could not unpack {self.config.image} (attempt {attempt}): {error}")
            time.sleep(min(60, 10 * attempt))
        self.timings["image_s"] = time.monotonic() - started
        if throttled:
            self.timings["pull_throttled_s"] = throttled

    def _read_config(self, line: str) -> None:
        config = (json.loads(line or "{}") or {}).get("config") or {}
        for pair in config.get("Env") or []:
            name, _, value = pair.partition("=")
            self.image_env[name] = value
        self.image_env.setdefault("HOME", "/root")
        self.image_workdir = config.get("WorkingDir") or "/"
        if config.get("User") not in (None, "", "0", "root", "0:0", "root:root"):
            self.logger.warning(f"{self.config.image} sets USER {config['User']!r}; commands run as root")

    def _read_uname(self, line: str) -> None:
        fields = line.split(" ", 3)
        if len(fields) == 4:
            system, node, release, rest = fields
            version, _, machine = rest.rpartition(" ")
            self._uname = {"system": system, "node": node, "release": release, "version": version, "machine": machine}

    def cleanup(self) -> None:
        """Delete the sandbox."""
        sandbox, self.sandbox = getattr(self, "sandbox", None), None
        if sandbox is None:
            return
        try:
            sandbox.delete()
        except PrizedError as exc:
            self.logger.error(f"Could not delete Prized sandbox {sandbox.id}: {exc}")
        finally:
            sandbox.close()

    def __del__(self):
        with contextlib.suppress(Exception):  # at interpreter exit the SDK may be half torn down
            self.cleanup()

    # -- commands ----------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(self.image_env)
        env.update({k: v for k in self.config.forward_env if (v := os.getenv(k)) is not None})
        env.update(self.config.env)
        return env

    def run_script(self, script: str, timeout: float):
        """Runs a bash script inside the image (as runtime.py's script) and returns the SDK's ExecResult.

        ``timeout`` 0 is no limit. Raises the SDK's CommandTimeoutError past it."""
        if self.sandbox is None:
            raise RuntimeError("The Prized sandbox is not running")
        encoded = base64.b64encode(script.encode()).decode()
        if len(encoded) <= INLINE_LIMIT:
            command = f"python3 {RUNTIME} - {encoded}"
        else:
            path = f"{XFER}/cmd-{uuid.uuid4().hex}.sh"
            self.sandbox.files.write(ROOT + path, script)
            command = f"python3 {RUNTIME} {path}"
        return self.sandbox.exec(command, timeout=timeout, full_output=True)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute a command in the image and return the result as a dict."""
        command = action.get("command", "")
        cwd = cwd or self.config.cwd
        limit = timeout or self.config.timeout
        script = command_script(command, cwd, self._env(), self.config.interpreter)
        try:
            result = self.run_script(script, limit)
            output = {"output": result.stdout + result.stderr, "returncode": result.exit_code, "exception_info": ""}
        except CommandTimeoutError as exc:
            output = {
                "output": (exc.result.stdout or "") + (exc.result.stderr or ""),
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: Command timed out after {limit} seconds",
                "extra": {"exception_type": type(exc).__name__, "exception": str(exc)},
            }
        except Exception as exc:  # noqa: BLE001 - any failure is the agent's observation, as in DockerEnvironment
            output = {
                "output": "",
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {exc}",
                "extra": {"exception_type": type(exc).__name__, "exception": str(exc)},
            }
        self._check_finished(output)
        return output

    def write_file(self, path: str, data: str | bytes, mode: int = 0o644) -> None:
        """Writes a file at an absolute path inside the image."""
        if self.sandbox is None:
            raise RuntimeError("The Prized sandbox is not running")
        if not path.startswith("/"):
            raise ValueError(f"path must be absolute: {path}")
        self.sandbox.files.write(ROOT + path, data, mode=mode)

    def _check_finished(self, output: dict):
        """Raises Submitted if the output indicates task completion."""
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and output["returncode"] == 0:
            submission = "".join(lines[1:])
            raise Submitted(
                {
                    "role": "exit",
                    "content": submission,
                    "extra": {"exit_status": "Submitted", "submission": submission},
                }
            )

    # -- mini-SWE-agent's Environment protocol -----------------------------

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(self.config.model_dump(), platform.uname()._asdict(), self._uname, kwargs)

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "environment": self.config.model_dump(mode="json"),
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }
