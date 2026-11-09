# SPDX-License-Identifier: Apache-2.0

"""Branch a group of rollouts from one live OpenEnv episode with Prized fork(N).

GRPO-style training compares G rollouts that start from the same state. With
``fork_group`` the group starts from a live, mid-episode environment: one
``fork(N)`` call copies the environment's sandbox (memory, processes, disk)
into N new sandboxes, and each copy gets a stock OpenEnv client attached to
the same episode::

    env = await forkable(await CodingEnv.from_docker_image(
        "space:openenv/coding_env", provider=PrizedProvider()))
    await env.reset()
    ...                                    # k steps: the shared prefix
    async with fork_group(env, 8) as group:
        results = await asyncio.gather(
            *(client.step(action) for client, action in zip(group, actions)))
    # the 8 copies are deleted; env is still at step k for the next group

How a copy keeps the episode: OpenEnv keeps an episode in a server-side
session. By default that session belongs to the client's WebSocket and ends
when it closes, so it cannot move to a copy. :func:`forkable` puts the episode
in an HTTP session instead (``openenv/session/create``), which clients join
with ``/ws?session_id=<id>``, so each copy's client rejoins the copied session.

What each copy gets on top of the copied state:

- **A reseeded RNG** (``reseed=True``), where the environment exposes one.
  A copy carries the parent's RNG state, so without it any randomness the
  environment draws after the branch point repeats across the group (common
  random numbers, which is sometimes what you want: ``reseed=False``). See
  :data:`RESEEDERS` for what is possible per environment.
- **Its own episode id** (``relabel=True``). Copies report the parent's
  ``episode_id``; the helper relabels the state each copy's client returns
  as ``<parent id>/<group id>-b<i>``, with ``parent_episode_id``,
  ``branch_index``, ``branch_step`` and the server's own
  ``server_episode_id`` alongside. The server is not told: OpenEnv has no
  call that renames a running episode.

Prized's edge admits at most 8 first opens of sandbox ports in flight per
API key (and about 2 a second after a burst of 30); more get ``429`` with
``Retry-After``. A copy is a new sandbox, so each copy's port is a first
open: the helper opens at most ``max_concurrent_opens`` (default 8) at a
time per event loop and backs off on 429 as the edge asks.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import numbers
import operator
import secrets
import time
import typing
import uuid
import warnings
import weakref
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Union
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

#: Prized's edge admits this many first opens of sandbox ports in flight per
#: API key; the next one gets 429 with Retry-After: 1.
MAX_CONCURRENT_OPENS = 8

_BACKOFF_START_S = 0.5
_BACKOFF_MAX_S = 8.0

_UNSET = object()
_PINGS_OFF = object()

# Seams for tests.
_sleep = asyncio.sleep
_monotonic = time.monotonic

Reseeder = Callable[[Any, int], Awaitable[Any]]


# ---------------------------------------------------------------------------
# Reseeding, per environment
# ---------------------------------------------------------------------------
class _Status:
    """A registry entry that is not a reseeder: why this env is not reseeded."""

    def __init__(self, status: str, reason: str):
        self.status = status
        self.reason = reason

    def __repr__(self) -> str:
        return f"<{self.status}: {self.reason}>"


#: The environment draws no randomness: nothing to reseed.
NO_RNG = _Status("no-rng", "the environment draws no randomness")


def unsupported(reason: str) -> _Status:
    """A registry entry for an environment whose RNG the API cannot reach."""
    return _Status("unsupported", reason)


def _action_class(client: Any) -> Any:
    """The Action class a client's ``step()`` takes, from ``EnvClient[A, O, S]``."""
    for cls in type(client).__mro__:
        for base in getattr(cls, "__orig_bases__", ()):
            args = typing.get_args(base)
            if args and isinstance(args[0], type):
                return args[0]
    raise TypeError(f"cannot tell which Action class {type(client).__name__} takes")


