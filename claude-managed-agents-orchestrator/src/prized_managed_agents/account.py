"""What the Prized API says about the workspace rather than one sandbox: whether it is on the free
tier, and why the free tier deleted a sandbox. The SDK covers sandboxes only, so these two reads go
to the same API, with the same key, over plain HTTPS.

- ``GET /api/v1/me``: ``billing.trial`` is true while the workspace is on its free credit. A key
  without Workspace: Read (a key for sandboxes only) gets ``billing: null``.
- ``GET /api/v1/sandboxes?ended=1``: on the free tier, ``ended`` lists the sandboxes it deleted at
  their run limit in the last 7 days (``{id, endedAt, reason, limitMin, message}``); the key is left
  out when there are none, and off the free tier.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("prized_managed_agents.account")


def _default_base() -> str:
    from prized import _core

    return str(getattr(_core, "DEFAULT_BASE_URL", "https://api.prized.dev")).rstrip("/")


class PrizedAccount:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._http = http_client
        self._own = http_client is None
        self.api_key = api_key if api_key is not None else os.environ.get("PRIZED_API_KEY", "")
        self.base = (base_url or _default_base()).rstrip("/")

    async def _get(self, path: str) -> dict[str, Any] | None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0)
        try:
            r = await self._http.get(
                f"{self.base}/api/v1{path}", headers={"Authorization": f"Bearer {self.api_key}"}, timeout=20.0
            )
        except httpx.HTTPError as err:
            log.debug("GET %s failed: %s", path, err)
            return None
        if r.status_code != 200:
            return None
        try:
            doc = r.json()
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None

    async def free_tier(self) -> tuple[bool | None, str]:
        """True on the free tier, False off it, None when the key can't tell; and how it was told."""
        me = await self._get("/me")
        billing = (me or {}).get("billing")
        if isinstance(billing, dict) and "trial" in billing:
            return bool(billing.get("trial")), "billing.trial in GET /api/v1/me"
        listed = await self._get("/sandboxes?ended=1")
        if listed is not None and "ended" in listed:
            return True, "the free tier's ended sandboxes in GET /api/v1/sandboxes?ended=1"
        return None, "the key can't read billing"

    async def ended(self) -> dict[str, dict[str, Any]]:
        """The sandboxes the free tier deleted at their run limit lately, by id."""
        listed = await self._get("/sandboxes?ended=1")
        rows = (listed or {}).get("ended") or []
        return {str(r.get("id")): r for r in rows if isinstance(r, dict) and r.get("id")}

    async def gone_reason(self, sandbox_id: str) -> str | None:
        """Why the free tier deleted this sandbox, in Prized's words, or None if it didn't."""
        row = (await self.ended()).get(sandbox_id)
        if row is None:
            return None
        limit = row.get("limitMin")
        said = str(row.get("message") or "").strip()
        head = (
            f"deleted at the free tier's {limit}-minute run limit" if limit else "deleted at the free tier's run limit"
        )
        return f"{head}, memory and files with it" + (f" ({said})" if said else "")

    async def close(self) -> None:
        if self._own and self._http is not None:
            await self._http.aclose()
