"""Upload file's fetch of a workflow file: which addresses it reaches, and how it reads."""

from __future__ import annotations

import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import KEY, StubFile, outputs, tool
from dify_plugin.errors.tool import ToolProviderCredentialValidationError
from test_errors import provider

import plugin_tool
import prized_client as pc

SID = "sbx_00000000000000aa"
URL = "https://files.dify.example/files/abc/file-preview?timestamp=1&sign=SECRETSIGNATURE"


def upload(url: str, **credentials) -> dict:
    tool_cls = tool("upload_file")
    instance = tool_cls.from_credentials({"api_key": KEY, **credentials})
    _, data, _ = outputs(list(instance.invoke({"sandbox_id": SID, "file": StubFile(url, "a.csv")})))
    return data


def test_the_connection_goes_to_the_checked_address_with_the_name_in_host_and_tls(fake, files):
    fake.add()
    files.content[URL] = b"a,b\n"
    upload(URL)
    (request,) = files.requests
    assert files.connected == ["93.184.216.34"] and files.lookups == ["files.dify.example"]
    assert request.url.host == "93.184.216.34"  # no second lookup of the name
    assert request.headers["host"] == "files.dify.example"
    assert request.extensions["sni_hostname"] == "files.dify.example"  # the certificate is checked for the name
    assert request.headers["accept-encoding"] == "identity"
    assert "authorization" not in request.headers


def test_a_name_that_rebinds_after_the_check_is_still_reached_at_the_checked_address(fake, files):
    fake.add()
    files.content[URL] = b"x"
    answers = iter(["93.184.216.34", "127.0.0.1"])
    real = files.resolve

    def rebinding(host, port, proto=0):
        files.addresses[host] = next(answers)
        return real(host, port, proto)

    plugin_tool.RESOLVE = rebinding  # the files fixture restores it
    upload(URL)
    assert files.connected == ["93.184.216.34"] and len(files.lookups) == 1


def test_a_trailing_dot_name_is_resolved_as_given(fake, files):
    fake.add()
    url = URL.replace("files.dify.example", "files.dify.example.")
    files.addresses["files.dify.example."] = "93.184.216.34"
    files.content[url] = b"x"
    upload(url)
    assert files.lookups == ["files.dify.example."]
    assert files.requests[0].extensions["sni_hostname"] == "files.dify.example"


def test_every_address_of_the_name_must_be_public(fake, files):
    fake.add()
    files.addresses["files.dify.example"] = ["93.184.216.34", "10.0.0.7"]
    files.content[URL] = b"x"
    with pytest.raises(plugin_tool.ToolError, match="public addresses"):
        upload(URL)
    assert files.connected == []


def test_the_next_checked_address_is_tried_when_one_refuses(fake, files):
    fake.add()
    files.addresses["files.dify.example"] = ["93.184.216.34", "93.184.216.35"]
    files.refuse.add("93.184.216.34")
    files.content[URL] = b"x"
    assert upload(URL)["size"] == 1
    assert files.connected == ["93.184.216.34", "93.184.216.35"]


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "64:ff9b:1::a00:1",  # local-use NAT64
        "::7f00:1",  # IPv4-compatible 127.0.0.1
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:8.8.8.8",  # IPv4-mapped, even of a public address
        "fec0::1",  # site-local
        "2002:7f00:1::1",  # 6to4 of 127.0.0.1
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "fe80::1%eth0",  # link-local, with a scope
        "0.0.0.0",  # noqa: S104
        "224.0.0.1",
        "198.18.0.1",  # benchmarking
        "168.63.129.16",  # Azure's WireServer, which counts as a global address
    ],
)
def test_addresses_that_reach_back_inside_are_refused(fake, files, address):
    fake.add()
    files.addresses["files.dify.example"] = address
    with pytest.raises(plugin_tool.ToolError, match="public addresses"):
        upload(URL)
    assert files.connected == []


@pytest.mark.parametrize(
    "url,message",
    [
        ("http://[::1/x", "not valid"),
        ("https://files.dify.example:99999/x", "not valid"),
        ("ftp://files.dify.example/x", "not an http"),
        ("https://user:pw@files.dify.example/x", "not an http"),
        ("http:///x", "not an http"),
        ("/files/abc/file-preview?sign=x", "INTERNAL_FILES_URL or FILES_URL"),
    ],
)
def test_malformed_urls_are_refused_with_a_clear_message(fake, files, url, message):
    fake.add()
    with pytest.raises(plugin_tool.ToolError, match=message) as caught:
        upload(url)
    assert "unexpectedly" not in str(caught.value) and "sign=" not in str(caught.value)
    assert files.connected == []


