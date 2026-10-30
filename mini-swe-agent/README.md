# mini-swe-agent-prized

Run [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) and [SWE-bench](https://github.com/SWE-bench/SWE-bench)'s evaluation in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=mini-swe-agent) (Firecracker microVMs), one sandbox per instance. No Docker on your machine, nothing to pull locally.

```bash
pip install "mini-swe-agent-prized[swebench] @ git+https://github.com/prized-dev/integrations#subdirectory=mini-swe-agent"   # not on PyPI yet
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=mini-swe-agent
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=mini-swe-agent.

## Run the agent on SWE-bench

```bash
python -m minisweagent_prized.swebench --subset verified --split test -m <model> -w 20 -o out/
```

It takes the same options as `mini-extra swebench` and adds `--environment-class prized`. Sandbox options go through `-c`, e.g. `-c environment.vcpu=4`.

## Evaluate predictions, without Docker

```bash
python -m minisweagent_prized.evaluate --predictions_path out/preds.json --run_id my-run --max_workers 24
python -m minisweagent_prized.evaluate --predictions_path gold --run_id gold   # the reference patches
```

The flags are `swebench.harness.run_evaluation`'s (`--dataset_name` defaults to `SWE-bench/SWE-bench_Verified`), and so are the steps: each instance's image, the prediction applied with the same `git apply`/`patch` fallbacks, the instance's eval script under the same 30-minute timeout, graded by swebench's own `get_eval_report`. Logs land where swebench puts them (`logs/run_evaluation/<run_id>/<model>/<instance_id>/`, plus `prized.json` with the sandbox, timings and attempts), and the final report is swebench's `make_run_report`. Extra flags: `--vcpu` (2), `--memory_mb` (4096), `--disk_gb` (16), `--retries` (1, for a sandbox or image that didn't come up), `--image_template` (a mirror, e.g. `ghcr.io/epoch-research/swe-bench.eval.x86_64.{instance_id}:latest`), `--max_sandbox_hours` (stop starting instances past it), `--label k=v`.

## In your own config or code

```yaml
environment:
  environment_class: minisweagent_prized.PrizedEnvironment
  image: docker.io/swebench/sweb.eval.x86_64.django_1776_django-11099:latest
  cwd: /testbed
```

`PrizedEnvironment` takes `DockerEnvironment`'s options (`image`, `cwd`, `env`, `forward_env`, `timeout`, `interpreter`) plus `template` (`prized-base-v1`), `vcpu` (2), `memory_mb` (4096), `disk_gb` (20), `labels`, `max_runtime_min` (240: the sandbox is deleted after it even if nothing cleans up; the evaluator uses its timeout plus 25 minutes), `startup_timeout` (900 s), `pull_attempts` (3) and `pull_rate_limit_wait` (1800 s of waiting, with no sandbox held, while the registry throttles). Every sandbox is labelled `minisweagent=1`, so `prized sandbox rm --label minisweagent=1` removes any a crashed run left behind.

## How it works

A Prized sandbox starts from a fixed template, not from an image, and its user has no root. So the environment unpacks the instance's image into a directory with [`crane`](https://github.com/google/go-containerregistry/tree/main/cmd/crane) (pinned, checksum checked) and runs every command chrooted into it as root of an unprivileged user namespace, with the image's `ENV` and `WORKDIR`, `HOME=/root`, umask 022 and the open-files limit raised, as `docker exec` would. A seccomp filter makes `chown` to other users and setuid bits succeed without effect, as fakeroot does, so `apt-get`, `dpkg` and `pip` work. All commands of an instance share the namespaces, so a server one command starts is visible to the next. Where `crane export` drops files that hard links point to (a later layer deleted the file; Docker keeps it), they are written from their own layer (19 of the 22 xarray images in SWE-bench Verified need this).

## Results

SWE-bench Verified, all 500 instances, gold patches, the official `swebench/sweb.eval.*` images from Docker Hub, graded by swebench 5.0.2, October 30, 2026: **494 of 500 resolved (98.8%)**, 0 errors. SWE-bench's maintainers report 500 of 500 on their own harness since August 2026. The run took two passes: 210 verdicts come from the first commit of this folder, 290 from the second, which changed only how images are pulled.

| Unresolved | Why |
|---|---|
| psf__requests-1724, -1766, -1921, -2317 | Their graded tests call httpbin.org, which got a new certificate on October 28, 2026 (Let's Encrypt YE1, ISRG Root X2). The CA bundle that 2014-era requests ships can't verify it: `openssl s_client -CAfile requests/cacert.pem` fails from inside a sandbox and from outside Prized alike, so these fail on any harness until httpbin.org or the tasks change. 4 of 4 runs |
| sympy__sympy-13091 | Flaky: a `RecursionError` in one test under hash randomization. Failed 1 of 4 runs |
| django__django-10097 | Unexplained. Its eval script runs Django's whole test suite, and 7 auth template tests fail with `TemplateDoesNotExist` (they pass when run alone in the same sandbox). 4 of 4 runs here; SWE-bench reports it passing. Not yet compared under Docker |

Per instance (2 vCPU, 4 GB, 16 GB disk): median 59 s from sandbox create to delete, of which 4 s starting the sandbox, 43 s pulling and unpacking the image and 5 s running the tests. The graded attempts took 11.1 sandbox-hours (about $1.45 at list price); with the first pass's throttled attempts, 13.0. The passes ran 273 instances in 17 minutes at 24 sandboxes and 290 in 21 minutes at 20, so a full run takes about 30-40 minutes when Docker Hub doesn't throttle (estimate).

Docker Hub limits anonymous pulls per IP address (its registry reported `ratelimit-limit: 100;w=3600` to our sandboxes), and sandboxes on one host share an address. The first version pulled each image twice and was throttled after about 190 instances; the second pulls once, and pulled its 290 images without being throttled. A throttled pull deletes its sandbox and waits, sharing one backoff across the run. For repeated large runs, a mirror (`--image_template`) avoids the limit.

Agent: mini-SWE-agent with `openai/gpt-5.6-luna` (through OpenRouter, 60-step limit) on 10 Verified instances (`--shuffle --slice 0:10`): all 10 submitted a patch, and this evaluator resolved 7. 1.3 to 4.6 minutes per instance, 5 at a time.

## Limits

- The image must be pullable without credentials. Its `ENTRYPOINT`, `CMD` and `USER` don't apply: commands run as root, as mini-SWE-agent runs SWE-bench's images.
- One id underneath: root's `chown` to another user, its switches to other users and setuid/setgid bits succeed without effect. Programs that check a real user switch fail.
- No Docker inside a sandbox (so no harness that builds images), no GPUs. SWE-bench Multimodal's image assets aren't staged by the evaluator.
- Concurrency is bounded by your workspace's sandbox quotas: by default 50 sandboxes, 200 running vCPU and 500 GB of disk, so about 30 instances at a time with the evaluator's 16 GB disks. When a quota is full the evaluator waits for a slot (up to 30 minutes per instance). Ask team@prized.dev for more for large runs.

## Tests

```bash
scripts/test.sh mini-swe-agent           # from the repo root: lint + offline tests
FLOOR=1 scripts/test.sh mini-swe-agent   # oldest supported mini-swe-agent, prized and swebench
```

The offline tests run the environment's command scripts with the local bash against a fake sandbox, and grade the evaluator's output with swebench itself. There is no live test in CI; the gold run above is how the live path is checked.

License: Apache-2.0.
