# SPDX-License-Identifier: BSD-3-Clause

"""The loopback proxy, with WebSockets that can move to another sandbox.

``PrizedProvider.rebase()`` swaps the sandbox an environment runs in for a
fork copy of it. A client's open WebSocket must survive that: OpenEnv's
``EnvClient`` treats a dropped episode socket as a lost episode
(``SessionLostError``), even when the session itself lives on in the copy.

So the proxy keeps the client's socket open and moves only its upstream half:

1. **Detach.** Send OpenEnv's ``close`` message upstream and wait for the
   server to close the socket. The server releases the session's attachment
   before it closes, so the session is free when the fork copies it (a
   session copied while attached refuses every new socket). A call already in
   flight is answered first: the server handles one message at a time.
2. The provider forks the sandbox and points the proxy at the copy.
3. **Reattach.** Open the same ``/ws?session_id=...`` on the copy, check the
   session answers, and carry on. Messages the client sent meanwhile wait and
   go to the copy. A client that connects during the move is accepted at once
   and joined to the copy.

A socket that owns its session (OpenEnv's default, no ``session_id``) cannot
move: closing it ends the episode. The provider refuses to rebase while one is
open. HTTP requests are not held: they go to whichever sandbox is the target
when they arrive.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import secrets
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from openenv.core.containers.runtime.hf_sandbox_provider import (
    _LocalAuthProxy,
    _to_ws_url,
)
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

_CLOSE = json.dumps({"type": "close"})
_STATE = json.dumps({"type": "state"})
# Error codes with which OpenEnv's /ws refuses a socket (and closes it). Any
# other error answer to the state probe means the socket is attached.
_REFUSALS = {"SESSION_ERROR", "CAPACITY_REACHED", "FACTORY_ERROR"}
_POLL_S = 0.02


class OwnSessionOpen(RuntimeError):
    """A WebSocket that owns its episode is open: it cannot move to a copy."""


class _Link:
    """A client's WebSocket and the upstream socket it is joined to.

    The proxy's own relay loops use it as their upstream (``send``,
    ``async for``, ``close``), so the upstream underneath can be swapped
    without them noticing. ``upstream`` is None for a client accepted during
    a move, until the move ends.
    """

    def __init__(self, proxy: "_SwitchingProxy", path: str, upstream: Any):
        self.proxy = proxy
        self.path = path  # "/ws?session_id=...", relative to the target
        self.upstream = upstream
        self.ready = asyncio.Event()  # cleared while the upstream moves
        self.moving = upstream is None
        if not self.moving:
            self.ready.set()
        self.lost = False  # the move failed: the client's socket closes
        self.closed = False
        self._send_lock = asyncio.Lock()
        self._later: Optional[asyncio.Future] = None

    @property
    def session_id(self) -> Optional[str]:
        return (parse_qs(urlsplit(self.path).query).get("session_id") or [None])[0]

    # -- what the proxy's relay loops call --------------------------------------
    async def send(self, message: Any) -> None:
        while True:
            await self.ready.wait()
            if self.lost:
                return  # the client's socket is closing; nothing can carry it
            async with self._send_lock:
                if self.ready.is_set():
                    await self.upstream.send(message)
                    return

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        while True:
            await self.ready.wait()  # a client accepted mid-move has no upstream yet
            if self.lost:
                return
            current = self.upstream
            try:
                async for message in current:
                    yield message
            except ConnectionClosed:
                pass
            if self.upstream is not current:
                continue  # already joined to the copy
            if not self.moving:
                return  # the server closed it, so the client's closes too
            # The detach finished: wait for the copy's socket.
            await self.ready.wait()
            if self.lost:
                return

    async def close(self) -> None:
        self.closed = True
        self.proxy._links.discard(self)
        if self._later is not None:
            self._later.cancel()
        if self.upstream is not None:
            await self.upstream.close()

    # -- moving -----------------------------------------------------------------
    async def detach(self) -> None:
        async with self._send_lock:
            if self.closed or self.moving:
                return
            self.moving = True
            self.ready.clear()
            self.proxy._held.append(self)
            try:
                await self.upstream.send(_CLOSE)
            except ConnectionClosed:
                pass
        await self.upstream.wait_closed()

    def give_up(self) -> None:
        self.lost = True
        self.moving = False
        self.ready.set()
        self.proxy._links.discard(self)

    async def rejoin(self, deadline: float) -> Optional[bool]:
        """Join the session at the proxy's current target. True: joined (or
        no longer needed); False: lost; None: a call is still running on the
        old socket, so it rejoins when the server closes that."""
        if self.closed or not self.moving:
            return True
        if self.upstream is not None and self.upstream.state is not State.CLOSED:
            try:  # again, in case a cancelled detach never sent it
                await self.upstream.send(_CLOSE)
            except ConnectionClosed:
                pass
            self._later = asyncio.ensure_future(self._rejoin_later())
            return None
        return await self.attach(deadline)

    async def _rejoin_later(self) -> None:
        await self.upstream.wait_closed()
        while self.proxy._holding:  # another move began: join where it ends
            await asyncio.sleep(_POLL_S)
        await self.attach(asyncio.get_running_loop().time() + 60.0)

    async def attach(self, deadline: float) -> bool:
        """Open the same path on the current target; False if it can't be."""
        if self.closed:
            return True
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    upstream = await asyncio.wait_for(
                        self.proxy._open(
                            self.proxy._ws_base() + self.path, self.proxy._ws_headers()
                        ),
                        max(deadline - loop.time(), 0.1),
                    )
                    break
                except Exception:
                    if loop.time() + 0.5 > deadline:
                        raise
                    await asyncio.sleep(0.5)  # the edge may still be opening the port
            try:
                if self.session_id:  # check the copied session takes the socket
                    await upstream.send(_STATE)
                    reply = json.loads(
                        await asyncio.wait_for(
                            upstream.recv(), max(deadline - loop.time(), 1.0)
                        )
                    )
                    data = reply.get("data") or {}
                    if reply.get("type") == "error" and data.get("code") in _REFUSALS:
                        raise RuntimeError(str(data.get("message")))
            except BaseException:
                await upstream.close()
                raise
        except Exception:
            self.give_up()
            return False
        if self.closed:  # the client left while the copy's socket opened
            await upstream.close()
            return True
        self.upstream = upstream
        self.moving = False
        self.ready.set()
        return True


