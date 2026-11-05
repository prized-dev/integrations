"""Exposed ports: a port of the sandbox at an https URL.

Every port of a Prized sandbox but 22 and 81 answers at
`https://<port>-<sandbox id>.p.prized.dev/` (the sandbox's
`previewUrlTemplate`), on any address in the sandbox, for a request that
carries a credential (https://prized.dev/docs/sandboxes-api#ports-at-a-url).
`resolve_exposed_port()` returns that URL as an `ExposedPortEndpoint`, with
the credential `exposed_port_auth` names:

- `token` (the default): each call makes a token for that one port, good for
  `exposed_port_url_ttl_s` seconds, and puts it in the URL's query
  (`prized_token`). The URL is on the browser domain (`prized.link`) where the
  deployment has one, so it opens in a browser tab (whose first visit shows
  Prized's notice) as well as for programs, `fetch`, and WebSockets. A token
  never wakes a paused sandbox, so the call resumes one the session holds
  that paused after `auto_pause_min`. Needs a workspace that has paid in.
- `key`: the bare URL on the API's domain; each request sends the API key in
  the `Prized-Key` header. It wakes a paused sandbox, never expires, and works
  on the free tier, for clients that can send a header.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import parse_qs, urlsplit

from agents.sandbox.errors import ExposedPortUnavailableError
from agents.sandbox.types import ExposedPortEndpoint

from openai_agents_prized._api import PrizedAPIError, is_loopback, trusted_edge
from openai_agents_prized._core import PrizedSessionBase, api_context, api_retryable
from openai_agents_prized._options import RESERVED_PORTS

if TYPE_CHECKING:
    from collections.abc import Mapping

_HOST = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
"""A host name and nothing else: no backslash or other character a URL parser reads apart."""

_NEEDS_PAYMENT_HINT = (
    "A port token needs a workspace that has paid in (https://prized.dev/dashboard/billing). "
    "On the free tier, set exposed_port_auth='key' and send the API key in the "
    "Prized-Key header."
)


def _token_url(answer: Mapping[str, object]) -> str | None:
    """The token answer's URL that carries the token: `browserUrl` first, else `url`."""
    for name in ("browserUrl", "url"):
        value = answer.get(name)
        if not isinstance(value, str) or not value:
            continue
        try:
            query = parse_qs(urlsplit(value).query)
        except ValueError:
            continue
        if query.get("prized_token"):
            return value
    return None


class PrizedPortsMixin(PrizedSessionBase):
    """`resolve_exposed_port()` for a Prized sandbox."""

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        # Once the session has paused or deleted its sandbox, a URL would only
        # fail (a token) or wake it again (the key), so none is given.
        if self._releasing:
            self._refuse(port, "session_released")
        if port in RESERVED_PORTS:  # a state written by hand can name one
            self._refuse(port, "port_reserved")
        try:
            sandbox = await self._client.get_sandbox(self.state.sandbox_id)
            if self.state.exposed_port_auth == "key":
                template = sandbox.get("previewUrlTemplate")
                if not isinstance(template, str) or "{port}" not in template:
                    self._refuse(port, "preview_unavailable")
                return self._endpoint(port, template.replace("{port}", str(port), 1), key=True)
            # A token never wakes a sandbox: one that paused after
            # auto_pause_min is resumed first, or no URL is given.
            if sandbox.get("state") != "running" and not await self._wake():
                self._refuse(port, "sandbox_not_running")
            answer = await self._client.create_port_token(
                self.state.sandbox_id, port, expires_in=self.state.exposed_port_url_ttl_s
            )
            url = _token_url(answer)
            if url is None:
                self._refuse(port, "token_missing")
            return self._endpoint(port, url, key=False)
        except PrizedAPIError as exc:
            extra: dict[str, object] = {"auth": self.state.exposed_port_auth}
            if exc.code == "share_links_need_payment":
                extra["hint"] = _NEEDS_PAYMENT_HINT
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context=api_context(exc, **extra),
                cause=exc,
                retryable=api_retryable(exc, idempotent=True),
            ) from exc

    def _refuse(self, port: int, detail: str, host: str | None = None) -> NoReturn:
        context: dict[str, object] = {"backend": "prized", "detail": detail}
        if host is not None:
            context["host"] = host
        raise ExposedPortUnavailableError(
            port=port,
            exposed_ports=self.state.exposed_ports,
            reason="backend_unavailable",
            context=context,
            retryable=False,
        )

    def _endpoint(self, port: int, url: str, *, key: bool) -> ExposedPortEndpoint:
        """The endpoint for a URL the API gave, refusing one a client must not use.

        The host must be a plain host name whose first label is this sandbox's
        port (`<port>-<sandbox id>`), so no parser reads another host out of it.
        With `key`, the caller sends the API key there, so the host must also
        be one the key may go to (`trusted_edge`). A token URL's query is the
        token: it is never put in an error.
        """
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            explicit_port = parts.port
        except ValueError:
            self._refuse(port, "invalid_preview_url")
        loopback = is_loopback(host)
        plain = loopback or _HOST.fullmatch(host) is not None
        secure = parts.scheme == "https" or (parts.scheme == "http" and loopback)
        label = f"{port}-{self.state.sandbox_id.replace('_', '-')}"
        api_host = urlsplit(self._client.api_url).hostname
        if (
            not plain
            or not secure
            or parts.username
            or parts.password
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            self._refuse(port, "invalid_preview_url", host if plain else None)
        if not loopback and host.split(".", 1)[0] != label:
            self._refuse(port, "foreign_preview_host", host)
        if key and (parts.query or not trusted_edge(host, api_host)):
            self._refuse(port, "untrusted_preview_host", host)
        tls = parts.scheme == "https"
        return ExposedPortEndpoint(
            host=host,
            port=explicit_port or (443 if tls else 80),
            tls=tls,
            query=parts.query,
        )


__all__ = ["PrizedPortsMixin"]
