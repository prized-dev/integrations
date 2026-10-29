# Prized as a selectable agent-computer backend

Hudson from Prized here. This PR lets an operator pick Prized for agent computers with `"sandbox.backend": "prized"`, the same way #2370 did for E2B.

A Prized sandbox is a Firecracker microVM that pauses with its memory and disk and resumes on the same machine, running processes included. So the fit is one computer per scope. Teardown pauses it and an idle computer bills storage only. The next turn resumes it. A `keepWarm` teardown refreshes activity instead, and Prized pauses the computer after `BACKGROUND_JOB_TTL_MAX_SEC` with nothing to do.

What the PR adds

- `SANDBOX_BACKEND=prized` with one secret, `PRIZED_API_KEY`, and no other settings. Computers get the `prized-agent-v1` defaults (4 vCPU, 8 GB, 10 GB disk).
- `"sandbox.backend": "prized"` in the CLI, wired like e2b on docker, fly and aws, with tests.
- `src/sandbox/prized-sandbox.ts` on the shared exec base, and a small client over `@prized/sdk`, loaded lazily like the e2b SDK.
- `docs/prized.md` and a live smoke script.

How it behaves

- Computers are found by labels, so a second core adopts the same one, and `destroyScope` deletes them all.
- A computer that's gone or in `error` fails the turn with a `NonRetryableTurnError` asking to retire it. Nothing gets replaced with an empty home. Refusals like balance, quota or a revoked key reach the user with our own message, which is what the Agent37 refusal ADR asked for.

One thing to know. Prized has no custom templates yet, and the guest has no sudo and a read-only `/usr/local`. So each new computer, scratch ones too, installs gh and the AWS CLI (the `fly/Dockerfile` pins) under `~/.local`, six to eight extra seconds on its first start. Layer files for `/usr/local` land in `~/.local` too. A template with these tools is on us, and then the install goes away.

It doesn't add checkpoints or home snapshots, egress enforcement, a deploy provider or core hosting.

If you would rather take only this file and do the code yourselves, say so and I will trim the PR.
