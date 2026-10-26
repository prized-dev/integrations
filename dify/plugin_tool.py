"""What each tool does, and the base class that turns it into Dify messages.

Tool modules import this module (``import plugin_tool``), not the class, so
each of them holds exactly one Tool subclass, as Dify's loader expects.
"""

from __future__ import annotations

import codecs
import ipaddress
import posixpath
import socket
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

import prized_client as pc


class ToolError(Exception):
    """A failure shown to the workflow or agent; its text is safe to show."""


def make_client(credentials: Mapping[str, Any]) -> pc.PrizedClient:
    return pc.PrizedClient(credentials.get("api_key"))


Run = Callable[[pc.PrizedClient, Mapping[str, Any]], "Result"]


class Result:
    """A tool's answer: JSON (also each field as a workflow variable) and an optional file."""

    def __init__(self, data: dict[str, Any], blob: bytes | None = None, meta: dict | None = None):
        self.data = data
        self.blob = blob
        self.meta = meta


class PrizedTool(Tool):
    run: Run

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        try:
            client = make_client(self.runtime.credentials)
            try:
                result = self.run(client, tool_parameters)
            finally:
                client.close()
        except (pc.InputError, pc.PrizedError) as exc:
            raise ToolError(pc.scrub(str(exc))) from None
        except Exception:
            # Never pass on an unexpected exception's text: it could hold anything.
            raise ToolError("The Prized plugin failed unexpectedly. Retry, or check the sandbox's state.") from None
        if result.blob is not None:
            yield self.create_blob_message(result.blob, meta=result.meta)
        for name, value in result.data.items():
            if value is not None:
                yield self.create_variable_message(name, value)
        yield self.create_json_message(result.data)


# --- the tools ------------------------------------------------------------


