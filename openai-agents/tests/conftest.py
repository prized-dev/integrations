from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agents import set_tracing_disabled

from openai_agents_prized import PrizedSandboxClient
from tests.fake_prized import API_KEY, API_ORIGIN, FakeClock, FakePrized

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


set_tracing_disabled(disabled=True)


@pytest.fixture(autouse=True)
def _no_prized_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Offline tests never see a developer's real key or API URL."""
    if request.node.get_closest_marker("live"):
        return
    for name in ("PRIZED_API_KEY", "PRIZED_API_URL"):
        monkeypatch.delenv(name, raising=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live = os.environ.get("PRIZED_LIVE_TESTS") == "1" and bool(os.environ.get("PRIZED_API_KEY"))
    skip = pytest.mark.skip(reason="needs PRIZED_LIVE_TESTS=1 and PRIZED_API_KEY")
    for item in items:
        if item.get_closest_marker("live") and not live:
            item.add_marker(skip)


@pytest.fixture
def fake(tmp_path: Path) -> FakePrized:
    return FakePrized(tmp_path / "sandboxes")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def make_client(
    fake: FakePrized, clock: FakeClock
) -> AsyncIterator[Callable[..., PrizedSandboxClient]]:
    """Build clients against the fake, with the fake clock; closed after the test."""
    made: list[PrizedSandboxClient] = []
    https = []

    def factory(**kwargs: Any) -> PrizedSandboxClient:
        http = fake.client()
        https.append(http)
        client = PrizedSandboxClient(
            api_key=API_KEY, api_url=API_ORIGIN, http_client=http, **kwargs
        )
        client._client.sleep = clock.sleep
        client._client.monotonic = clock.monotonic
        made.append(client)
        return client

    yield factory
    for client in made:
        await client.close()
    for http in https:
        await http.aclose()


class Gate:
    """Holds the first request `matches` accepts until `release` is set.

    `after=True` lets the fake handle it first, so the API has acted and only
    the answer is held, as when it is still on its way back. Otherwise a
    cancelled caller cancels the held request, unless `in_flight=True`: then
    it reaches the fake once released whatever the caller did, as a request
    already on the wire does.
    """

    def __init__(
        self,
        fake: FakePrized,
        matches: Callable[[httpx.Request], bool],
        *,
        after: bool = False,
        in_flight: bool = False,
    ) -> None:
        self.fake = fake
        self.matches = matches
        self.after = after
        self.in_flight = in_flight
        self.armed = True
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.arrivals: list[asyncio.Future[httpx.Response]] = []

    async def _arrive(self, request: httpx.Request) -> httpx.Response:
        await self.release.wait()
        return self.fake.handle(request)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        held = self.armed and self.matches(request)
        if held and self.in_flight:
            self.armed = False
            arrival = asyncio.ensure_future(self._arrive(request))
            self.arrivals.append(arrival)
            self.reached.set()
            return await asyncio.shield(arrival)
        if held:
            self.armed = False
            if self.after:
                response = self.fake.handle(request)
                self.reached.set()
                await self.release.wait()
                return response
            self.reached.set()
            await self.release.wait()
        return self.fake.handle(request)


@pytest.fixture
async def gated(
    fake: FakePrized, clock: FakeClock
) -> AsyncIterator[Callable[..., PrizedSandboxClient]]:
    """Build clients whose requests pass through a `Gate`; closed after the test."""
    made: list[PrizedSandboxClient] = []
    https: list[httpx.AsyncClient] = []

    def factory(gate: Gate, **kwargs: Any) -> PrizedSandboxClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(gate.handler))
        https.append(http)
        client = PrizedSandboxClient(
            api_key=API_KEY, api_url=API_ORIGIN, http_client=http, **kwargs
        )
        client._client.sleep = clock.sleep
        client._client.monotonic = clock.monotonic
        made.append(client)
        return client

    yield factory
    for client in made:
        await client.close()
    for http in https:
        await http.aclose()


async def settle(client: PrizedSandboxClient) -> None:
    """Wait for the cleanup a cancellation left running in the background."""
    pending = list(client._client._background)
    if pending:
        await asyncio.wait(pending, timeout=30)