async def reseed_python_random(client: Any, seed: int) -> None:
    """Reseed Python's ``random`` module inside a code-executing environment.

    One env step runs ``import random; random.seed(<seed>)`` in the
    environment's interpreter, so the copy's ``step_count`` is one higher
    than the parent's. It covers code that uses the ``random`` module (the
    process-wide generator); generators the environment's code created
    itself, such as ``random.Random(7)`` or NumPy's, keep their copied state.
    """
    action_cls = _action_class(client)
    result = await client.step(
        action_cls(code=f"import random\nrandom.seed({int(seed)})\n")
    )
    observation = result.observation
    if getattr(observation, "exit_code", 0) != 0:
        stderr = str(getattr(observation, "stderr", ""))[:300]
        raise RuntimeError(f"reseed step failed in the environment: {stderr}")


#: What reseeding means per environment client, by class name. An entry is a
#: reseeder ``async (client, seed) -> None``, :data:`NO_RNG`, or
#: :func:`unsupported`. Add your own with :func:`register_reseeder`, or pass
#: ``reseed=<callable>`` to :func:`fork_group`.
RESEEDERS: Dict[str, Union[Reseeder, _Status]] = {
    # OpenEnv's coding_env (a smolagents Python REPL): its code shares the
    # server process's `random` module, which a code step can seed.
    "CodingEnv": reseed_python_random,
    # echo_env: echoes its input.
    "EchoEnv": NO_RNG,
    # browsergym_env: the page's seeded Math.random, the task's NumPy RNG
    # and Chromium's own state are copied, and BrowserGym's actions cannot
    # run page JavaScript. MiniWoB tasks draw their randomness at reset().
    "BrowserGymEnv": unsupported(
        "BrowserGym's actions cannot reach the page's Math.random or the "
        "task's NumPy generator"
    ),
}


def register_reseeder(
    client: Union[type, str], entry: Union[Reseeder, _Status]
) -> None:
    """Say how to reseed copies of an environment (by client class or name)."""
    name = client if isinstance(client, str) else client.__name__
    RESEEDERS[name] = entry


def _registered(client: Any) -> Optional[Union[Reseeder, _Status]]:
    for cls in type(client).__mro__:
        if cls.__name__ in RESEEDERS:
            return RESEEDERS[cls.__name__]
    return None


_warned: set = set()


def _seeds(seeds: Union[None, int, Sequence[int]], n: int) -> List[int]:
    if seeds is None:
        return [secrets.randbits(31) for _ in range(n)]
    if isinstance(seeds, numbers.Integral) and not isinstance(seeds, bool):
        return [int(seeds) + i for i in range(n)]
    seeds = list(seeds)
    if len(seeds) != n:
        raise ValueError(f"seeds has {len(seeds)} entries for {n} copies")
    return [operator.index(s) for s in seeds]


# ---------------------------------------------------------------------------
# Sessions: an episode that a copy can rejoin
# ---------------------------------------------------------------------------
def _is_mcp(env: Any) -> bool:
    """An MCP client in production mode keeps its own HTTP session."""
    return bool(getattr(env, "use_production_mode", False)) and hasattr(
        env, "_production_session_id"
    )


def session_id_of(env: Any) -> Optional[str]:
    """The OpenEnv session a client's episodes live in, if any.

    For an MCP client (``MCPToolClient``, e.g. echo_env) that is the HTTP
    session it creates on connect; for other clients, the session their
    WebSocket joins with ``/ws?session_id=`` (set by :func:`forkable`).
    """
    if _is_mcp(env):
        return env._production_session_id
    ws_url = getattr(env, "_ws_url", None)
    if not ws_url:
        return None
    return (parse_qs(urlsplit(ws_url).query).get("session_id") or [None])[0]


def _with_session(ws_url: str, session_id: str) -> str:
    parts = urlsplit(ws_url)
    query = parse_qs(parts.query)
    query["session_id"] = [session_id]
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def _http_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    return urlunsplit(parts._replace(scheme=scheme)).rstrip("/")


