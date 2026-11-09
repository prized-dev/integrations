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

## Start environments in seconds: `template="auto"`

```python
provider = PrizedProvider(template="auto")
env = await CodingEnv.from_docker_image("space:openenv/coding_env", provider=provider)
```

The first start of an environment builds a base: it installs and starts the environment, forks it once to drop the install's memory, runs one throwaway `reset()` to warm it, and pauses it. Every later start, in this process or any other on the machine, forks that paused base instead of installing.

Live on `coding_env` (2 vCPU/4 GB, Oregon, through a test relay, 11-09), from `from_docker_image()` to a connected client:

| | start |
|---|---|
| fresh (no template) | 11.9-12.1 s (4 runs) |
| `template="auto"`, first use (builds the base) | 22.0 s |
| `template="auto"`, later uses | 3.0-3.1 s (2 runs; 2.5-2.7 s with `reseed_after_fork=False`) |

A paused base bills storage only, about $0.65 a month at 2 vCPU/4 GB.

For a group, `start_many(n)` forks the base once into `n` copies instead of making `n` separate forks, and returns one started provider per environment:

```python
providers = PrizedProvider(template="auto").start_many(8, "space:openenv/coding_env")
# One provider per environment; closing its client deletes its sandbox.
envs = [CodingEnv(base_url=p.base_url, provider=p) for p in providers]
```

Live on `openapp_env` (OpenApps and BrowserGym with Chromium, from a `dir:` source with its own `install_cmd` and `cmd`; 2 vCPU/4 GB, one host in Oregon, through a test relay, 11-09), 8 environments started at once, until all 8 had answered `/health` and finished their first `reset()`:

| 8 at once | all 8 ready, median of rounds (range) | one env, median |
|---|---|---|
| fresh (no template) | 96.6 s (1 round) | 93.8 s |
| `template="auto"`, 8 `start_container()` calls in parallel | 14.0 s (13.9-14.8 s, 6 rounds) | 13.5 s |
| `start_many(8)` | 6.0 s (5.8-6.6 s, 6 rounds) | 5.9 s |

The base was built once, in 65 s. A group fork is one checkpoint of the base for all 8 copies instead of one per fork. It took 1.6-2.2 s instead of 2.0-5.3 s, and each copy's first `reset()` took 3.2-3.5 s instead of 7.1-8.4 s. On `coding_env` (7 at once, reseeded): 3.7-4.0 s with separate starts, 2.6 s with `start_many(7)`.

