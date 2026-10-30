"""A Prized sandbox presented through agent-env's ``VmSandbox`` contract.

Each sandbox is one Prized microVM. Commands run as the sandbox user ``prized`` through the Prized API, each one
detached and followed to its end, so a long ``docker compose up`` or image build is never cut off by a request
timeout. agent-env prefixes the commands that need root with ``sudo``. Prized's command runner has no sudo (it runs with
NoNewPrivileges and a read-only system), so on a template with Docker those commands run through a privileged helper
container in the VM's own namespaces (``RootMode``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import shlex
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from enum import StrEnum
from typing import IO, TYPE_CHECKING, Any

from agent_env.providers.sandbox_providers.sandbox import NetworkPolicy, VmSandbox
from agent_env.providers.sandbox_providers.sandbox_provider import SANDBOX_MODE_VM
from prized import AsyncSandbox, PrizedError

if TYPE_CHECKING:
    from agent_env.store.object_store import ObjectStore

logger = logging.getLogger(__name__)

SANDBOX_TYPE = "prized"
# What exec returns when a command's outcome is unknown, its answer lost on the way; exec_script retries only this.
UNKNOWN_EXIT_CODE = -1
# SDK error codes after which the command may or may not have run: retried by agent-env when the script is idempotent.
LOST_CODES = frozenset(
    {
        "network_error",
        "timeout",
        "bad_response",
        "rate_limited",
        "sandbox_unreachable",
        "sandbox_not_ready",
        "resolve_failed",
        "output_read_failed",
    }
)
# SDK error codes that mean the sandbox is gone.
GONE_CODES = frozenset({"not_found", "sandbox_deleted"})
# Codes of a sandbox that can't take commands, which no retry fixes.
_SETTLED_CODES = GONE_CODES | {"sandbox_not_running"}
# Codes of a request that got no readable answer.
_NO_ANSWER_CODES = frozenset({"network_error", "timeout", "bad_response"})
# States in which a sandbox no longer exists for agent-env.
GONE_STATES = frozenset({"deleting", "deleted"})

# Every command runs under umask 022, as on a machine of its own. Prized's command runner keeps 077, which would leave
# the files agent-env writes on the host, and copies into containers, unreadable to a container's non-root user.
UMASK_PREFIX = ("bash", "-c", 'umask 022 && exec "$@"', "agentenv")

# The sandbox user's home: the only place Prized's files API writes, so uploads are staged there.
HOME = "/home/prized"
# Prized's files API takes up to 100 MiB a request; bigger files go in parts of this size, joined in the sandbox.
UPLOAD_PART_BYTES = 64 << 20

# Docker with Compose v2, which agent-env's gateway deploys need. The template's dockerd starts at boot, and the first
# command can be ahead of it, so it is polled this many times, a second apart.
DOCKER_READY_ATTEMPTS = 90
DOCKER_READY = (
    "command -v docker >/dev/null 2>&1 || "
    '{ echo "this sandbox\'s template has no Docker; use a template with Docker, such as prized-docker-v1" >&2; '
    "exit 3; }\n"
    f"for _ in $(seq {DOCKER_READY_ATTEMPTS}); do\n"
    "  docker info >/dev/null 2>&1 && exec docker compose version\n"
    "  sleep 1\n"
    "done\n"
    'echo "the Docker daemon did not become ready" >&2\n'
    "exit 1\n"
)

# How root is had, probed once per handle: through the Docker daemon (root in this sandbox's own VM) when the
# template has one, else not at all.
ROOT_PROBE = "if docker info >/dev/null 2>&1; then echo docker; else echo none; fi"
# The container that runs root commands on a template whose user has the Docker socket: privileged and in the VM's PID
# and network namespaces, so each command enters PID 1's namespaces (nsenter) and runs as root on the VM itself, with
# its live mounts. Pinned by digest, since it gets root; Debian's util-linux, which has nsenter, is essential.
ROOT_HELPER = "agentenv-prized-root"
ROOT_HELPER_IMAGE = (
    "public.ecr.aws/docker/library/debian:bookworm-slim"
    "@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3"
)
ROOT_ENTER = ("nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--")
# What docker exec says when the helper container is gone or stopped; the helper is then started again, once.
_HELPER_GONE = ("No such container", "is not running", "is restarting")


def start_root_helper_script(image: str) -> str:
    """A script that starts the root helper container unless it is running: the image pulled with retries, a stopped
    container of that name removed, then started and waited for. Two handles starting it at once end with one
    container: the second's run fails on the name and waits for the first's."""
    name = ROOT_HELPER
    quoted = shlex.quote(image)
    return (
        "running() { [ \"$(docker inspect -f '{{.State.Running}}' " + name + ' 2>/dev/null)" = true ]; }\n'
        "running && exit 0\n"
        "for attempt in 1 2 3; do docker pull -q "
        + quoted
        + " >/dev/null 2>&1 && break; sleep $((attempt * 2)); done\n"
        "if docker inspect "
        + name
        + " >/dev/null 2>&1 && ! running; then docker rm -f "
        + name
        + " >/dev/null 2>&1; fi\n"
        "err=$(mktemp); trap 'rm -f \"$err\"' EXIT\n"
        "docker run -d --name "
        + name
        + " --privileged --pid=host --net=host --ipc=host --restart=unless-stopped "
        + quoted
        + ' sleep infinity >/dev/null 2>"$err" || true\n'
        "for _ in $(seq 20); do running && exit 0; sleep 1; done\n"
        'cat "$err" >&2\n'
        'echo "the root helper container did not start" >&2\n'
        "exit 1\n"
    )


class RootMode(StrEnum):
    """How a ``sudo`` command runs in this sandbox."""

    DOCKER = "docker"  # as root on the VM, through the root helper container over the Docker socket
    NONE = "none"  # as the sandbox user: root-owned paths are refused


def answer_lost(exc: PrizedError) -> bool:
    """Whether ``exc`` leaves a command's outcome unknown, the answer lost on the way, so an idempotent script may be
    run again: no readable answer, a rate limit or any 5xx, as the SDK's own polling rides out. A sandbox that is gone
    or can't run commands is not."""
    if exc.code in _SETTLED_CODES:
        return False
    if exc.code in LOST_CODES:
        return True
    if exc.status is None:
        return exc.code in _NO_ANSWER_CODES
    return exc.status >= 500 or exc.status == 429