class _SwitchingProxy(_LocalAuthProxy):
    """``_LocalAuthProxy`` whose target, and open WebSockets, can move."""

    def __init__(self, *, target_url: str, headers: Dict[str, str]):
        super().__init__(target_url=target_url, headers=headers)
        self._links: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._holding = False
        self._connecting = 0
        self._held: List[_Link] = []
        self._old_bases: List[str] = []

    def _ws_base(self) -> str:
        return _to_ws_url(self.target_url)

    def _ws_headers(self) -> Dict[str, str]:
        return {**self.headers, "accept": "*/*"}

    def _relative(self, target: str) -> str:
        """The path and query of a target URL built from this or an earlier base."""
        for base in [self._ws_base(), *reversed(self._old_bases)]:
            if target.startswith(base + "/"):
                return target[len(base) :]
        parts = urlsplit(target)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    # -- on the proxy's event loop --------------------------------------------
    async def _open(self, target: str, headers: Dict[str, str]) -> Any:
        return await super()._connect_upstream_websocket(target, headers)

    async def _connect_upstream_websocket(self, target: str, headers: Dict[str, str]):
        self._loop = asyncio.get_running_loop()
        path = self._relative(target)
        if self._holding:
            # A move is under way: accept the client now, open its upstream
            # where the move ends.
            link = _Link(self, path, None)
            self._links.add(link)
            self._held.append(link)
            return link
        self._connecting += 1
        try:
            upstream = await self._open(self._ws_base() + path, self._ws_headers())
        finally:
            self._connecting -= 1
        link = _Link(self, path, upstream)
        self._links.add(link)
        return link

    async def _detach_all(self, timeout_s: float) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while self._connecting:
            if loop.time() > deadline:
                raise TimeoutError("a WebSocket was still opening")
            await asyncio.sleep(_POLL_S)
        links = [link for link in self._links if not link.closed and not link.moving]
        own = [link for link in links if not link.session_id]
        if own:
            raise OwnSessionOpen(
                f"{len(own)} open WebSocket(s) own their episode, which would end. "
                "Make clients forkable() before reset(), or close them first."
            )
        if not links:
            return 0
        tasks = [asyncio.ensure_future(link.detach()) for link in links]
        _, pending = await asyncio.wait(tasks, timeout=max(deadline - loop.time(), 0))
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if pending:
            raise TimeoutError(
                f"{len(pending)} WebSocket(s) still had a call running after "
                f"{timeout_s}s, so nothing was forked; they rejoin their "
                "session when the call ends"
            )
        return len(links)

    async def _open_port(self, deadline: float) -> None:
        """Open the target's port at the edge with an HTTP request first,
        waiting as the edge's 429 Retry-After says. Best effort."""
        import requests

        from .provider import _is_loopback

        loop = asyncio.get_running_loop()
        url = f"{self.target_url}/health"
        proxies = (
            {"http": None, "https": None}
            if _is_loopback(urlsplit(url).hostname)
            else None
        )
        backoff = 0.5
        while loop.time() < deadline:
            try:
                response = await asyncio.to_thread(
                    requests.get, url, headers=self.headers, timeout=10, proxies=proxies
                )
            except requests.RequestException:
                return
            if response.status_code != 429:
                return
            try:
                delay = max(float(response.headers.get("Retry-After", "1")), backoff)
            except ValueError:
                delay = backoff
            backoff = min(backoff * 2, 8.0)
            await asyncio.sleep(delay + secrets.randbelow(250) / 1000)

    async def _attach_all(self, timeout_s: float) -> Tuple[int, int]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        lost = later = 0
        opened = False
        while True:
            held, self._held = self._held, []
            if not held:
                self._holding = False  # no await between the check and this
                return lost, later
            try:
                if not opened and any(link.session_id for link in held):
                    await self._open_port(deadline)
                    opened = True
                results = await asyncio.gather(
                    *(link.rejoin(deadline) for link in held)
                )
            except asyncio.CancelledError:
                for link in held:
                    if link.moving and link._later is None:
                        link.give_up()
                self._holding = False
                raise
            lost += sum(1 for result in results if result is False)
            later += sum(1 for result in results if result is None)

    # -- from the provider's thread -------------------------------------------
    def _call(self, coroutine: Any, timeout_s: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout_s)
        except concurrent.futures.TimeoutError:
            if future.done():  # the coroutine's own TimeoutError (3.11+: same class)
                raise
            future.cancel()
            raise TimeoutError("the loopback proxy did not finish in time") from None

    def hold(self, timeout_s: float) -> int:
        """Accept new sockets without opening them, and detach the open ones
        from their sessions. Returns how many were detached. Whatever
        happens, call ``release()`` after."""
        self._holding = True
        if self._loop is None:  # no socket has ever been opened
            return 0
        return self._call(self._detach_all(timeout_s), timeout_s + 30)

    def retarget(self, target_url: str, headers: Dict[str, str]) -> None:
        self._old_bases.append(self._ws_base())
        self.target_url = target_url.rstrip("/")
        self.headers = headers

    def release(self, timeout_s: float) -> Tuple[int, int]:
        """Join the held sockets to the current target and open new ones
        normally again. Returns ``(lost, later)``: sockets that could not
        rejoin (their clients' sockets close), and sockets still finishing a
        call, which rejoin when it ends."""
        if self._loop is None:
            self._holding = False
            return 0, 0
        try:
            return self._call(self._attach_all(timeout_s), timeout_s + 30)
        except BaseException:
            self._holding = False  # never leave new sockets waiting
            raise