def _is_loopback_url(url: str) -> bool:
    from .provider import _is_loopback

    return _is_loopback(urlsplit(url).hostname)


def _no_proxy(url: str) -> Optional[Dict[str, None]]:
    return {"http": None, "https": None} if _is_loopback_url(url) else None


def _create_session(base_url: str, headers: Optional[Dict[str, str]]) -> str:
    import requests

    url = f"{_http_url(base_url)}/mcp"
    body = {"jsonrpc": "2.0", "id": 1, "method": "openenv/session/create", "params": {}}
    response = requests.post(
        url, json=body, headers=headers or None, timeout=60, proxies=_no_proxy(url)
    )
    response.raise_for_status()
    reply = response.json()
    error = reply.get("error")
    if error:
        cls = (
            _AtCapacity if "max_sessions" in (error.get("data") or {}) else RuntimeError
        )
        raise cls(
            f"the environment refused a new session: {error.get('message', error)}"
        )
    return reply["result"]["session_id"]


class _AtCapacity(RuntimeError):
    pass


async def forkable(env: Any) -> Any:
    """Move ``env``'s episodes into an OpenEnv HTTP session, so forks keep them.

    Call it once, before ``reset()``: an episode already running on the
    client's own WebSocket ends when that socket closes and cannot be moved.
    Returns ``env``, which works as before (it reconnects on its next call).
    Safe to call twice. MCP clients already keep their episodes in an HTTP
    session; for them it only makes sure that session exists.
    """
    if session_id_of(env):
        return env
    if _is_mcp(env):
        await env.connect()  # creates the client's HTTP session
        return env
    if getattr(env, "_episode_ws", None) is not None:
        raise RuntimeError(
            "env has an episode running on its own WebSocket, which ends when the "
            "socket closes and cannot be forked. Call forkable(env) before reset()."
        )
    if env.base_url is None:
        await env.connect()  # a provider-owned client starts its server here
    # Drop the WebSocket's own (episode-less) session first: most environments
    # allow one session at a time, and the server frees it just after the close.
    await env.disconnect()
    deadline = _monotonic() + 10.0
    while True:
        try:
            session_id = await asyncio.to_thread(
                _create_session, env.base_url, getattr(env, "_headers", None)
            )
            break
        except _AtCapacity:
            if _monotonic() > deadline:
                raise
            await _sleep(0.05)
    env._ws_url = _with_session(env._ws_url, session_id)
    return env


async def _release_session(env: Any, timeout_s: float) -> Dict[str, Any]:
    """Detach ``env`` from its session and wait until the server has let go.

    A session can have one WebSocket attached, and a copy forked while the
    parent's socket was attached would refuse every client. The server
    releases the session before it closes the socket, so attaching once more,
    sending ``close`` and seeing the server close proves the release. Returns
    the parent's state as the server reported it.
    """
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    await env.disconnect()
    url = _with_session(env._ws_url, session_id_of(env))
    kwargs: Dict[str, Any] = {"open_timeout": timeout_s, "max_size": None}
    if getattr(env, "_headers", None):
        kwargs["additional_headers"] = env._headers
    if _is_loopback_url(url):
        kwargs["proxy"] = None
    deadline = _monotonic() + timeout_s
    while True:
        async with connect(url, **kwargs) as ws:
            await ws.send(json.dumps({"type": "state"}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout_s))
            if reply.get("type") != "error":
                await ws.send(json.dumps({"type": "close"}))
                try:
                    while True:
                        await asyncio.wait_for(ws.recv(), timeout_s)
                except ConnectionClosed:
                    pass
                return reply.get("data") or {}
        message = str((reply.get("data") or {}).get("message", reply))
        if (
            "already has an attached WebSocket" not in message
            or _monotonic() > deadline
        ):
            raise RuntimeError(f"cannot fork this episode: {message}")
        await _sleep(0.05)  # the parent's own close is still in flight