If the group fork fails (the base's host has no room for `n` copies, a quota), each environment starts as `start_container()` would, and `last_template["status"]` is `"each"`; a copy that fails to come up is replaced on its own. Every sandbox of a group is labelled `openenv-group=<provider.last_group>`, and `provider.stop_many()` stops a group whose list you lost (a cancelled `asyncio.to_thread`).

- **Where bases are recorded:** `~/.local/state/openenv-prized/templates.json` (or `template_state=`, or `$OPENENV_PRIZED_STATE`), plain JSON with one entry per base: source, revision, shape, sandbox id, when it was built. A base is kept per source and revision, shape, `cmd`, `install_cmd`, env vars (hashed), the settings copies inherit (`max_runtime_min`, `auto_pause_min`, `template_auto_delete_min`) and API key. Pin a revision (`space:owner/name@<sha>`) to get a new base when the environment changes; an unpinned source keeps its first base, and its entry records the commit it was built from.
- **Delete a base:** `provider.delete_template()` deletes it and its entry. Bases are labelled `openenv=template` (not with your `labels`, so cleaning up a run by its labels leaves the shared base alone), so `prized sandbox ls --label openenv=template` finds any whose entry is gone. A base is deleted after 7 days paused (`template_auto_delete_min`; `0` keeps it).
- **If a fork fails** (quota, capacity, a deleted base), the environment starts fresh and the `openenv_prized` logger says why. A base that no longer exists or can't be forked again is deleted and forgotten, and the next start builds a new one. Every copy lands on the base's host, so a large burst of starts can run out of room there (`fork_capacity`) and fall back to fresh starts.
- **Random numbers:** every copy starts with the base's memory, so the server's random generators would draw the same numbers in every environment. With `reseed_after_fork=True` (the default), the provider starts the server through a small launcher and, in each copy, signals it to reseed Python's `random` module and NumPy's global generator from `os.urandom`. Generators your environment holds itself (`self.rng = np.random.default_rng()`): define `prized_reseed(seed)` in the app's module (e.g. `server/app.py`) and reseed them there. Live on `coding_env`, two copies without reseeding drew the same first number; three reseeded copies drew three different ones. With your own `cmd=` there is no launcher, and a warning says so.

Also inherited from the base: its clock (`time.monotonic()` carries on from the base, and a base hot-paused for minutes can make it jump, so timeouts may fire early), its labels (the provider relabels copies `openenv=server` plus your `labels`) and its settings (`max_runtime_min`, `auto_pause_min`; `auto_delete_min` is the base's 7 days). Outbound connections the server opened before the base was paused are dropped. Each copy counts its full disk (10 GB) toward the workspace's retained-disk quota. Sources only (`space:`, `git+`, `dir:`); `fork_base` is implied.

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
    "space:openenv/coding_env", provider=PrizedProvider(fork_base=True)
)
async with env:  # closing env deletes the parent's sandbox
    await forkable(env)  # before reset(): the episode goes in a session copies rejoin
    await env.reset()
    ...  # the shared prefix: k steps
    async with fork_group(env, 8) as group:  # one fork(8): 8 clients, each at step k+1
        results = await asyncio.gather(*(c.step(a) for c, a in zip(group, actions)))
    # the 8 copies are deleted; env is still at step k for the next group
```

Each copy holds the parent's episode at the parent's step k (memory, processes and files; nothing is replayed). Its client starts at k, or at k+1 where reseeding takes an env step, as `coding_env`'s does (see the table below). Each copy also:

- **gets its own RNG seed** (`reseed=True`) where the environment exposes one. The seed is in `group.branches[i].seed` (`seeds=` sets them). `reseed=False` leaves every copy on the parent's RNG state: common random numbers.
- **reports its own episode id** from `state()` (`relabel=True`): `<parent id>/<group id>-b<i>`, with `parent_episode_id`, `branch_index`, `branch_step` and the server's own `server_episode_id`. The server isn't told: OpenEnv has no call that renames a running episode.

| Environment | Copied with the fork | `reseed=True` does |
|---|---|---|
| `coding_env` | the REPL's `random` module | runs `random.seed(s)` as one env step, so each copy's client starts at step k+1, not k (`branch_step` stays k). Generators the episode built itself (`random.Random(7)`) keep the copied state |
| `browsergym_env` | the page's seeded `Math.random`, the task's NumPy RNG, Chromium | nothing (`"unsupported"`): BrowserGym's actions can't run page JavaScript. MiniWoB draws its task at `reset()` |
| `echo_env` (MCP clients) | no randomness | nothing (`"no-rng"`). MCP clients already keep their episode in an HTTP session, so `forkable` has nothing to move |
| others | unknown | warns once. Pass `reseed=<async (client, seed) -> None>`, or `register_reseeder()` |

[`examples/grpo_fork_group.py`](examples/grpo_fork_group.py) runs a whole loop on a CPU: a softmax policy over five candidate code cells, groups of 8 forked from one live coding_env state, group-relative advantages. Clients must be async (`await`); tested with `coding_env`. With `--no-reseed`, every `random_search` branch in every group drew the same numbers and scored the same 0.7368, so one lucky draw was rewarded every iteration: that is what common random numbers mean here.

### Fork from a base: `fork_base`

```python
provider = PrizedProvider(fork_base=True)  # or "install"; default None
```

With `fork_base`, `start_container()` forks the new sandbox once, deletes the original and runs the environment in the copy. Every later fork then copies less. A fork copies the memory a sandbox has touched since it started: for the sandbox that ran the install, that's all of it, while a copy has touched only what the server used since.

- `True` or `"ready"`: fork once the server answers `/health`, checked inside the sandbox (`wait_timeout`, default 120 s).
- `"install"`: fork after the install, then start the server in the copy. Use it if your server opens connections at start that a copy couldn't keep.

**Use it for environments you fork** (`fork()`, `fork_group`): a GRPO trainer forks hundreds of groups from one environment. Leave it off for environments you never fork, which would pay its cost for nothing.

**What it saves.** On OpenEnv's `openapp_env` (a shop and a bank on SQLite, 2 vCPU/4 GB, Oregon, one host, 3 runs per cell; the same steps scripted by hand, before this option existed), from the fork call until every copy had finished its first action:

| | `fork(4)` | `fork(8)` | parent paused per fork |
|---|---|---|---|
| no base: fork the sandbox that ran the install | 12.1 s | 11.5 s | 4.4-5.1 s |
| `fork_base="install"` | 6.7 s | 8.4 s | 1.3 s |
| `fork_base="ready"` | 6.3 s | 7.8 s | 0.9 s |
| N warm environments, SQL restore + replay | 13.2 s | 13.4 s | - |

Copies of a base also ran their first steps faster: their first probe took 0.8 s instead of 1.7-1.9 s. Copies matched their parent exactly in every arm, page and database rows (148 of 148). Over 10 episodes on one base, forks stayed fast (one slow fork in 10).

**On a small environment it makes no difference.** Installing `coding_env` leaves little memory touched, so its forks were already fast. Live on 11-09 (3 runs each way, one host), `fork(8)` took a median 1.4 s on Prized's side with a base and 1.3 s without, 6 forks each (outliers both ways: one at 5.5 s with a base, two at 11 and 38 s without). The base added about 2.3 s to the start. If `fork_group`'s `timings["fork_s"]` is well over 2 s without a base, turn it on.

**What it costs:** one extra `fork(1)`, and a slower first episode while the copy pages its memory in. On `openapp_env` that was 12 s (`"ready"`) to 14 s (`"install"`) more per environment, once, and under $0.001. It paid back after 3-5 groups.

**`provider.rebase()`** does the same at any time: `fork(1)`, delete the original, carry on in the copy. Sessions are kept, and through the default loopback proxy the base URL stays the same and open WebSockets stay open: each is detached from its session (a call in flight finishes first, up to `timeout_s`, 60 s), then joined to the same session in the copy. WebSocket messages sent meanwhile wait; HTTP requests (MCP tool calls) don't, so make none while it runs. It refuses while a WebSocket that owns its episode is open (make clients `forkable` first). `provider.last_rebase` has the timings. Live on `coding_env`, with the client's socket open mid-episode, 6 of 6 rebases kept the socket and the episode, in 1.2-7.4 s (median 2.1 s), 0.2-0.4 s of it rejoining the socket. Call it if a long-lived environment's forks grow slow again; on `openapp_env` they didn't over 10 episodes. A copy gets a fresh `max_runtime_min` window.

### When fork wins

Measured on 11-09 in Oregon with 2 vCPU/4 GB environments, env time only (no model in the loop), 3 runs per cell:

- **Fork wins while the environment's memory is small.** A fork took about 0.9 s plus 1.75 s per GiB in use on the server, and results came back about 2.2 s later per GiB end to end.
  - `browsergym_env` at 0.9 GiB, 8 steps in: `fork(8)` had results back in 6.2 s, against 9.1 s for replaying on 8 warm environments. At 1.9 GiB: 9.4 s against 9.1 s.
  - `coding_env` with a 0.09 s prefix: a warm pool replays in 0.16 s, against 2.2-4.7 s for a fork. Fork pays off once the prefix costs more than the fork itself.
- **Against N fresh environments plus replay, fork was 3-7× faster and 4-13× cheaper.** Each fresh environment installs from source here.
- **Seeded replay is exact; unseeded replay isn't.** Seeded MiniWoB replays matched the parent's page 72 of 72 times. After OpenEnv's default `reset()`, which passes no seed, 48 of 48 replays diverged without an error. Fork copies matched the parent every time (132 of 132 in BrowserGym).
- **This helper, live on `coding_env` (one host, through a test relay):** `fork_group(env, 16)` had all 16 clients attached, checked and reseeded in 4.6 and 5.3 s (2 runs), 3.1 s of it the fork call. The example's groups of 8 were ready in a median 2.85 s (12 groups in 3 runs, 2.6-7.1 s). The two slow ones were slow forks on the server: 3.8 and 5.9 s, against a median of 1.4 s for `fork(8)`.

A copy also keeps the parent's episode clock: MiniWoB ends an episode 1,000 s after `reset()`, counting time before the fork.

`coding_env`'s `random` module is the server's too, and the server's WebSocket keepalive draws from it: uvicorn sends a ping every 20 s on each open connection, with a payload from `random.getrandbits(32)`. After `random.seed(5)` and 45 s idle on an open socket, the next draw was always `Random(5)`'s second value, not its first (3 of 3 runs); with the socket closed it was the first (2 of 2). So a seeded sequence there shifts with wall-clock time, in replays and in fork copies alike.

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

The tests use a fake SDK and a loopback stand-in for Prized's edge; no account needed. `tests/test_fork_group.py` runs real OpenEnv servers behind a fake Prized API whose fork copies the servers' sessions; `tests/test_fork_base.py` uses it for `fork_base` and `rebase()`, moving real WebSockets through the provider's proxy. `tests/test_template.py` covers `template="auto"` (build once, fork after, fallback, the state file, `delete_template()`) and runs the reseed launcher for real in a subprocess. `tests/test_live.py` runs `fork_group`, `fork_base`, `rebase()` and `template="auto"` on Prized with `coding_env` when `PRIZED_LIVE=1` and `PRIZED_API_KEY` are set.
