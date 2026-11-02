# @prized/eve-sandbox

> **Not on npm yet.** The install line below works once `@prized/eve-sandbox` is published. The Prized TypeScript SDK it uses (`@prized/sdk`) is on npm.

A sandbox provider for Vercel's [eve](https://github.com/vercel/eve) that runs each session in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=eve), a Firecracker microVM. `eve build` prepares one paused template sandbox, each session starts as a fork of it in about two seconds, and an idle sandbox pauses itself after 10 minutes, keeping its memory and disk and billing storage only.

## Set up

1. Install it in your eve app (eve 0.86 or later, Node.js 24):

   ```sh
   npm install @prized/eve-sandbox
   ```

2. Set `PRIZED_API_KEY` ([make a key](https://prized.dev/dashboard/workspace/api-keys?via=eve) with Full access, or Custom with Sandboxes: Write) where `eve build` runs and where the agent runs, such as your Vercel project's environment variables. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=eve.

3. Use it in `agent/sandbox.ts` (`agent/sandbox/sandbox.ts` if `agent/sandbox/` exists):

   ```ts
   import { defineSandbox } from 'eve/sandbox';
   import { PrizedSandbox } from '@prized/eve-sandbox';

   export const environment = PrizedSandbox.environment({
     resources: { vcpu: 2, memoryGb: 4 },
     prepare: async (sandbox) => {
       const result = await sandbox.run({ command: 'python3 -m venv ~/.venv && ~/.venv/bin/pip install pandas' });
       if (result.exitCode !== 0) throw new Error(result.stderr);
     },
   });

   export default defineSandbox(() => environment.open());
   ```

4. Run `eve build`. It prints the template's id (`prepared Prized sandbox template sbx_…`).

The `bash`, `read_file` and `write_file` tools, and your own tools' `ctx.getSandbox()`, now run in the session's sandbox. `ctx.getSandbox(environment)` also has `sandboxId`, for anything else in [`@prized/sdk`](https://prized.dev/docs/sandboxes-python-typescript?via=eve).

## How it maps

| eve | Prized |
| --- | --- |
| `eve build` (`prepare`) | Creates a sandbox, writes the agent's workspace and skill files, runs your `prepare`, pauses it, and keeps it as the template (label `eve-template`). A build with the same inputs reuses it. An agent with nothing to prepare gets no template. |
| A session's first sandbox use (`start`) | Forks the template, or creates a sandbox when there is none. The sandbox carries the label `eve-session`, so concurrent starts of one session end up with one sandbox. |
| `sandbox.stop()`, server shutdown | Pauses it. Memory, disk and processes are kept. |
| A later turn (`resume`) | Reconnects without waking it. The first command or file request wakes it. |
| Idle | It pauses itself after `autoPauseMin` minutes (default 10). |
| Session ends, `sandbox.delete()` | Deletes it. |

## Start from your own sandbox

There are no custom images, so set up a sandbox once and start every session from it with `forkFrom`. With the [prized CLI](https://prized.dev/docs/cli?via=eve) and `PRIZED_API_KEY` set:

```sh
TPL=$(prized sandbox create --vcpu 2 --memory 4 --auto-pause 10m --label role=eve-base)
prized sandbox exec $TPL --timeout 10m -- 'git clone --depth 1 https://github.com/you/app && cd app && npm install'
prized sandbox pause $TPL --wait
```

```ts
export const environment = PrizedSandbox.environment({ forkFrom: 'sbx_…' }); // the id in $TPL
```

Sessions take the base sandbox's files, shape, environment variables and auto-pause, so set those on `prized sandbox create` (`eve build` warns when auto-pause is off). `template`, `resources` and `autoPauseMin` don't apply to forks. With workspace files, skills or a `prepare`, `eve build` forks the base once more and adds them there.

- Keep the base sandbox: builds fork it, and an agent with nothing to prepare forks it for every session (if it's gone, new sessions fail with eve's "template is not available" error).
- `eve build` doesn't see changes inside the base. After changing it, point `forkFrom` at a new base or edit `agent/sandbox.ts`.
- [Use a sandbox as your own template](https://prized.dev/docs/sandboxes-own-template?via=eve) shows how to add system packages without root.

## Options

`PrizedSandbox.environment()` takes these, plus eve's `prepare`. Each applies when a sandbox is created. With `forkFrom`, `template`, `resources` and `autoPauseMin` don't apply.

- `apiKey`: default `PRIZED_API_KEY`.
- `template`: `prized-base-v1` (default: Ubuntu 24.04, git, Python 3, Node.js 22) or `prized-agent-v1` (adds Claude Code and Codex).
- `resources`: `{ vcpu, memoryGb, diskGb }`. Default 4 vCPU, 8 GB, 10 GB.
- `env`: variables sent with every command, so a changed value needs no new build. `AI_AGENT=eve` is set unless you set it.
- `labels`: your own labels, to find the sandboxes with `prized sandbox ls --label`.
- `autoPauseMin`: idle minutes before it pauses itself. Default 10; `0` never.
- `maxRuntimeMin`: minutes of running before Prized deletes it. Default your plan's (24 hours paid, 60 minutes on the free tier).
- `forkFrom`: a paused sandbox to start from (above).
- `workspaceRoot`: where commands start. Default `/home/prized/work`, not eve's `/workspace`.

`environment.open({ labels })` adds labels to a session's sandbox, such as a user id.

## Limits

- Sandboxes run in Oregon (`us-west-2`) only.
- No root or sudo: commands run as the user `prized`. Install Python packages in a venv, and run `npm config set prefix ~/.local` before `npm install -g`.
- No Docker in a sandbox.
- No custom images or Dockerfiles. Use `forkFrom`.
- No stdin: `spawn({ stdin: true })` throws, so stdio servers don't work.
- No network policy: every sandbox has outbound internet, and eve's `setNetworkPolicy` isn't offered.
- A keyed `invokeTool` call fails if its tool opens the sandbox (eve's `toolSessions` isn't supported).
- Command output arrives as UTF-8 text, so binary output to stdout isn't byte-exact. Write it to a file instead.
- On the free tier a sandbox gets 60 minutes of running in all, a fork keeps its template's deletion time, and an unused template is deleted after 30 days. Top up (from $10) before you deploy.
- Templates are kept until you delete them, unless their machine fails; then run `eve build` again and redeploy. A build with changed inputs makes a new template and leaves the old one: `eve build` prints each id, so delete old ones with `prized sandbox rm <id>`.
- A paused sandbox can still count toward the workspace's running vCPU and memory quota (200 vCPU and 400 GB by default), so end sessions you're done with.

## Tests

```sh
npm test                                   # offline: fakes for eve's side and the Prized API
PRIZED_API_KEY=dcp_... npm run test:live   # one run against the real API
```

The live run builds a template, forks a session from it, runs commands, moves files, pauses and wakes it, then deletes everything it made, failures included. It makes up to four 1 vCPU, 1 GB sandboxes (at most two running at once) and takes under a minute.

## License

Apache-2.0