class _BytesReader:
    """The ``StreamReader`` subset ``Sandbox.exec_with_output`` reads."""

    def __init__(self, value: bytes):
        self._value = value

    async def read(self) -> bytes:
        return self._value


class CompletedProcess:
    """A finished command in agent-env's process protocol."""

    def __init__(self, stdout: bytes, stderr: bytes, exit_code: int):
        self.stdout = _BytesReader(stdout)
        self.stderr = _BytesReader(stderr)
        self.exit_code = exit_code
        self.stderr_text = stderr.decode(errors="replace")

    async def wait(self) -> int:
        return self.exit_code


def tunnel_urls_for(document: Mapping[str, Any], ports: list[int]) -> dict[int, str]:
    """``{port: url}`` for ``ports``: empty when none are asked for. Prized gives no port URLs that agent-env can use
    yet: where the API names them (``previewUrlTemplate``) they answer only requests carrying a Prized API key, which
    agent-env's gateways and agents don't send, and putting a key in every workload is no answer. So exposed ports are
    refused, with the reason, until Prized serves public port URLs."""
    if not ports:
        return {}
    if document.get("previewUrlTemplate"):
        reason = (
            "Prized's sandbox port URLs answer only requests that carry a Prized API key, which agent-env's "
            "gateways and agents don't send"
        )
    else:
        reason = "this Prized workspace has no port URLs for sandboxes"
    raise ValueError(f"{reason}, so exposed ports can't be reached yet; create the sandbox without exposed_ports")


