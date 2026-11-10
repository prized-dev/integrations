# feat(agentic): add Prized as a sandbox provider (OpenEnv route)

Draft: the offline half of a new sandbox provider, in the shape of the Novita PR (#4435).

Where it stands against `docs/developer/adding-a-sandbox-provider.md`: step 1 (the registry entry)
is here. Steps 2 and 3, the golden episode (`scripts/sandbox_smoke/run.py --connector harbor
--backend prized`) and the GPU rollout, both run through Harbor, and Harbor has no `prized`
environment today. So neither has been run, the provider table gets a row with empty cells, and
nothing here is claimed as working end to end. What this PR adds beyond step 1 is the OpenEnv
backend, which does not need Harbor.

## What this adds

[Prized](https://prized.dev/docs/sandboxes) sandboxes are Firecracker microVMs that pause with their
memory kept and fork, running or paused, into copies that each start with the parent's memory,
disk and processes.

Prized has no templates built from an image. So what the E2B and Novita backends keep as a named
template, this backend keeps as **one paused sandbox per task**:

- First episode of a task: create a sandbox, `docker build` the recipe on the task's official image
  inside it, start the env server in a container, wait for `/health`, pause.
- Every later episode: **fork** that paused sandbox. The copy starts with the image built and the
  env server already answering, so the episode pays for neither.
- `python tb2_sandbox_prized.py --tasks-dir ... --tasks a,b` builds parents ahead of a run, like the
  E2B and Novita bake CLIs.

Files:

- `miles/rollout/agentic/credentials.py`: the `prized` registry entry (`PRIZED_API_KEY`, or
  `PRIZED_API_KEY_FILE`, default `~/.config/prized/api_key`; nothing forwarded by value).
- `examples/experimental/openenv/tb2_sandbox_prized.py`: the materialization (parent build, fork,
  lifetime, the port forwarder, bake CLI).
- `examples/experimental/openenv/openenv_prized_agent_function.py`: the backend (start hook, and
  which API errors count as throttling).
- `AGENT_MODULES`, launcher args, the `[prized]` extra (`prized>=0.1.3`), README and
  `sandbox-providers.md`.

## Three things that differ from the other backends

1. **The URL is a loopback forwarder.** A Prized sandbox port answers only requests that carry the
   workspace key in a `Prized-Key` header, and the env client takes a bare base URL. So `base_url`
   is `http://127.0.0.1:<port>` in the rollout process: a small forwarder adds the header and
   carries HTTP and the WebSocket to the sandbox over TLS. Nothing is made public and the key is
   never in a URL. Any process on the rollout host can reach that port while the episode lives.
2. **No keepalive thread.** A sandbox's lifetime is a hard cap on its running time, and each copy
   gets it afresh from its parent. The cap is 30 minutes (the margin open PR #4756 uses for E2B)
   past the longer of the episode cap (`OPENENV_MAX_ROLLOUT_TIME_SECONDS`) and the parent's own
   build window; with the defaults, 90 minutes. An orphan is deleted then however its creator died.
3. **The env server's knobs are baked.** It is already running in the parent, so
   `TB2_COMMAND_TIMEOUT_S` is part of the parent's digest label, as are the template, the sandbox
   shape and the lifetime cap: a rollout with other values builds its own parents, and a bake has
   to run with the rollout's environment to be found.

## Limits

- **It cannot run today.** The parent needs Docker in the sandbox, from the `prized-docker-v1`
  template, and that template is not available yet: a create from it answers `409
  template_inactive`.
- Copies start on their parent's machine, as many as fit there. A fork that does not fit is refused
  whole (`fork_capacity`) and retried by the shared backend as throttling; if it still does not fit
  when the retries run out, the episode fails. So the samples of one task that run at once are
  bounded by one machine.
- A workspace's disk quota counts paused sandboxes (500 GB by default), and each parent holds its
  whole disk (30 GB by default). That is about 16 parents and copies at once: the full
  89-task suite does not fit without a larger quota.
- A paused parent bills storage and is deleted after a day paused
  (`OPENENV_PRIZED_PARENT_KEEP_MIN`); the next episode of that task rebuilds it.
- Builds are locked per task within one rollout. Two rollouts in one workspace are not locked
  against each other: both build, and each forks the newest parent.
- The OpenEnv route only. The Harbor route passes `HARBOR_ENV_TYPE` straight to Harbor, which has no
  `prized` environment today. Once it does, the lifetime cap would be one more branch in
  `_environment_config`, next to Daytona's.

## Tests

Run in a clean Linux VM on `main` at 171c33bd8f, Python 3.12, with `requirements.txt` and
`tests/ci/requirements-ci-cpu.txt` installed and sglang (`sglang-miles`) and Megatron-LM
(`miles-main`) on `PYTHONPATH`, as the CPU CI workflow sets them up:

- `pre-commit run` on the changed files: clean (helm-lint skipped; no chart is touched).
- `tests/fast/examples/experimental/{openenv,harbor}`, `tests/fast/rollout/agentic` and
  `tests/fast/scripts/test_sandbox_smoke.py`: 296 passed, 5 skipped. Unpatched `main`: 218 passed,
  5 skipped.
- A wider set (`tests/fast/examples/experimental`, `tests/fast/tools/test_lint_config_attribute_access.py`,
  `tests/fast/doc`, `tests/fast/launch_scripts`, `tests/ci/test`): 1,942 passed, 35 skipped, 2 failed. Unpatched `main`: 1,866 passed, 35 skipped, and the
  same 2 failed (`tests/ci/test/test_reconcile_dependencies.py`, which resolves GPU packages).
- The whole of `tests/fast` was not completed: the VM running it died partway, and it was not
  rerun.
- 71 of the new tests are the two new files. They fake the Prized SDK and cover: the
  digest label (recipe, shape, server command, container flags, template, lifetime); the parent
  build and its failure paths (a parent that fails before its pause is deleted and never paused);
  fork per episode; rebuild once when the parent is gone or its copy has no server, and never a
  second time for a parent that was itself such a replacement; no rebuild on `fork_capacity` or
  `parent_too_big`; the lifetime arguments; key supply; throttling decided on
  the API's code and reason (a failed build is never retried as throttling, whatever its log
  says); and the forwarder, against a local TCP server and against a local TLS server with a
  self-signed certificate (header added, Host rewritten, request body, an upgraded connection both
  ways, a client that stops sending before the answer, stop in mid-stream, descriptors numbered
  above 1023, and an upstream certificate that does not verify).
- A mutation check on the two new modules: 50 of 50 deliberate breaks were caught by those tests.
- `rollout_lock` around the build is not exercised by a test.
- `tests/e2e/agentic/test_sandbox_golden.py` now skips a provider the installed Harbor has no
  environment for, since that case runs through Harbor. The guide says step 1 needs no test change;
  glad to drop this if you would rather the case fail until Harbor carries `prized`.

Also checked against the real Prized API, from our own harness. It used stand-ins for the Miles
modules this file imports, a stand-in for the `docker` binary, a 40-line stand-in for the env
server and the plain template, because the Docker template is not available. So it shows the
Prized calls and the forwarder, not a task image: parent create, pause and fork; `/health`, a
half-closed client and two concurrent WebSockets through the forwarder over TLS; the copy's own
90-minute expiry; four concurrent episodes forking one existing parent; delete; forced re-bake; a
rebuild when the parent had been deleted before the lookup; and a rebuild when the parent had no
server in it. Two copies reported the same server process id, so the process came with the fork.
With that stand-in server, fork plus ready took about 2 s; that is not a number for a real task
image.

## Not run yet

- **The golden episode** (step 2 of the guide), and the same thing on the OpenEnv route
  (`scan_golden.py` with `OPENENV_SANDBOX_BACKEND=prized`).
- `docker build` of a real Terminal-Bench-2 task image inside a Prized sandbox, and a fork of a
  sandbox whose env server runs in a container.
- A GPU rollout.

## Agents used

The code and the tests were written by Claude (Anthropic) coding agents run by Prized and reviewed
by a second agent that did not write them.