# ---------------------------------------------------------------------------
# Opening a copy: its port (rate limited at the edge), then its client
# ---------------------------------------------------------------------------
_open_slots: "weakref.WeakKeyDictionary[Any, Dict[int, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)


def _slots(limit: int) -> asyncio.Semaphore:
    """One semaphore per event loop and limit: groups that run at once share it."""
    try:
        per_loop = _open_slots.setdefault(asyncio.get_running_loop(), {})
    except TypeError:  # a loop that takes no weak references
        return asyncio.Semaphore(limit)
    if limit not in per_loop:
        per_loop[limit] = asyncio.Semaphore(limit)
    return per_loop[limit]


_env_locks: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _env_lock(env: Any) -> asyncio.Lock:
    """Serializes fork_group calls on one parent (release through fork)."""
    try:
        lock = _env_locks.get(env)
        if lock is None:
            lock = _env_locks[env] = asyncio.Lock()
        return lock
    except TypeError:  # a client that takes no weak references
        return asyncio.Lock()


def _retry_after(response: Any) -> float:
    try:
        return max(float(response.headers.get("Retry-After", "1")), 0.0)
    except ValueError:
        return 1.0


async def _wait_ready(base_url: str, deadline: float) -> int:
    """Poll a copy's ``/health`` until 200; return how many 429s it took.

    The copy's server is already running (its memory was copied), so this is
    mostly the edge opening the copy's port. On 429 it waits as long as the
    edge's Retry-After says, doubling from 0.5 s up to 8 s, plus jitter.
    """
    import requests

    url = f"{_http_url(base_url)}/health"
    proxies = _no_proxy(url)
    rate_limited = 0
    not_found = 0
    backoff = _BACKOFF_START_S
    while True:
        status, delay = None, 0.25
        try:
            response = await asyncio.to_thread(
                requests.get, url, timeout=10, proxies=proxies
            )
            status = response.status_code
            if status == 200:
                return rate_limited
            if status == 404:
                not_found += 1
            if status in (401, 402, 403) or not_found >= 2:
                # The edge's answer won't change by asking again (and repeated
                # 404s cost the key its standing at the edge).
                raise RuntimeError(
                    f"Prized refused a fork copy's port: {status} {response.text[:300]}"
                )
            if status == 429:
                rate_limited += 1
                delay = max(_retry_after(response), backoff)
                delay += secrets.randbelow(250) / 1000
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
        except requests.RequestException:
            pass
        if _monotonic() + delay > deadline:
            raise TimeoutError(
                f"a fork copy did not answer /health in time (last status {status}, "
                f"{rate_limited} rate-limited answers)"
            )
        await _sleep(delay)


def _client_kwargs(env: Any, base_url: str) -> Dict[str, Any]:
    """Constructor options for a copy's client: the parent's, minus its provider."""
    candidates = {
        "base_url": base_url,
        "connect_timeout_s": getattr(env, "_connect_timeout", None),
        "message_timeout_s": getattr(env, "_message_timeout", None),
        "max_message_size_mb": (
            env._max_message_size / (1024 * 1024)
            if getattr(env, "_max_message_size", None)
            else None
        ),
        "websocket_ping_interval_s": getattr(env, "_websocket_ping_interval_s", _UNSET),
        "websocket_ping_timeout_s": getattr(env, "_websocket_ping_timeout_s", _UNSET),
        "mode": getattr(env, "_mode", None),
        "headers": getattr(env, "_headers", None),
    }
    # None is a setting for the pings (off), not a missing value.
    for name in ("websocket_ping_interval_s", "websocket_ping_timeout_s"):
        if candidates[name] is None:
            candidates[name] = _PINGS_OFF
    parameters = inspect.signature(type(env)).parameters
    any_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )
    return {
        name: (None if value is _PINGS_OFF else value)
        for name, value in candidates.items()
        if value is not None
        and value is not _UNSET
        and (any_kwargs or name in parameters)
    }


