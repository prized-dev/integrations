# openenv-prized

Run [OpenEnv](https://github.com/openenv/OpenEnv) environments in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=openenv) (Firecracker microVMs), with pause, resume and fork.

```bash
pip install "openenv-prized @ git+https://github.com/prized-dev/integrations#subdirectory=openenv"   # not on PyPI yet
export PRIZED_API_KEY=...   # a workspace key with Sandboxes: Write, from https://prized.dev/onboarding/sandboxes?via=openenv
```

```python
from echo_env import EchoEnv
from openenv_prized import PrizedProvider

env = await EchoEnv.from_docker_image("space:openenv/echo_env", provider=PrizedProvider())
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

copies = base.fork(8)          # 8 started providers, servers already installed and running
for p in copies:
    env = EchoEnv(base_url=p.base_url, provider=p)   # closing env deletes that copy

base.pause()                   # memory and processes kept, compute not billed
# the next request through base.base_url resumes it
```

Open sessions don't carry over into copies or across a pause: clients connect to a copy's `base_url` and start new sessions. In our live test, a session open on the parent kept working through a fork (the parent pauses briefly while it is copied).

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

The tests use a fake SDK and a loopback stand-in for Prized's edge; no account needed.