def test_a_big_content_length_is_refused_before_reading(fake, files, monkeypatch):
    fake.add()
    monkeypatch.setattr(pc, "UPLOAD_LIMIT", 1024 * 1024)
    files.content[URL] = b"x" * 10
    files.headers = {"content-length": str(2 * 1024 * 1024)}
    with pytest.raises(plugin_tool.ToolError, match="over 1 MiB"):
        upload(URL)
    assert not any(r.url.path.endswith("/files/upload") for r in fake.requests)


def test_a_compressed_answer_is_refused_rather_than_decompressed(fake, files):
    fake.add()
    files.content[URL] = b"\x1f\x8b" + b"\x00" * 20
    files.headers = {"content-encoding": "gzip"}
    with pytest.raises(plugin_tool.ToolError, match="compressed"):
        upload(URL)


@pytest.mark.parametrize("phase,delay", [("header_delay", 1.0), ("chunk_delay", 0.25)])
def test_a_slow_server_is_cut_off_at_the_deadline(fake, files, monkeypatch, phase, delay):
    """The deadline covers the whole fetch: a server trickling its headers is cut off too."""
    fake.add()
    files.content[URL] = b"x" * (200 * 1024)  # four pieces
    setattr(files, phase, delay)  # 1 s before the headers, or 1 s across the body's four pieces
    monkeypatch.setattr(plugin_tool, "FETCH_DEADLINE", 0.3)
    start = time.monotonic()
    with pytest.raises(plugin_tool.ToolError, match="took too long"):
        upload(URL)
    assert time.monotonic() - start < 0.8
    assert not any(r.url.path.endswith("/files/upload") for r in fake.requests)


@pytest.mark.parametrize("host", ["134744072", "0x8.0x8.0x8.0x8", "8.8.8", "0177.0.0.1"])
def test_other_ip_literal_forms_are_refused_before_any_lookup(fake, files, host):
    fake.add()
    with pytest.raises(plugin_tool.ToolError, match="public addresses|not valid"):
        upload(f"http://{host}/files/x")
    assert files.lookups == [] and files.connected == []


# --- the Dify file URL setting (a self-hosted Dify's own file server) ---------

INTERNAL = "http://api:5001/files/abc/file-preview?timestamp=1&sign=SIG"


def test_without_the_setting_a_private_dify_file_server_is_refused(fake, files):
    fake.add()
    files.addresses["api"] = "172.18.0.4"
    with pytest.raises(plugin_tool.ToolError, match="Dify file URL setting"):
        upload(INTERNAL)
    assert files.connected == []


def test_the_setting_lets_upload_file_fetch_from_that_server(fake, files):
    fake.add()
    files.addresses["api"] = "172.18.0.4"
    files.content[INTERNAL] = b"a,b\n"
    data = upload(INTERNAL, dify_files_url="http://api:5001")
    assert data["size"] == 4 and fake.files[(SID, "/home/prized/work/a.csv")] == b"a,b\n"
    assert files.connected == ["172.18.0.4"] and files.requests[0].headers["host"] == "api:5001"


@pytest.mark.parametrize(
    "setting,url",
    [
        ("http://api:5001", "http://api:5001/console/api/apps"),  # not under /files/
        ("http://api:5001", "http://api:5001/files/%2e%2e/console/api/apps"),  # an encoded dot segment
        ("http://api:5001", "http://api:5001/files/..%2fconsole"),  # an encoded slash
        ("http://api:5001", "http://api:5002/files/abc"),  # another port
        ("http://api:5001", "https://api:5001/files/abc"),  # another scheme
        ("http://api:5001", "http://other:5001/files/abc"),  # another host
        ("https://dify.example.com/sub", "https://dify.example.com/files/abc"),  # outside its path
    ],
)
def test_the_setting_trusts_only_its_own_files(fake, files, setting, url):
    fake.add()
    files.addresses.update({"api": "172.18.0.4", "other": "172.18.0.5", "dify.example.com": "10.0.0.9"})
    with pytest.raises(plugin_tool.ToolError, match="public addresses"):
        upload(url, dify_files_url=setting)
    assert files.connected == []


