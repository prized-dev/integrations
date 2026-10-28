# harbor-prized

Run [Harbor](https://github.com/harbor-framework/harbor) tasks, such as Terminal-Bench, in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=harbor) (Firecracker microVMs). No Harbor fork or PR needed.

```bash
uv tool install harbor --with "harbor-prized @ git+https://github.com/prized-dev/integrations#subdirectory=harbor"   # not on PyPI yet
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=harbor
harbor run -d terminal-bench/terminal-bench-2-1 -a oracle -e harbor_prized:PrizedEnvironment -n 4
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=harbor.

Each trial gets its own sandbox, sized from the task's `cpus`, `memory_mb` and `storage_mb`, plus 20 GB of disk for the image, and deleted when the trial ends. The task's prebuilt `docker_image` is unpacked with `crane`, and every command runs chrooted into it as uid 0 of one unprivileged user namespace per trial, with the image's `ENV` and `WORKDIR`. A seccomp filter makes `chown`, setuid/setgid bits and switches to other users succeed without effect, as fakeroot does, so `apt-get`, `useradd` and `sudo` work. Long commands run detached through the [`prized` SDK](https://pypi.org/project/prized/), so an hour-long agent run outlives a dropped connection.

Options (`--ek key=value`): `template` (default `prized-base-v1`), `max_runtime_min` (180), `image_headroom_gb` (20), `labels` (`k=v,k=v`).

## Results

Terminal-Bench 2.1, all 89 tasks, oracle agent, no retries, `-n 6`, Harbor main f3a5a923, October 28, 2026: **79 of 89 pass (88.8%)** in 56 minutes, 3.95 sandbox-hours (about $0.56 at list price). Six of the ten failures are task rot that Sail also hit on Modal ([harbor#3475](https://github.com/harbor-framework/harbor/pull/3475#issuecomment-6022288555)); without them, 79 of 83 pass (95.2%).

| Fails | Why |
|---|---|
| build-cython-ext, crack-7z-hash, mcmc-sampling-stan, protein-assembly, qemu-alpine-ssh, qemu-startup | The task itself: dependencies moved, Debian 404s, or the reference solution runs past its timeout. They fail on Modal too |
| configure-git-webserver, git-multibranch | `git push` over ssh: sshd's login needs a real switch to the git user |
| mailman | Postfix checks that `postdrop` is group-owned and setgid |
| torch-tensor-parallelism | The verifier runs past its 900 s timeout on the task's 1 vCPU |

The same run on 0.1.0 passed 70 of 89: ten tasks failed on `chown`, `dpkg` or `sudo`, and three on commands longer than about 15 minutes.

## Limits

- Prebuilt `docker_image` tasks only. Dockerfile builds and Docker Compose tasks are refused before a sandbox starts.
- One user: commands run as root and `user=` is ignored. Switching users succeeds without effect, so programs that check the switch (an `sshd` login, `postgres`, Postfix's ownership checks) fail.
- No GPUs or network policies (Harbor refuses those tasks).
- Concurrency: each Terminal-Bench trial takes 1 vCPU, 2 GB of memory and 30 GB of disk, so the workspace's sandbox and disk quotas set how many run at once. Lower `image_headroom_gb` to fit more, or ask team@prized.dev to raise the quota for large runs. On the free tier pass `--ek max_runtime_min=60` (its limit); its 100 GB of disk fits three trials at a time.

License: Apache-2.0.