async def _attach(env: Any, base_url: str, session_id: str, deadline: float) -> Any:
    """A client of ``env``'s class on a copy, joined to the copied session."""
    client = type(env)(**_client_kwargs(env, base_url))
    if _is_mcp(client):
        client._production_session_id = session_id
    else:
        client._ws_url = _with_session(client._ws_url, session_id)
    backoff = _BACKOFF_START_S
    while True:
        try:
            await client.connect()
            return client
        except ConnectionError:
            if _is_mcp(client):  # its failed connect forgot the session
                client._production_session_id = session_id
            # The edge can refuse the upgrade while it opens the port; the
            # client holds no provider, so a failed connect deletes nothing.
            if _monotonic() + backoff > deadline:
                raise
            await _sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)


def _relabel(client: Any, branch: "Branch") -> None:
    """Make the client's ``state()`` report the branch's own episode id."""
    parse = client._parse_state

    def relabeled(payload: Dict[str, Any]) -> Any:
        state = parse(payload)
        update = {
            "episode_id": branch.episode_id,
            "parent_episode_id": branch.parent_episode_id,
            "branch_index": branch.index,
            "branch_step": branch.branch_step,
            "server_episode_id": getattr(state, "episode_id", None),
        }
        if isinstance(state, dict):
            update["server_episode_id"] = state.get("episode_id")
            return {**state, **update}
        return state.model_copy(update=update)

    client._parse_state = relabeled


# ---------------------------------------------------------------------------
# The group
# ---------------------------------------------------------------------------
@dataclass
class Branch:
    """One copy: its client, the provider that owns its sandbox, and labels."""

    index: int
    client: Any
    provider: Any
    episode_id: str
    parent_episode_id: Optional[str]
    branch_step: Optional[int]
    seed: Optional[int]
    #: "reseeded", "no-rng", "unsupported", "unknown" (no reseeder
    #: registered for this env) or "off" (reseed=False).
    reseed: str
    timings: Dict[str, float] = field(default_factory=dict)
    #: 429 answers from the edge while this copy's port opened.
    rate_limited: int = 0
    _closed: bool = False

    @property
    def sandbox_id(self) -> Optional[str]:
        return getattr(self.provider, "sandbox_id", None)

    async def aclose(self) -> None:
        """Disconnect the client and delete this copy's sandbox. Idempotent."""
        if self._closed:
            return
        try:
            if self.client is not None:
                await self.client.close()
        except Exception:
            pass  # the socket goes away with the sandbox anyway
        finally:
            # Shielded: a cancelled caller must not leave the sandbox running.
            await asyncio.shield(asyncio.to_thread(self.provider.stop_container))
        self._closed = True


