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
from dataclasses import dataclass
from typing import Any

import gevent
import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

import prized_client as pc


class ToolError(Exception):
    """A failure shown to the workflow or agent; its text is safe to show."""


def make_client(credentials: Mapping[str, Any]) -> pc.PrizedClient:
    return pc.PrizedClient(credentials.get("api_key"))


@dataclass(frozen=True)
class Settings:
    """The provider's settings besides the key, from the same credentials form."""

    # A self-hosted Dify's own file server, which Upload file trusts even at a private address.
    dify_files: FilesOrigin | None = None

    @classmethod
    def from_credentials(cls, credentials: Mapping[str, Any]) -> Settings:
        return cls(dify_files=files_origin(credentials.get("dify_files_url")))


Run = Callable[[pc.PrizedClient, Mapping[str, Any], Settings], "Result"]


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
            settings = Settings.from_credentials(self.runtime.credentials)
            client = make_client(self.runtime.credentials)
            try:
                result = self.run(client, tool_parameters, settings)
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


def create_sandbox(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
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


def get_sandbox(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
    return Result(pc.summarize(client.get(pc.sandbox_id(params))))


def pause_sandbox(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
    return Result(pc.summarize(client.pause(pc.sandbox_id(params))))


def resume_sandbox(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
    return Result(pc.summarize(client.resume(pc.sandbox_id(params))))


def delete_sandbox(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
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


def run_command(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
    command = pc.require_text(params, "command", pc.COMMAND_LIMIT)
    cwd = pc.optional_text(params, "cwd", 4096)
    cwd = pc.guest_path(cwd) if cwd else pc.WORKDIR
    timeout = pc.integer(params, "timeout_seconds", pc.DEFAULT_COMMAND_TIMEOUT, 1, pc.MAX_COMMAND_TIMEOUT)
    with target_sandbox(client, params) as (sandbox, one_off):
        result = client.run_command(sandbox["id"], command, cwd=cwd, timeout=timeout, sandbox=sandbox)
    return Result(_finish(result, one_off))


RUNNERS = {"python": ("py", "python3"), "javascript": ("js", "node"), "bash": ("sh", "bash")}


def run_code(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
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


def upload_file(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
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
    data = fetch_file(url, pc.UPLOAD_LIMIT, trusted=settings.dify_files)
    return Result(client.upload(sid, path, data))


# --- fetching a workflow file -----------------------------------------------

FILE_TRANSPORT: httpx.BaseTransport | None = None  # tests put a fake here
RESOLVE = socket.getaddrinfo
FETCH_DEADLINE = 240.0  # seconds for the whole fetch: lookup, connect, headers and body
DEFAULT_PORTS = {"http": 80, "https": 443}
FILE_REFUSED = (
    "Upload file fetches only from public addresses, and this file's URL points at an IP address or a "
    "private, loopback or link-local host. On a self-hosted Dify, put the address Dify gives plugins for "
    "files (its INTERNAL_FILES_URL, such as http://api:5001) in the plugin's Dify file URL setting."
)
NO_FILES_URL = (
    "Dify gave the plugin a file path with no address. Set INTERNAL_FILES_URL or FILES_URL in Dify's "
    "configuration so plugins can fetch files."
)
BAD_FILES_URL = "Dify file URL must be an http(s) address such as http://api:5001, with no user, query or fragment."
# IPv6 ranges that carry or reach an IPv4 address (IPv4-compatible, IPv4-mapped, NAT64, 6to4,
# Teredo) or are old site-local: not public, whatever ipaddress says on this Python version.
NOT_PUBLIC_V6 = tuple(
    ipaddress.ip_network(net)
    for net in ("::/96", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "fec0::/10")
)
CARRIES_V4 = tuple(ipaddress.ip_network(net) for net in ("::/96", "64:ff9b::/96"))  # in its last 32 bits
# Cloud metadata services outside link-local: AWS over IPv6, Alibaba Cloud.
METADATA = frozenset(ipaddress.ip_address(a) for a in ("fd00:ec2::254", "100.100.100.200"))


@dataclass(frozen=True)
class FilesOrigin:
    """A self-hosted Dify's file server, as set in the plugin's Dify file URL setting."""

    scheme: str
    host: str
    port: int
    prefix: str  # the path its files sit under, ending in /files/

    def holds(self, url: httpx.URL) -> bool:
        path = url.raw_path.split(b"?", 1)[0].decode("ascii", "replace")
        return (
            (url.scheme, _host(url), _port(url)) == (self.scheme, self.host, self.port)
            and path.startswith(self.prefix)
            # httpx has already removed dot segments; refuse encoded ones and encoded slashes.
            and not any(token in path.lower() for token in ("%2e", "%2f", "%5c", "\\"))
        )


def files_origin(value: Any) -> FilesOrigin | None:
    """The Dify file URL setting (empty: none), such as ``http://api:5001``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise pc.InputError(BAD_FILES_URL)
    try:
        url = httpx.URL(value.strip())
        if url.scheme not in DEFAULT_PORTS or not url.raw_host or url.userinfo or url.query or url.fragment:
            raise ValueError("not an http(s) base URL")
        return FilesOrigin(url.scheme, _host(url), _port(url), url.raw_path.decode("ascii").rstrip("/") + "/files/")
    except (httpx.InvalidURL, ValueError):
        raise pc.InputError(BAD_FILES_URL) from None


def fetch_file(url: str, limit: int, trusted: FilesOrigin | None = None) -> bytes:
    """The workflow file's bytes, up to ``limit``, from the http(s) URL Dify gives the plugin.

    A file can also be a remote URL a user typed, so the plugin refuses IP literals and hosts
    that resolve to private, loopback, link-local or other non-public addresses (except the
    configured Dify file server, which may be private but never link-local), connects only to
    an address it checked, follows no redirects, and stops reading at the cap. No error
    repeats the URL (it can carry Dify's signature).
    """
    if url.startswith("/files/"):
        raise pc.InputError(NO_FILES_URL)
    # A wall clock over the whole fetch: httpx's timeouts are per read, so a server could
    # otherwise trickle its headers or body for hours. Dify runs plugins under gevent.
    timer = gevent.Timeout(FETCH_DEADLINE)
    timer.start()
    try:
        return _fetch(url, limit, trusted)
    except gevent.Timeout as exc:
        if exc is not timer:
            raise
        raise pc.InputError("Fetching the file took too long.") from None
    finally:
        timer.close()


def _fetch(url: str, limit: int, trusted: FilesOrigin | None) -> bytes:
    try:
        target = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        raise pc.InputError("The file's URL is not valid.") from None
    if target.scheme not in DEFAULT_PORTS or not target.raw_host or target.userinfo:
        raise pc.InputError("The file's URL is not an http(s) URL.")
    try:
        port = _port(target)
    except ValueError:
        raise pc.InputError("The file's URL is not valid.") from None
    host = _host(target)
    own = trusted is not None and trusted.holds(target)
    if _is_ip(host) and not own:
        raise pc.InputError(FILE_REFUSED)
    try:
        infos = RESOLVE(host, port, proto=socket.IPPROTO_TCP)
        addresses = list(dict.fromkeys(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos))
    except (OSError, UnicodeError, ValueError):
        raise pc.InputError("Could not resolve the file's host.") from None
    if not addresses or not all(_allowed(ip, own) for ip in addresses):
        raise pc.InputError(FILE_REFUSED)
    # Connect to an address checked above, never to a second lookup of the name (which could
    # answer differently): the request goes to the IP, with the name in the Host header and in
    # TLS (SNI and the certificate check).
    for ip in addresses[:3]:
        try:
            return _download(target, str(ip), limit)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            continue
    raise pc.InputError("Could not connect to the file's host.")


def _download(target: httpx.URL, ip: str, limit: int) -> bytes:
    too_big = pc.InputError(f"The file is over {limit // (1024 * 1024)} MiB.")
    headers = {"Host": target.netloc.decode("ascii"), "Accept-Encoding": "identity"}
    extensions = {"sni_hostname": _host(target).rstrip(".")} if target.scheme == "https" else {}
    # trust_env=False: no proxy from the environment, which would make the connection itself
    # (an HTTPS proxy tunnel would also check the certificate against the IP, not the name).
    # Certificates are still checked against SSL_CERT_FILE or SSL_CERT_DIR when set.
    tls = httpx.create_ssl_context(trust_env=True)
    with httpx.Client(
        transport=FILE_TRANSPORT, verify=tls, follow_redirects=False, timeout=60.0, trust_env=False
    ) as http:
        try:
            with http.stream("GET", target.copy_with(host=ip), headers=headers, extensions=extensions) as response:
                if response.status_code != 200:
                    raise pc.InputError(f"Could not read the file from its URL (HTTP {response.status_code}).")
                if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                    raise pc.InputError("The file's server sent it compressed, which Upload file does not accept.")
                size = response.headers.get("content-length", "").strip()
                if size.isdigit() and int(size) > limit:
                    raise too_big
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_raw():  # as sent: nothing is decompressed
                    total += len(chunk)
                    if total > limit:
                        raise too_big
                    chunks.append(chunk)
                return b"".join(chunks)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise
        except httpx.HTTPError:
            raise pc.InputError("Could not read the file from its URL.") from None


def _host(url: httpx.URL) -> str:
    return url.raw_host.decode("ascii").lower().strip("[]")


def _port(url: httpx.URL) -> int:
    port = url.port if url.port is not None else DEFAULT_PORTS.get(url.scheme, 0)
    if not 0 < port < 65536:
        raise ValueError("port out of range")
    return port


def _allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, own: bool) -> bool:
    for each in _with_embedded(ip):
        if each.is_link_local or each.is_multicast or each.is_unspecified or each in METADATA:
            return False  # never, not even for Dify's own file server: that is where cloud metadata lives
    if own:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in net for net in NOT_PUBLIC_V6):
        return False
    return ip.is_global


def _with_embedded(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list:
    """The address, and any IPv4 address an IPv6 one carries."""
    found = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        found += [v4 for v4 in (ip.ipv4_mapped, ip.sixtofour, ip.teredo and ip.teredo[1]) if v4]
        if any(ip in net for net in CARRIES_V4):
            found.append(ipaddress.IPv4Address(ip.packed[-4:]))
    return found


def _is_ip(host: str) -> bool:
    """An IP literal, in any form the resolver takes as one (``134744072``, ``0x8.8.8.8``, ``8.8.8``)."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def write_file(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
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


def read_file(client: pc.PrizedClient, params: Mapping[str, Any], settings: Settings) -> Result:
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