class PrizedSandbox(VmSandbox):
    """A Prized microVM. The SDK handle behind it is kept per event loop, since an HTTP client belongs to the loop it
    was made in and the agent-env CLI runs several loops in one process; a loop without one connects by id."""

    type = SANDBOX_TYPE

    def __init__(
        self,
        sandbox_id: str,
        *,
        connect: Callable[[str], Awaitable[AsyncSandbox]],
        tunnel_urls: Mapping[int, str],
        network_policy: NetworkPolicy | None,
        sdk: AsyncSandbox | None = None,
        root_helper_image: str = ROOT_HELPER_IMAGE,
    ):
        self.sandbox_id = sandbox_id
        self.mode = SANDBOX_MODE_VM
        self.vnc_url = None
        self.tunnel_urls = dict(tunnel_urls)
        self.network_policy = network_policy
        self._connect = connect
        self._root_helper_image = root_helper_image
        self._sdks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncSandbox] = weakref.WeakKeyDictionary()
        if sdk is not None:
            self._sdks[asyncio.get_running_loop()] = sdk
        self._root_mode: RootMode | None = None
        self._root_lock: asyncio.Lock | None = None

    async def _sdk(self) -> AsyncSandbox:
        loop = asyncio.get_running_loop()
        sdk = self._sdks.get(loop)
        if sdk is None:
            sdk = self._sdks[loop] = await self._connect(self.sandbox_id)
        return sdk

    async def close(self) -> None:
        """Release this event loop's SDK handle; the sandbox keeps running."""
        sdk = self._sdks.pop(asyncio.get_running_loop(), None)
        if sdk is not None:
            await sdk.close()

    # --- lifecycle -----------------------------------------------------------------------

    async def terminate(self) -> None:
        """Delete the sandbox and its disk; one already gone is not an error."""
        try:
            sdk = await self._sdk()
            await sdk.delete()
        except PrizedError as exc:
            if exc.code not in GONE_CODES and exc.status != 404:
                raise
            logger.info("Prized sandbox %s was already gone at terminate", self.sandbox_id)
        finally:
            await self.close()

    # --- commands --------------------------------------------------------------------------

    async def exec(self, *command: str) -> CompletedProcess:
        """Run argv to its end, detached in the sandbox and followed, with no input, no time limit and umask 022.
        A ``sudo`` prefix runs the rest as root (``RootMode``). An answer lost on the way is exit -1, which
        ``exec_script`` retries; a sandbox that is gone raises."""
        argv = list(command)
        as_root = argv[:1] == ["sudo"]
        if as_root:
            argv = await self._as_root(argv[1:])
        if not argv:
            raise ValueError("exec needs a command")
        process = await self._run(argv)
        if as_root and self._root_mode is RootMode.DOCKER and _helper_gone(process):
            # The helper stopped (a Docker restart, say): start it again, and run the command once more, which never
            # ran, since docker exec found no container to run it in.
            logger.warning("The root helper in Prized sandbox %s was gone; starting it again", self.sandbox_id)
            await self._start_root_helper()
            process = await self._run(argv)
        return process

    async def _run(self, argv: list[str]) -> CompletedProcess:
        sdk = await self._sdk()
        try:
            result = await sdk.exec([*UMASK_PREFIX, *argv], timeout=0)
        except PrizedError as exc:
            if answer_lost(exc):
                return CompletedProcess(b"", f"Prized {exc.code}: {exc}".encode(), UNKNOWN_EXIT_CODE)
            raise
        if result.truncated:
            # Partial output never reads as the whole.
            raise RuntimeError(
                f"A command's output in Prized sandbox {self.sandbox_id} passed what an exec returns "
                f"({result.truncated_reason}); write it to a file and read that instead"
            )
        return CompletedProcess(
            result.stdout.encode("utf-8", "surrogateescape"),
            result.stderr.encode("utf-8", "surrogateescape"),
            result.exit_code,
        )

    async def exec_with_output(self, *args: str) -> tuple[int, str, str]:
        """An exec whose answer was lost (exit -1) returns no stdout, so a caller that skips the exit code can't take
        partial output for the whole."""
        process = await self.exec(*args)
        stdout, stderr = await asyncio.gather(process.stdout.read(), process.stderr.read())
        exit_code = await process.wait()
        kept = b"" if exit_code == UNKNOWN_EXIT_CODE else stdout
        return exit_code, kept.decode(errors="replace"), stderr.decode(errors="replace")

    async def root_mode(self) -> RootMode:
        """How ``sudo`` commands run here, probed on first use; in Docker mode the root helper is started then."""
        if self._root_mode is not None:
            return self._root_mode
        if self._root_lock is None:
            self._root_lock = asyncio.Lock()
        async with self._root_lock:
            if self._root_mode is None:
                # The probe only reads, so an answer lost on the way is asked again.
                for _ in range(3):
                    code, out, err = await self.exec_with_output("bash", "-c", ROOT_PROBE)
                    if code != UNKNOWN_EXIT_CODE:
                        break
                if code != 0:
                    raise RuntimeError(
                        f"Could not tell how to run root commands in Prized sandbox "
                        f"{self.sandbox_id} (exit {code}): {err.strip()[-500:]}"
                    )
                mode = RootMode(out.strip())
                if mode is RootMode.DOCKER:
                    await self._start_root_helper()
                else:
                    logger.warning(
                        "Prized sandbox %s has no Docker, so commands agent-env runs as root run as the sandbox "
                        "user, and root-owned paths are refused",
                        self.sandbox_id,
                    )
                self._root_mode = mode
        return self._root_mode

    async def _start_root_helper(self) -> None:
        script = start_root_helper_script(self._root_helper_image)
        code, _, err = await self.exec_with_output("bash", "-c", script)
        if code != 0:
            raise RuntimeError(
                f"Could not start the root helper container in Prized sandbox {self.sandbox_id} "
                f"(exit {code}): {err.strip()[-500:]}"
            )
        logger.info("Root commands in Prized sandbox %s run through the %s container", self.sandbox_id, ROOT_HELPER)

    async def _as_root(self, argv: list[str]) -> list[str]:
        """``argv`` as this sandbox runs it as root."""
        if not argv:
            raise ValueError("sudo needs a command")
        if argv[0].startswith("-"):
            raise ValueError(f"sudo options are not supported in Prized sandbox {self.sandbox_id}: {argv[0]}")
        if await self.root_mode() is RootMode.DOCKER:
            return ["docker", "exec", ROOT_HELPER, *ROOT_ENTER, *argv]
        return argv

    # --- Docker readiness ------------------------------------------------------------------

    async def wait_for_vm(self) -> None:
        """Wait for the template's Docker daemon, and require Compose v2, which ``deploy_env`` uses."""
        code, version, err = await self.exec_with_output("bash", "-c", DOCKER_READY)
        if code != 0:
            raise RuntimeError(
                f"Docker with Compose v2 is not ready in Prized sandbox {self.sandbox_id}: "
                f"{(err or version).strip()[-500:]}"
            )
        logger.info("Docker Compose ready in Prized sandbox %s: %s", self.sandbox_id, version.strip())

    async def setup_vm_for_gateway(self, exposed_ports: list[int] | None = None) -> None:
        """Wait for Docker, then settle how root commands run. Ports are reached through Prized's port URLs, so no
        firewall rule is opened."""
        await self.wait_for_vm()
        await self.root_mode()

    # --- files -------------------------------------------------------------------------------

    async def _write_bytes_to_vm_path(self, data: bytes, vm_path: str) -> None:
        """Bytes one exec carries go as base64 in it; more go through Prized's files API."""
        if len(base64.b64encode(data)) <= self._WFT_CHUNK_BYTES:
            await super()._write_bytes_to_vm_path(data, vm_path)
            return
        await self._upload(_bytes_parts(data), vm_path)

    async def _write_unsigned_object(self, object_store: ObjectStore, object_url: str, vm_path: str) -> None:
        """Stream an object the store can't sign a URL for through Prized's files API, not as base64 over exec."""
        source = await asyncio.to_thread(object_store.open, object_url)
        try:
            await self._upload(_read_parts(source), vm_path)
        finally:
            source.close()

    async def _upload(self, parts: AsyncIterator[bytes], vm_path: str) -> None:
        """Upload ``parts`` one at a time to a file under the sandbox user's home, the files API's root, appending each
        to a file beside ``vm_path`` as root (as an exec would write it) and removing it, so the disk holds the object
        once plus a part; then move it into place and check its sha256."""
        sdk = await self._sdk()
        token = uuid.uuid4().hex
        building = f"{vm_path}.agentenv-{token}"
        target, partial = shlex.quote(vm_path), shlex.quote(building)
        digest = hashlib.sha256()
        staged: str | None = None
        parts_seen = 0
        try:
            async for part in parts:
                staged = f"{HOME}/.agentenv-upload-{token}.{parts_seen:05d}"
                await sdk.files.write(staged, part)
                digest.update(part)
                into = ">" if parts_seen == 0 else ">>"
                await self.exec_script(f"cat {shlex.quote(staged)} {into} {partial} && rm -f {shlex.quote(staged)}")
                staged = None
                parts_seen += 1
            if not parts_seen:
                await super()._write_bytes_to_vm_path(b"", vm_path)
                return
            written = await self.exec_script(f"mv -f {partial} {target} && sha256sum {target}")
        except BaseException:
            leftovers = [building] + ([staged] if staged else [])
            await self._remove_vm_temp_file(*leftovers)
            raise
        actual = written.split()[0] if written.split() else ""
        if actual != digest.hexdigest():
            raise RuntimeError(
                f"{vm_path} in Prized sandbox {self.sandbox_id} does not match the upload: its sha256 "
                f"is {actual or 'missing'}, the upload's {digest.hexdigest()}"
            )


def _helper_gone(process: CompletedProcess) -> bool:
    """Whether docker exec found the root helper container gone or stopped."""
    return process.exit_code not in (0, UNKNOWN_EXIT_CODE) and any(sign in process.stderr_text for sign in _HELPER_GONE)


async def _bytes_parts(data: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(data), UPLOAD_PART_BYTES):
        yield data[offset : offset + UPLOAD_PART_BYTES]


async def _read_parts(source: IO[bytes]) -> AsyncIterator[bytes]:
    """``source`` in upload-sized parts, each read in full (a reader may return less than asked), off the event loop."""
    while part := await asyncio.to_thread(_read_full, source, UPLOAD_PART_BYTES):
        yield part


def _read_full(source: IO[bytes], size: int) -> bytes:
    """Up to ``size`` bytes of ``source``, fewer only at its end."""
    chunks: list[bytes] = []
    left = size
    while left > 0:
        chunk = source.read(left)
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)