class ForkGroup:
    """N clients, each on its own fork copy of one live episode.

    Use it as ``async with fork_group(env, n) as group:`` (the copies are
    deleted on exit), or ``group = await fork_group(env, n)`` and then
    ``await group.aclose()``. Iterating, indexing and ``len()`` give the
    clients; :attr:`branches` has each copy's episode id, seed and timings.
    """

    def __init__(
        self,
        env: Any,
        n: int,
        *,
        reseed: Union[bool, Reseeder] = True,
        relabel: bool = True,
        seeds: Union[None, int, Sequence[int]] = None,
        provider: Any = None,
        labels: Optional[Dict[str, str]] = None,
        ready_timeout_s: float = 120.0,
        max_concurrent_opens: int = MAX_CONCURRENT_OPENS,
    ):
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError("n must be a positive int")
        if isinstance(max_concurrent_opens, bool) or max_concurrent_opens < 1:
            raise ValueError("max_concurrent_opens must be a positive int")
        provider = provider if provider is not None else getattr(env, "_provider", None)
        if provider is None or not callable(getattr(provider, "fork", None)):
            raise TypeError(
                "fork_group needs the PrizedProvider running env's server: create env "
                "with provider=PrizedProvider(...), or pass provider="
            )
        self.env = env
        self.n = n
        self.provider = provider
        self._reseed = reseed
        self._relabel = relabel
        self._seeds = _seeds(seeds, n) if reseed is not False else [None] * n
        self._labels = dict(labels or {})
        self._ready_timeout_s = ready_timeout_s
        self._max_concurrent_opens = max_concurrent_opens
        #: Names this group's copies (label ``fork_group``) and their episode ids.
        self.group_id = uuid.uuid4().hex[:8]
        self.session_id: Optional[str] = None
        self.parent_episode_id: Optional[str] = None
        #: The parent's ``step_count`` when it was forked.
        self.branch_step: Optional[int] = None
        self.branches: List[Branch] = []
        #: Seconds: detach (parent released), fork (the API call and the
        #: copies' local proxies), open (all copies attached, reseeded) and total.
        self.timings: Dict[str, float] = {}
        self._state = "new"

    # -- results ------------------------------------------------------------
    @property
    def clients(self) -> List[Any]:
        return [branch.client for branch in self.branches]

    @property
    def episode_ids(self) -> List[str]:
        return [branch.episode_id for branch in self.branches]

    @property
    def sandbox_ids(self) -> List[Optional[str]]:
        return [branch.sandbox_id for branch in self.branches]

    @property
    def rate_limited(self) -> int:
        """429 answers from the edge while the copies' ports opened."""
        return sum(branch.rate_limited for branch in self.branches)

    def __len__(self) -> int:
        return len(self.branches)

    def __iter__(self):
        return iter(self.clients)

    def __getitem__(self, index: int) -> Any:
        return self.branches[index].client

    # -- lifecycle -----------------------------------------------------------
    def __await__(self):
        return self._open().__await__()

    async def __aenter__(self) -> "ForkGroup":
        if self._state == "open":  # `async with await fork_group(...)`
            return self
        return await self._open()

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _open(self) -> "ForkGroup":
        if self._state != "new":
            raise RuntimeError("this fork group was already opened")
        self._state = "opening"
        started = _monotonic()
        session_id = session_id_of(self.env)
        if not session_id:
            raise ValueError(
                "env's episode lives on its own WebSocket and would not survive a fork. "
                "Call `await forkable(env)` before reset()."
            )
        self.session_id = session_id
        timeout = self._ready_timeout_s

        # Nothing may attach to the parent's session between its release and
        # the fork, or every copy would hold the session as attached.
        async with _env_lock(self.env):
            state = await _release_session(self.env, min(timeout, 30.0))
            self.parent_episode_id = state.get("episode_id")
            self.branch_step = state.get("step_count")
            self.timings["detach_s"] = round(_monotonic() - started, 3)

            t = _monotonic()
            forking = asyncio.ensure_future(
                asyncio.to_thread(
                    self.provider.fork,
                    self.n,
                    labels={**self._labels, "fork_group": self.group_id},
                )
            )
            try:
                copies = await asyncio.shield(forking)
            except asyncio.CancelledError:
                # The fork call runs on in its thread: delete what it makes.
                done = await asyncio.gather(forking, return_exceptions=True)
                if isinstance(done[0], list):
                    await asyncio.gather(
                        *(asyncio.to_thread(c.stop_container) for c in done[0]),
                        return_exceptions=True,
                    )
                self._state = "closed"
                raise
            self.timings["fork_s"] = round(_monotonic() - t, 3)

        t = _monotonic()
        slots = _slots(self._max_concurrent_opens)
        self.branches = [self._branch(i, copy) for i, copy in enumerate(copies)]
        tasks = [
            asyncio.ensure_future(self._open_branch(branch, slots, timeout))
            for branch in self.branches
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # Stop the other copies' setup before deleting every copy.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            failed = await asyncio.shield(self._close_all(raise_errors=False))
            if failed:
                warnings.warn(
                    f"fork_group could not delete {len(failed)} fork copies "
                    f"({', '.join(map(str, failed))}); find them with "
                    f"`prized sandbox ls --label fork_group={self.group_id}`",
                    RuntimeWarning,
                    stacklevel=2,
                )
            self._state = "closed"
            raise
        self.timings["open_s"] = round(_monotonic() - t, 3)
        self.timings["total_s"] = round(_monotonic() - started, 3)
        self._state = "open"
        return self

    def _branch(self, index: int, copy: Any) -> Branch:
        parent = self.parent_episode_id
        episode_id = f"{self.group_id}-b{index}"
        if parent:
            episode_id = f"{parent}/{episode_id}"
        return Branch(
            index=index,
            client=None,
            provider=copy,
            episode_id=episode_id if self._relabel else parent,
            parent_episode_id=parent,
            branch_step=self.branch_step,
            seed=self._seeds[index],
            reseed="off",
        )

    async def _open_branch(
        self, branch: Branch, slots: asyncio.Semaphore, timeout_s: float
    ) -> None:
        t = _monotonic()
        async with slots:
            # The clock starts once the copy may open its port: with many
            # groups at once, waiting for a slot is not a failure.
            deadline = _monotonic() + timeout_s
            branch.rate_limited = await _wait_ready(branch.provider.base_url, deadline)
            branch.timings["ready_s"] = round(_monotonic() - t, 3)
            branch.client = await _attach(
                self.env, branch.provider.base_url, self.session_id, deadline
            )
        # The copy holds the parent's session at the parent's step.
        state = await branch.client.state()
        if isinstance(state, dict):
            step = state.get("step_count")
        else:
            step = getattr(state, "step_count", None)
        if None not in (step, self.branch_step) and step != self.branch_step:
            raise RuntimeError(
                f"fork copy {branch.index} is at step {step}, "
                f"not the parent's {self.branch_step}"
            )
        branch.timings["attach_s"] = round(_monotonic() - t, 3)
        if self._relabel:
            _relabel(branch.client, branch)
        await self._reseed_branch(branch)
        branch.timings["ready_total_s"] = round(_monotonic() - t, 3)

    async def _reseed_branch(self, branch: Branch) -> None:
        if self._reseed is False:
            branch.reseed = "off"
            return
        entry = self._reseed if callable(self._reseed) else _registered(branch.client)
        if entry is None:
            name = type(branch.client).__name__
            if name not in _warned:
                _warned.add(name)
                warnings.warn(
                    f"fork_group: no reseeder is registered for {name}, so any "
                    "randomness the environment draws after the fork repeats across "
                    "the group. Register one with register_reseeder(), or pass "
                    "reseed=False to say that is intended.",
                    RuntimeWarning,
                    stacklevel=3,
                )
            branch.reseed = "unknown"
            return
        if isinstance(entry, _Status):
            branch.reseed = entry.status
            return
        t = _monotonic()
        await entry(branch.client, branch.seed)
        branch.timings["reseed_s"] = round(_monotonic() - t, 3)
        branch.reseed = "reseeded"

    async def aclose(self) -> None:
        """Disconnect every copy's client and delete every copy. Idempotent.

        The parent is untouched: its client reconnects to its session on its
        next call, still at the step it was forked at.
        """
        if self._state == "closed":
            return
        await self._close_all(raise_errors=True)
        self._state = "closed"

    async def _close_all(self, *, raise_errors: bool) -> List[Optional[str]]:
        """Delete every copy, retrying failures once; return the ids left."""
        results = await asyncio.gather(
            *(branch.aclose() for branch in self.branches), return_exceptions=True
        )
        failed = [
            branch
            for branch, result in zip(self.branches, results)
            if isinstance(result, BaseException)
        ]
        if failed:  # one more try, e.g. after a transient API error
            results = await asyncio.gather(
                *(branch.aclose() for branch in failed), return_exceptions=True
            )
            failed = [
                branch
                for branch, result in zip(failed, results)
                if isinstance(result, BaseException)
            ]
        left = [branch.sandbox_id for branch in failed]
        if left and raise_errors:
            ids = " ".join(map(str, left))
            raise RuntimeError(
                f"could not delete {len(left)} fork copies; aclose() again retries. "
                f"Or `prized sandbox rm -y {ids}` (they carry the label "
                f"fork_group={self.group_id})"
            )
        return left

    def stats(self) -> Dict[str, Any]:
        """A JSON-friendly summary for logs."""
        return {
            "group_id": self.group_id,
            "n": self.n,
            "parent_episode_id": self.parent_episode_id,
            "branch_step": self.branch_step,
            "timings": dict(self.timings),
            "rate_limited": self.rate_limited,
            "branches": [
                {
                    "index": b.index,
                    "sandbox_id": b.sandbox_id,
                    "episode_id": b.episode_id,
                    "seed": b.seed,
                    "reseed": b.reseed,
                    "rate_limited": b.rate_limited,
                    "timings": dict(b.timings),
                }
                for b in self.branches
            ],
        }


def fork_group(
    env: Any,
    n: int,
    *,
    reseed: Union[bool, Reseeder] = True,
    relabel: bool = True,
    seeds: Union[None, int, Sequence[int]] = None,
    provider: Any = None,
    labels: Optional[Dict[str, str]] = None,
    ready_timeout_s: float = 120.0,
    max_concurrent_opens: int = MAX_CONCURRENT_OPENS,
) -> ForkGroup:
    """Fork ``env``'s live episode into ``n`` copies, each with its own client.

    ``env`` is an OpenEnv client made :func:`forkable` before its ``reset()``,
    whose server runs on a :class:`PrizedProvider` (``env``'s own provider, or
    ``provider=``). One ``fork(n)`` call copies the server's sandbox; each
    copy gets a client of ``env``'s class joined to the copied episode, at the
    parent's step. ``env`` itself is detached for the fork and reconnects on
    its next call, at the same step: don't use it until ``fork_group``
    returns. Groups forked from one ``env`` at once take turns.

    Args:
        reseed: ``True`` reseeds each copy's environment RNG where
            :data:`RESEEDERS` knows how (and warns once for an environment it
            does not know); ``False`` leaves every copy with the parent's RNG
            state (common random numbers); a callable
            ``async (client, seed) -> None`` reseeds any environment.
        relabel: give each copy's ``state()`` its own ``episode_id``
            (``<parent>/<group id>-b<i>``) plus ``parent_episode_id``,
            ``branch_index``, ``branch_step`` and ``server_episode_id``.
        seeds: one seed per copy, or a base seed (copy ``i`` gets
            ``seeds + i``); random when omitted. Recorded on each branch.
        labels: extra labels for the copies' sandboxes (each copy also gets
            ``fork_group=<group id>``).
        ready_timeout_s: bound on attaching every copy, 429 waits included.
        max_concurrent_opens: copies whose ports open at once, across every
            group on this event loop (Prized's edge admits 8 in flight per
            API key; other processes using the same key count too).

    Returns a :class:`ForkGroup`: ``await`` it, or use ``async with``.
    """
    return ForkGroup(
        env,
        n,
        reseed=reseed,
        relabel=relabel,
        seeds=seeds,
        provider=provider,
        labels=labels,
        ready_timeout_s=ready_timeout_s,
        max_concurrent_opens=max_concurrent_opens,
    )


__all__ = [
    "Branch",
    "ForkGroup",
    "MAX_CONCURRENT_OPENS",
    "NO_RNG",
    "RESEEDERS",
    "fork_group",
    "forkable",
    "register_reseeder",
    "reseed_python_random",
    "session_id_of",
    "unsupported",
]
