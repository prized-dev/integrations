"""Connect an outpost through Devin's partner flow (PKCE), as https://docs.devin.ai/cloud/outposts/partners
describes: a Devin admin confirms in their browser, the one-time code comes back to a listener on
this machine, and it is exchanged server-to-server for the outpost's service-user token. The token
never passes through the browser.

The callback URL must be on Cognition's allowlist for Prized; until it is, use the manual setup
(create the outpost and a service-user token in Devin, then set them in the environment).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import urllib.parse
from dataclasses import dataclass

import httpx

CONNECT_URL = "https://app.devin.ai/outposts/connect"
TOKEN_URL = "https://api.devin.ai/outposts/connection-token"
CALLBACK_URL = "http://localhost:8977/prized/devin/callback"
# A PNG icon Devin shows next to the outpost. prized.dev serves no PNG logo yet, so none is sent.
ICON_URL: str | None = None


class ConnectError(Exception):
    pass


@dataclass
class Connection:
    outpost_id: str
    outpost_name: str
    account_id: str | None
    service_user_id: str | None
    api_base_url: str
    access_token: str

    def __repr__(self) -> str:  # never show the token
        return f"Connection(outpost_id={self.outpost_id!r}, outpost_name={self.outpost_name!r})"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)  # 64 characters from the unreserved alphabet
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def connect_url(
    challenge: str,
    *,
    callback_url: str = CALLBACK_URL,
    outpost_name: str | None = None,
    platform: str = "linux",
    icon_url: str | None = ICON_URL,
    base: str = CONNECT_URL,
) -> str:
    params = {"callback_url": callback_url, "code_challenge": challenge, "platform": platform}
    if outpost_name:
        params["outpost_name"] = outpost_name
    if icon_url:
        params["outpost_image"] = icon_url
    return f"{base}?{urllib.parse.urlencode(params)}"


def wait_for_code(callback_url: str = CALLBACK_URL, timeout: float = 600.0) -> str:
    """Listen once on the loopback callback and return the code Devin relays to it."""
    parsed = urllib.parse.urlparse(callback_url)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ConnectError("the callback listener only runs on a loopback http URL")
    got: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path != parsed.path:
                self.send_error(404)
                return
            if "code" not in query:
                # Only Devin's redirect carries a code. Anything else (a stray request from another page
                # in the browser) is answered and ignored, so it can't end the attempt.
                self.send_error(400, "no code")
                return
            got["code"] = query["code"][0]
            body = b"Connected. You can close this tab and go back to your terminal.\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args: object) -> None:  # keep the code out of logs
            return

    server = http.server.HTTPServer(("127.0.0.1", parsed.port or 80), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout):
            raise ConnectError("no answer from Devin within the time allowed; start again")
    finally:
        server.shutdown()
        server.server_close()
    return got["code"]


def exchange(
    code: str, verifier: str, *, token_url: str = TOKEN_URL, http: httpx.Client | None = None
) -> Connection:
    client = http or httpx.Client(timeout=30.0)
    resp = client.post(
        token_url,
        data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code == 400:
        raise ConnectError("Devin refused the code (expired, used, or not ours); start again")
    if not resp.is_success:
        raise ConnectError(f"Devin's token endpoint answered {resp.status_code}")
    try:
        body = resp.json()
        return Connection(
            outpost_id=body["outpost_id"],
            outpost_name=body.get("outpost_name") or "",
            account_id=body.get("account_id"),
            service_user_id=body.get("service_user_id"),
            api_base_url=body.get("api_base_url") or "https://api.devin.ai",
            access_token=body["access_token"],
        )
    except (ValueError, KeyError) as err:
        raise ConnectError("Devin's token endpoint answered without the expected fields") from err
