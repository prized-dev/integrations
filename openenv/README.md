# openenv-prized

Run [OpenEnv](https://github.com/openenv/OpenEnv) environments in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=openenv) (Firecracker microVMs), with pause, resume and fork.

```bash
pip install "openenv-prized @ git+https://github.com/prized-dev/integrations#subdirectory=openenv"   # not on PyPI yet
export PRIZED_API_KEY=...   # a workspace key with Sandboxes: Write, from https://prized.dev/onboarding/sandboxes?via=openenv
```

```python
from echo_env import EchoEnv
from openenv_prized import PrizedProvider

env = await EchoEnv.from_docker_image(
    "space:openenv/echo_env", provider=PrizedProvider()
)
async with env:
    await env.reset()
    print(await env.call_tool("echo_message", message="hi"))
# closing the client deletes the sandbox
```

## Sources

`image` (in `start_container(image)`, `from_docker_image(image, ...)` or `PrizedProvider(image=...)`) is one of:

| Source | Runs on | Docker |
|---|---|---|
| `space:<owner>/<name>[@rev]` (a Hugging Face Space) | `prized-base-v1` | no |
| `git+https://<url>[@ref][#subdirectory=<path>]` (public repos) | `prized-base-v1` | no |
| `dir:<local path>` (uploaded, up to 100 MiB packed; no `.git`, `.venv`, caches, `.env` or symlinks) | `prized-base-v1` | no |
| a registry image, e.g. `registry.hf.space/openenv-echo-env:latest` | `prized-docker-v1` (not generally available yet) | yes |

A source is fetched, installed with `uv sync`, and served with `uvicorn` on the `app` its `openenv.yaml` names (else the one its Dockerfile's `CMD` serves, else `server.app:app`), the way OpenEnv's `UVProvider` runs it locally. Change either step with `install_cmd=` or `cmd=`.

Tested live: `space:openenv/echo_env`, `space:openenv/coding_env`, and `git+https://github.com/openenv/OpenEnv#subdirectory=envs/echo_env`.

## How clients reach it

Port 8000 stays private. `start_container()` returns a loopback proxy URL (`http://127.0.0.1:<port>`) that adds the `Prized-Key` header, so any `EnvClient` connects unchanged and nothing is public. Prized's edge strips that header: the environment never sees the key.

`public=True` makes the port public instead and returns its `https://` URL. That needs the workspace owner's key and a paid workspace, and counts toward the workspace's 10 public ports.

## Pause, resume, fork

```python
base = PrizedProvider(image="space:openenv/echo_env")
base.wait_for_ready(base.start_container())

copies = base.fork(8)  # 8 started providers, servers already installed and running
for p in copies:
    env = EchoEnv(base_url=p.base_url, provider=p)  # closing env deletes that copy

base.pause()  # memory and processes kept, compute not billed
# the next request through base.base_url resumes it
```

A copy has the parent's memory, so it holds the parent's OpenEnv sessions, but not its connections: an episode that lives on a client's own WebSocket (OpenEnv's default) can't be reached in a copy. An episode in an HTTP session can: `fork_group` below does that for you. In our live test, a session open on the parent kept working through a fork (the parent pauses briefly while it is copied).

## Branch a group from one live episode (GRPO)

```python
from coding_env import CodeAction, CodingEnv
from openenv_prized import PrizedProvider, fork_group, forkable

env = await CodingEnv.from_docker_image(
    "space:openenv/coding_env", provider=PrizedProvider()
)
async with env:  # closing env deletes the parent's sandbox
    await forkable(env)  # before reset(): the episode goes in a session copies rejoin
    await env.reset()
    ...  # the shared prefix: k steps
    async with fork_group(env, 8) as group:  # one fork(8): 8 clients, each at step k
        results = await asyncio.gather(*(c.step(a) for c, a in zip(group, actions)))
    # the 8 copies are deleted; env is still at step k for the next group
```

Each copy holds the parent's episode at the parent's step (memory, processes and files; nothing is replayed), and:

- **gets its own RNG seed** (`reseed=True`) where the environment exposes one. The seed is in `group.branches[i].seed` (`seeds=` sets them). `reseed=False` leaves every copy on the parent's RNG state: common random numbers.
- **reports its own episode id** from `state()` (`relabel=True`): `<parent id>/<group id>-b<i>`, with `parent_episode_id`, `branch_index`, `branch_step` and the server's own `server_episode_id`. The server isn't told: OpenEnv has no call that renames a running episode.

| Environment | Copied with the fork | `reseed=True` does |
|---|---|---|
| `coding_env` | the REPL's `random` module | runs `random.seed(s)` as one env step, so copies start at step k+1. Generators the episode built itself (`random.Random(7)`) keep the copied state |
| `browsergym_env` | the page's seeded `Math.random`, the task's NumPy RNG, Chromium | nothing (`"unsupported"`): BrowserGym's actions can't run page JavaScript. MiniWoB draws its task at `reset()` |
| `echo_env` (MCP clients) | no randomness | nothing (`"no-rng"`). MCP clients already keep their episode in an HTTP session, so `forkable` has nothing to move |
| others | unknown | warns once. Pass `reseed=<async (client, seed) -> None>`, or `register_reseeder()` |

[`examples/grpo_fork_group.py`](examples/grpo_fork_group.py) runs a whole loop on a CPU: a softmax policy over five candidate code cells, groups of 8 forked from one live coding_env state, group-relative advantages. Clients must be async (`await`); tested with `coding_env`. With `--no-reseed`, every `random_search` branch in every group drew the same numbers and scored the same 0.7368, so one lucky draw was rewarded every iteration: that is what common random numbers mean here.

### When fork wins

Measured on 11-09 in Oregon with 2 vCPU/4 GB environments, env time only (no model in the loop), 3 runs per cell:

- **Fork wins while the environment's memory is small.** A fork took about 0.9 s plus 1.75 s per GiB in use on the server, and results came back about 2.2 s later per GiB end to end.
  - `browsergym_env` at 0.9 GiB, 8 steps in: `fork(8)` had results back in 6.2 s, against 9.1 s for replaying on 8 warm environments. At 1.9 GiB: 9.4 s against 9.1 s.
  - `coding_env` with a 0.09 s prefix: a warm pool replays in 0.16 s, against 2.2-4.7 s for a fork. Fork pays off once the prefix costs more than the fork itself.
- **Against N fresh environments plus replay, fork was 3-7× faster and 4-13× cheaper.** Each fresh environment installs from source here.
- **Seeded replay is exact; unseeded replay isn't.** Seeded MiniWoB replays matched the parent's page 72 of 72 times. After OpenEnv's default `reset()`, which passes no seed, 48 of 48 replays diverged without an error. Fork copies matched the parent every time (132 of 132 in BrowserGym).
- **This helper, live on `coding_env` (one host, through a test relay):** `fork_group(env, 16)` had all 16 clients attached, checked and reseeded in 4.6 and 5.3 s (2 runs), 3.1 s of it the fork call. The example's groups of 8 were ready in a median 2.85 s (12 groups in 3 runs, 2.6-7.1 s). The two slow ones were slow forks on the server: 3.8 and 5.9 s, against a median of 1.4 s for `fork(8)`.

A copy also keeps the parent's episode clock: MiniWoB ends an episode 1,000 s after `reset()`, counting time before the fork.

`coding_env`'s `random` module is the server's too, and the server's WebSocket keepalive draws from it: uvicorn sends a ping every 20 s on each open connection, with a payload from `random.getrandbits(32)`. After `random.seed(5)` and 45 s idle on an open socket, the next draw was always `Random(5)`'s third value, not its first (3 of 3 runs); with the socket closed it was the first (2 of 2). So a seeded sequence there shifts with wall-clock time, in replays and in fork copies alike.

### Rate limits

Each copy is a new sandbox, so its port is opened afresh at Prized's edge. The edge admits about 2 such opens per second per API key after a burst of 30, and 8 in flight; past that it answers `429` with `Retry-After`. `fork_group` opens at most 8 at a time per process (`max_concurrent_opens`) and waits as `Retry-After` says, doubling from 0.5 s up to 8 s. A throttled group waits instead of failing: each copy's `ready_timeout_s` (120 s) starts when it gets one of the 8 slots. Sustained, plan on about 2 copies a second per key. In 6 back-to-back groups of 16 (96 opens in about 45 s), two groups got 1 and 9 rate-limited answers, and their slowest copy was ready after 1.9 s and 5.4 s instead of about 1 s.

## Limits (today)

- The sandbox is deleted after 60 minutes of running (`max_runtime_min`) or 60 minutes paused (`auto_delete_min`) if nothing stops it first. Auto-pause is off: port traffic doesn't count as activity, so it could pause a live episode.
- Open egress; no egress allowlist yet.
- `prized-docker-v1` (registry images) is not live yet: it answers `409 template_inactive` until it launches.
- us-west-2 (Oregon), linux/amd64, no GPU.

## Tests

```bash
pip install -e '.[test]'
pytest -q
```

The tests use a fake SDK and a loopback stand-in for Prized's edge; no account needed. `tests/test_fork_group.py` runs real OpenEnv servers behind a fake Prized API whose fork copies the servers' sessions. `tests/test_live.py` runs `fork_group` on Prized with `coding_env` when `PRIZED_LIVE=1` and `PRIZED_API_KEY` are set.