def test_the_setting_keeps_a_path_prefix(fake, files):
    fake.add()
    files.addresses["dify.example.com"] = "10.0.0.9"
    url = "https://dify.example.com/sub/files/abc/file-preview?sign=x"
    files.content[url] = b"x"
    assert upload(url, dify_files_url="https://dify.example.com/sub/")["size"] == 1


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "::ffff:169.254.169.254",  # IPv4-mapped
        "::a9fe:a9fe",  # IPv4-compatible
        "64:ff9b::a9fe:a9fe",  # NAT64
        "2002:a9fe:a9fe::1",  # 6to4
        "fe80::1",
        "fd00:ec2::254",  # AWS metadata over IPv6
        "100.100.100.200",  # Alibaba Cloud metadata
        "192.0.0.192",  # Oracle Cloud metadata
        "168.63.129.16",  # Azure WireServer
    ],
)
def test_the_setting_never_opens_metadata_or_link_local_addresses(fake, files, address):
    fake.add()
    files.addresses["api"] = address
    with pytest.raises(plugin_tool.ToolError, match="public addresses"):
        upload(INTERNAL, dify_files_url="http://api:5001")
    assert files.connected == []


def test_port_zero_is_not_the_default_port(fake, files):
    fake.add()
    files.addresses["api"] = "172.18.0.4"
    with pytest.raises(plugin_tool.ToolError, match="not valid"):
        upload("http://api:0/files/x", dify_files_url="http://api")
    assert files.connected == []


@pytest.mark.parametrize(
    "setting",
    [
        "ftp://api:5001",
        "http://user:pw@api:5001",
        "http://api:5001?x=1",
        "http://api:5001#x",
        "api:5001",
        "http://[::1",
    ],
)
def test_a_malformed_setting_is_refused_at_authorize_and_at_use(fake, files, setting):
    with pytest.raises(ToolProviderCredentialValidationError, match="Dify file URL must be"):
        provider().validate_credentials({"api_key": KEY, "dify_files_url": setting})
    fake.add()
    with pytest.raises(plugin_tool.ToolError, match="Dify file URL must be"):
        upload(URL, dify_files_url=setting)
    assert fake.requests == []


@pytest.mark.parametrize("setting", [None, "", "  ", "http://api:5001", "https://dify.example.com/sub/"])
def test_a_good_or_empty_setting_passes_authorize(monkeypatch, setting):
    from conftest import FakePrized
    from test_errors import _client_with

    monkeypatch.setattr(pc, "PrizedClient", _client_with(FakePrized()))
    provider().validate_credentials({"api_key": KEY, "dify_files_url": setting})


# --- a real TLS connection to the checked address --------------------------------


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    """A throwaway CA and certificates for files.test and other.test, made with openssl."""
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("needs openssl")
    where = tmp_path_factory.mktemp("tls")

    def run(*args: str) -> None:
        subprocess.run([openssl, *args], cwd=where, check=True, capture_output=True)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "1",
        "-subj", "/CN=Test CA", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")  # fmt: skip
    for name in ("files.test", "other.test"):
        (where / f"{name}.ext").write_text(
            f"subjectAltName=DNS:{name}\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\n"
        )
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
            "-subj", f"/CN={name}")  # fmt: skip
        run("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", f"{name}.pem", "-days", "1", "-extfile", f"{name}.ext")  # fmt: skip
    return where


def serve_tls(certs, name: str, seen: dict) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.update(
                host=self.headers.get_all("host"),
                auth=self.headers.get("authorization"),
                encoding=self.headers.get("accept-encoding"),
            )
            self.send_response(200)
            self.send_header("content-length", "5")
            self.end_headers()
            self.wfile.write(b"hello")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / f"{name}.pem", certs / f"{name}.key")
    context.sni_callback = lambda sock, sni, ctx: seen.update(sni=sni)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("served,ok", [("files.test", True), ("other.test", False)])
def test_tls_to_the_checked_address_verifies_the_certificate_for_the_name(certs, monkeypatch, served, ok):
    """files.test resolves to 127.0.0.1 (let through by the Dify file URL setting); the plugin
    connects to that IP, and TLS must still check the certificate against files.test."""
    seen: dict = {}
    server = serve_tls(certs, served, seen)
    try:
        port = server.server_port
        monkeypatch.setenv("SSL_CERT_FILE", str(certs / "ca.pem"))
        monkeypatch.setattr(plugin_tool, "RESOLVE", lambda host, p, proto=0: [(2, 1, 6, "", ("127.0.0.1", p))])
        trusted = plugin_tool.files_origin(f"https://files.test:{port}")
        url = f"https://files.test:{port}/files/a?sign=x"
        if ok:
            assert plugin_tool.fetch_file(url, 1024, trusted=trusted) == b"hello"
            assert seen == {"sni": "files.test", "host": [f"files.test:{port}"], "auth": None, "encoding": "identity"}
        else:
            with pytest.raises(pc.InputError, match="Could not connect"):
                plugin_tool.fetch_file(url, 1024, trusted=trusted)
            assert "host" not in seen  # no request was sent over the unverified connection
    finally:
        server.shutdown()
