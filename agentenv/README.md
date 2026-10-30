# agentenv-prized

Run [AgentEnv Framework](https://github.com/scaleapi/agentenv-framework) (`agent-env`, by Scale) environments and agents in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=agentenv) (Firecracker microVMs). It is a sandbox provider plugin: no agent-env fork or PR needed.

**Status: early.** Its gateway and agent flows need two things Prized doesn't offer yet: the `prized-docker-v1` template (Docker inside the sandbox) and public port URLs. Until both are live, it creates, runs commands in, moves files into and deletes sandboxes, and refuses what it can't do yet with an error that says so. See [Limits](#limits).

```bash
pip install agentenv-framework agentenv-prized   # not on PyPI yet: until then, "agentenv-prized @ git+https://github.com/prized-dev/integrations#subdirectory=agentenv"
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=agentenv
```

```toml
# .agentenv/config.toml
[sandbox.providers.prized.config]
api_key = "env:PRIZED_API_KEY"   # or "secret:prized_api_key"
```

```bash
agent-env plugin check
agent-env run hello --sandbox prized
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=agentenv.

## How it works

- **Sandboxes:** each env and agent sandbox is one Prized sandbox from the configured template, sized up from the request to whole vCPUs and GiB. A request above 32 vCPU, 128 GiB of memory or 128 GB of disk is refused before anything is created, not shrunk.
- **Lifetime:** the run's `timeout` becomes the sandbox's run time (whole minutes, up to 7 days), after which Prized deletes it even if a run never tears it down. Auto-pause stays off.
- **Commands:** each one runs detached in the sandbox and is followed to its end, with no input and no time limit, so a long `docker compose up` or build isn't cut off. A lost answer is exit code -1, which agent-env's `exec_script` retries for idempotent scripts.
- **Root:** agent-env runs some commands with `sudo`. The provider checks once per sandbox: the template's `sudo` when it has one; otherwise, on a template with Docker, a privileged helper container (`agentenv-prized-root`, Debian slim from `public.ecr.aws`) that runs them chrooted into the sandbox's own filesystem, which is root in that sandbox's VM and nowhere else; otherwise they run as the sandbox user `prized`, and root-owned paths are refused.
- **Files:** small writes go inline; larger ones and objects a store can't sign a URL for go through Prized's files API in 64 MiB parts, joined in place and checked by sha256.
- **Ports:** exposed ports get the URLs the Prized API gives for the sandbox (`previewUrlTemplate`); a workspace without them refuses exposed ports and deletes the sandbox.
- **Reconnect:** `get_sandbox` restores the exposed ports from the sandbox's labels, on a freshly built provider too. A sandbox that is gone raises `SandboxNotFoundError`, and `terminate` is idempotent.
- **Cleanup:** a sandbox whose setup fails or is cancelled is deleted before the error is raised. A create that fails or is cancelled before it answers is deleted by the Prized SDK.
- **Attribution:** every sandbox carries the label `agentenv=true` and each attribution dimension as `agentenv-<key>`, so `prized sandbox ls` and the dashboard can filter a run's sandboxes.

## Configuration

All keys go under `[sandbox.providers.prized.config]`. Unknown keys are refused.

| Key | Default | Meaning |
| --- | --- | --- |
| `api_key` | required | A Prized API key, as an `env:` or `secret:` reference. The provider never reads the environment itself, and workloads never see it. |
| `template` | `prized-docker-v1` | The template sandboxes start from (`prized sandbox templates` lists them). |
| `base_url` | `https://api.prized.dev` | The Prized API. |
| `edge_url` | from the API | The command endpoint, for another deployment. |
| `root_helper_image` | `public.ecr.aws/docker/library/debian:bookworm-slim` | The image of the root helper container. |

## Limits

- **Docker:** agent-env's gateway, image loading and agent containers need Docker with Compose v2 in the sandbox. Prized's Docker template, `prized-docker-v1`, isn't published yet; on a template without Docker, a gateway or agent deploy fails at setup with a message that names it, and the sandbox is deleted.
- **Ports:** Prized sandboxes have no public port URLs yet. Envs and agents reach each other over them, so `deploy_env` and `deploy_agent` wait on them too.
- **Network policy:** Prized has no egress allowlists yet, so a policy that restricts egress is refused with `NetworkPolicyUnsupportedError`, and a fallback chain moves on to its next provider.
- **Images:** a per-call `image` is refused; sandboxes boot the template, and OCI images run under Docker inside. `boot_mode` is ignored with a warning.
- **Platform:** linux/amd64, no GPUs.

## Tests

Offline, with a stand-in for the Prized SDK (commands run by this machine's bash under a temporary directory):

```bash
scripts/test.sh agentenv
```

Live, against your workspace (creates and deletes a few small sandboxes, all labelled `agentenv-live-run=<run>`; checks that need Docker or port URLs skip, saying which):

```bash
PRIZED_LIVE=1 PRIZED_API_KEY=dcp_... PRIZED_TEMPLATE=prized-base-v1 pytest tests/test_live.py -rs
```

License: Apache-2.0.