def create_sandbox(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    sandbox = client.create(
        template=pc.choice(params, "template", pc.TEMPLATES, "prized-base-v1"),
        vcpu=pc.integer(params, "vcpu", 2, 1, 32),
        memory_mb=pc.integer(params, "memory_gb", 4, 1, 128) * 1024,
        max_runtime_min=pc.integer(params, "max_runtime_minutes", 60, 1, 10080),
        auto_pause_min=pc.integer(params, "auto_pause_minutes", 10, 0, 40320),
        auto_delete_min=pc.integer(params, "auto_delete_minutes", 1440, 0, 40320),
        labels=pc.parse_labels(pc.optional_text(params, "labels", 4096)),
    )
    return Result(pc.summarize(sandbox))


def get_sandbox(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    return Result(pc.summarize(client.get(pc.sandbox_id(params))))


def pause_sandbox(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    return Result(pc.summarize(client.pause(pc.sandbox_id(params))))


def resume_sandbox(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    return Result(pc.summarize(client.resume(pc.sandbox_id(params))))


def delete_sandbox(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    return Result(pc.summarize(client.delete(pc.sandbox_id(params))))


# A run with no sandbox_id gets a sandbox of its own, deleted after the run. Its run time is a
# backstop: if the delete fails, or Dify cuts the request off, it is gone within 15 minutes.
ONE_OFF = {
    "template": "prized-base-v1",
    "vcpu": 2,
    "memory_mb": 4096,
    "max_runtime_min": 15,
    "auto_pause_min": 0,
    "auto_delete_min": 0,
}
ONE_OFF_LABELS = {"one-off": "true"}


@contextmanager
def target_sandbox(client: pc.PrizedClient, params: Mapping[str, Any]) -> Iterator[tuple[dict[str, Any], bool]]:
    """The sandbox named by sandbox_id, or a one-off sandbox deleted afterwards: (sandbox, one_off)."""
    raw = params.get("sandbox_id")
    if raw is not None and not (isinstance(raw, str) and not raw.strip()):
        yield client.get(pc.sandbox_id(params)), False
        return
    sandbox = client.create(**ONE_OFF, labels=ONE_OFF_LABELS)
    try:
        yield sandbox, True
    finally:
        try:
            client.delete(sandbox["id"])
        except pc.PrizedError:
            pass  # its 15-minute run time deletes it


def _finish(result: dict[str, Any], one_off: bool) -> dict[str, Any]:
    if one_off:
        result = {k: v for k, v in result.items() if k != "sandbox_id"}  # already deleted: nothing to reuse
    return {**result, "one_off": one_off}


def run_command(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    command = pc.require_text(params, "command", pc.COMMAND_LIMIT)
    cwd = pc.optional_text(params, "cwd", 4096)
    cwd = pc.guest_path(cwd) if cwd else pc.WORKDIR
    timeout = pc.integer(params, "timeout_seconds", pc.DEFAULT_COMMAND_TIMEOUT, 1, pc.MAX_COMMAND_TIMEOUT)
    with target_sandbox(client, params) as (sandbox, one_off):
        result = client.run_command(sandbox["id"], command, cwd=cwd, timeout=timeout, sandbox=sandbox)
    return Result(_finish(result, one_off))


RUNNERS = {"python": ("py", "python3"), "javascript": ("js", "node"), "bash": ("sh", "bash")}


def run_code(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    language = pc.choice(params, "language", tuple(RUNNERS), "python")
    code = pc.require_text(params, "code", pc.CODE_LIMIT)
    timeout = pc.integer(params, "timeout_seconds", pc.DEFAULT_COMMAND_TIMEOUT, 1, pc.MAX_COMMAND_TIMEOUT)
    extension, program = RUNNERS[language]
    # The code goes in as a file beside the user's files (so imports from the working directory
    # work), nothing in it needs quoting, and it is removed after the run.
    path = f"{pc.WORKDIR}/.dify-run-{uuid.uuid4().hex}.{extension}"
    command = f"{program} {path}; status=$?; rm -f {path}; exit $status"
    with target_sandbox(client, params) as (sandbox, one_off):
        client.upload(sandbox["id"], path, code.encode(), sandbox=sandbox)
        result = client.run_command(sandbox["id"], command, cwd=pc.WORKDIR, timeout=timeout, sandbox=sandbox)
        if result["timed_out"] and not one_off:
            # A timeout kills the whole command, the rm included.
            try:
                client.run_command(sandbox["id"], f"rm -f {path}", cwd=pc.WORKDIR, timeout=30, sandbox=sandbox)
            except pc.PrizedError:
                pass
    return Result({**_finish(result, one_off), "language": language})


def upload_file(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    sid = pc.sandbox_id(params)
    file = params.get("file")
    url = getattr(file, "url", None)
    if not isinstance(url, str) or not url:
        raise pc.InputError("file is required: pick a file variable from the workflow.")
    if isinstance(getattr(file, "size", None), int) and file.size > pc.UPLOAD_LIMIT:
        raise pc.InputError(f"The file is over {pc.UPLOAD_LIMIT // (1024 * 1024)} MiB.")
    name = posixpath.basename(str(getattr(file, "filename", None) or "upload").replace("\\", "/")).strip()
    if not name or name in (".", "..") or "\0" in name:
        name = "upload"
    target = pc.optional_text(params, "path", 4096)
    if target is None:
        path = f"{pc.WORKDIR}/{name}"
    elif target.strip().endswith("/"):
        path = posixpath.join(pc.guest_path(target), name)
    else:
        path = pc.guest_path(target)
    data = fetch_file(url, pc.UPLOAD_LIMIT)
    return Result(client.upload(sid, path, data))


# --- fetching a workflow file -----------------------------------------------

FILE_TRANSPORT: httpx.BaseTransport | None = None  # tests put a fake here
RESOLVE = socket.getaddrinfo
FILE_REFUSED = (
    "Upload file only fetches files from a public address, and this file's URL points at an IP address "
    "or a private, loopback or link-local host. Set Dify's FILES_URL to a public URL."
)


def fetch_file(url: str, limit: int) -> bytes:
    """The workflow file's bytes, up to ``limit``, from its public http(s) URL.

    The URL comes from Dify, but a file can also be a remote URL a user typed, so the
    plugin refuses IP literals and hosts that resolve to private, loopback, link-local
    or other non-public addresses, follows no redirects, and stops reading at the cap.
    No error repeats the URL (it can carry Dify's signature).
    """
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise pc.InputError("The file's URL is not valid.") from None
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise pc.InputError("The file's URL is not an http(s) URL.")
    if _is_ip(host):
        raise pc.InputError(FILE_REFUSED)
    try:
        addresses = {info[4][0] for info in RESOLVE(host, port, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError):
        raise pc.InputError("Could not resolve the file's host.") from None
    for address in addresses:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            raise pc.InputError(FILE_REFUSED)
    try:
        with httpx.Client(transport=FILE_TRANSPORT, follow_redirects=False, timeout=60.0) as http:
            with http.stream("GET", url) as response:
                if response.status_code != 200:
                    raise pc.InputError(f"Could not read the file from Dify (HTTP {response.status_code}).")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise pc.InputError(f"The file is over {limit // (1024 * 1024)} MiB.")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError:
        raise pc.InputError("Could not read the file from Dify.") from None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def write_file(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    sid = pc.sandbox_id(params)
    path = pc.guest_path(pc.require_text(params, "path", 4096))
    content = params.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise pc.InputError("content must be text.")
    data = content.encode()
    if len(data) > pc.WRITE_LIMIT:
        raise pc.InputError(f"content is over {pc.WRITE_LIMIT // (1024 * 1024)} MiB.")
    return Result(client.upload(sid, path, data))


def read_file(client: pc.PrizedClient, params: Mapping[str, Any]) -> Result:
    sid = pc.sandbox_id(params)
    path = pc.guest_path(pc.require_text(params, "path", 4096))
    output = pc.choice(params, "output", ("text", "file"), "text")
    if output == "file":
        data, size, cut = client.download(sid, path, pc.READ_FILE_LIMIT)
        if cut:
            raise pc.InputError(
                f"The file is over {pc.READ_FILE_LIMIT // (1024 * 1024)} MiB; "
                "compress or split it in the sandbox first."
            )
        name = posixpath.basename(path)
        meta = {"mime_type": pc.guess_mime(path), "filename": name}
        return Result({"sandbox_id": sid, "path": path, "size": len(data), "filename": name}, blob=data, meta=meta)
    data, size, cut = client.download(sid, path, pc.READ_TEXT_LIMIT)
    try:
        # A cut can split a character; only a cut file may end mid-character.
        text, binary = codecs.getincrementaldecoder("utf-8")().decode(data, final=not cut), False
    except UnicodeDecodeError:
        text, binary = data.decode("utf-8", errors="replace"), True
    return Result(
        {
            "sandbox_id": sid,
            "path": path,
            "content": text,
            "size": size if size is not None else len(data),
            "truncated": cut,
            "binary": binary,
        }
    )
