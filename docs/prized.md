# Prized agent computers

The `prized` backend runs each QM scope in a [Prized](https://prized.dev/docs/sandboxes) sandbox, a
Firecracker microVM. Ending a turn pauses the computer with its memory and disk; the next turn
resumes the same machine, running processes included.

## Configure

```jsonc
"sandbox": { "backend": "prized" }
```

Set `PRIZED_API_KEY` with `qm secrets set`. The key needs Sandboxes: Write; create one at
https://prized.dev/dashboard/workspace/api-keys. For core run from source, set
`SANDBOX_BACKEND=prized` and `PRIZED_API_KEY` in core's environment instead. Production requires
`DATABASE_URL` for sandbox records and provisioning locks across instances.

There are no other settings. Computers get the template's default shape, and the shared
`SANDBOX_TIMEOUT_SEC` (default 600) sets the command deadline.

Use a workspace that has paid in. Computers ask for Prized's longest run time, seven days, which
restarts at every resume; the free tier allows 60 minutes in all, so it refuses the create with
that reason.

## The machine

Computers start from Prized's `prized-agent-v1` template: Ubuntu 24.04, Node 22, Python 3, git,
ripgrep, jq, Claude Code and Codex, with the default 4 vCPU, 8 GB of memory and a 10 GB disk. On
its first start each computer, scratch ones included, installs `gh` and AWS CLI v2 (the
`fly/Dockerfile` pins) under `~/.local`, which is first on `PATH`; this adds six to eight seconds
to that start. The user is `prized`, without sudo, and the workspace is `/home/prized/workspace`.

Prized has no custom templates, so there is no image to build. The root file system is read-only,
so deployment-layer files bound for `/usr/local/bin` or `/usr/local/lib` install under
`~/.local/bin` and `~/.local/lib` instead; a tool that names `/usr/local/lib` in its own code must
find its files relative to itself to run here.

Each computer carries the labels `qm-deployment` (`qm`), `qm-name` (the scope's computer name)
and `qm-kind` (`computer` or `scratch`). QM finds computers by these labels after a
restart, and a second core adopts the same computer instead of creating another.

## Lifecycle

- **Teardown pauses.** A paused computer bills storage only and resumes on its next command.
  Teardown with `keepWarm` (live background processes) refreshes activity instead, and Prized
  pauses the computer after `BACKGROUND_JOB_TTL_MAX_SEC` with no activity.
- **Lost computers are refused.** A computer deleted outside QM fails the turn with a message to
  retire the sandbox and create a new one; QM never replaces a home silently. One in `error` fails
  the turn and is kept, since it may recover. There are no home snapshots, so publish durable work
  to git or Files.
- **Scratch computers** are deleted when their last lease ends, and pause after 10 idle minutes and
  delete a day later as a fallback.
- **Refusals** (balance, quota, key, template) reach the user with Prized's own message and are not
  retried.

## Egress

Egress is not enforced: sandboxes reach the public internet and nothing reaches them from it, and
the profile reports `egressEnforcement: "none"`.

## Tests

`test/prized-sandbox.test.ts` and `test/prized-client.test.ts` run against in-process fakes, so CI
needs no Prized account. `npm run smoke:prized-sandbox` exercises a real workspace through the
Sandbox interface (provision, commands, files, process sessions, pause and resume, scratch and
scope deletion), prints its timings, and deletes what it made. It reads `PRIZED_API_KEY`, and
`PRIZED_SMOKE_LABELS` (`key=value,...`) adds labels to every sandbox it creates.
