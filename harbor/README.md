# harbor-prized

Run [Harbor](https://github.com/harbor-framework/harbor) tasks, such as Terminal-Bench, in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=harbor) (Firecracker microVMs). No Harbor fork or PR needed.

```bash
pip install harbor harbor-prized   # not on PyPI yet: until then, "harbor-prized @ git+https://github.com/prized-dev/integrations#subdirectory=harbor"
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=harbor
harbor run -d terminal-bench@2.0 -a oracle -e harbor_prized:PrizedEnvironment -n 4
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=harbor.

Each trial gets its own sandbox, sized from the task's `cpus`, `memory` and `storage`, plus 20 GB of disk for the image. The task's prebuilt `docker_image` is unpacked with `crane`, and every command runs chrooted in an unprivileged user namespace, as uid 0 with the image's `ENV` and `WORKDIR`. The sandbox is deleted when the trial ends.

Options (`--ek key=value`): `template` (default `prized-base-v1`), `max_runtime_min` (180), `image_headroom_gb` (20), `labels` (`k=v,k=v`).

## Results

Terminal-Bench 2.0, oracle agent, Harbor 0.24.0 from PyPI, October 2026: 30 of the 34 tasks we ran pass. We have not run the full set of 89 yet.

| Fails | Why |
|---|---|
| configure-git-webserver, pypi-server | Their setup adds users or runs `dpkg` steps that `chown` to other users; only uid 0 is mapped |
| torch-tensor-parallelism | Verifier timed out at 900 s on the task's 1 vCPU |
| gpt2-codegolf | The compiled program segfaults; not investigated |
| crack-7z-hash (an earlier run) | Agent timeout at 30 min, CPU-bound on 1 vCPU |

## Limits

Prebuilt `docker_image` tasks only (no Dockerfile builds). Only uid 0: `user=` is ignored and setuid does not work. No Docker Compose, GPUs, or network policies. One command runs up to an hour; a sandbox runs 24 hours by default, up to 7 days on a paid plan (1 hour on the free tier).

Concurrency: each Terminal-Bench trial takes 1 vCPU, 2 GB of memory and 30 GB of disk, so the workspace's sandbox and disk quotas set how many run at once. Lower `image_headroom_gb` to fit more, or ask team@prized.dev to raise the quota for large runs. On the free tier pass `--ek max_runtime_min=60` (its limit); its 100 GB of disk fits three trials at a time.

License: Apache-2.0.
