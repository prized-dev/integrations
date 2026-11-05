# opencode-prized: design

What the plugin does, which OpenCode API it rests on (file and line), and how it was tested. Line numbers are OpenCode **v1.19.5** (`opencode-ai`, tag `v1.19.5`, 810c0eb) for OpenCode 1 and **dev at 5ea5c5e** (2026-11-04, = 2.2.5) for OpenCode 2. All paths are in [anomalyco/opencode](https://github.com/anomalyco/opencode).

## Why a plugin, and why tool replacement

- OpenCode 1 is what most people run: `opencode-ai` had 13.4M npm downloads from 10-05 to 11-03, against 1.7M for OpenCode 2's `@opencode/cli`. The community list in `packages/web/src/content/docs/ecosystem.mdx` is OpenCode 1's documentation.
- OpenCode 2 has a lower-level seam, a workspace `Driver` with a spawner and file overrides (`packages/core/src/environment/driver.ts` L4-7). But only an embedder of the SDK can register one (`packages/core/src/workspace/driver.ts` L81-88, `packages/sdk/src/internal/host.ts` L21, L43-45). Plugins get no workspace hook; issue #45612 asks for one.
- So the plugin replaces the tools themselves. It keeps OpenCode's tool names, parameters and permission prompts, so the model, your permission rules and the TUI's renderers keep working. Only where the tools run changes.
- One default export serves both lines:
  - OpenCode 1 reads `{ id, server }` (`packages/opencode/src/plugin/shared.ts` L272-300 at v1.19.5). It resolves the package's `./server` export first, then `main` (L103-114).
  - OpenCode 2 decodes `{ id, setup }` with an Effect `Schema.Struct` (`packages/core/src/plugin/module.ts` L80-95, L170-186). A Struct ignores keys it does not name, so the extra `server` key does no harm. OpenCode 1 likewise ignores `setup`.

## OpenCode 1 (`src/v1.ts`)

- **Tools replace the built-ins.** `bash`, `read`, `write`, `edit`, `glob`, `grep` and `apply_patch` come from the plugin's `tool` hook (`packages/plugin/src/index.ts` L226-228). The registry lists the built-ins first and plugin tools after them (`packages/opencode/src/tool/registry.ts` L199-204, L256-259). The session then keys every tool by id, so the later definition wins (`packages/opencode/src/session/tools.ts` L92-99).
  - The model sees a single `bash` and a single `read`, with OpenCode's schemas. In the live test only `command` was required.
  - The arguments are zod, the same zod (4.1.8) as `@opencode-ai/plugin` 1.19.5. The registry turns them into JSON Schema (`registry.ts` L125-137).
  - The output passes through OpenCode's own truncation (L159).
- **`apply_patch` matters.** For `gpt-*` models OpenCode drops `edit` and `write` and offers only `apply_patch` (`registry.ts` L297-300). Without an `apply_patch` replacement, GPT models would edit your files locally.
- **Parameters and prompts match OpenCode's.**
  - The schemas follow `tool/shell/prompt.ts` L16-22, `read.ts` L28-36, `write.ts` L20-25, `edit.ts` L47-56, `glob.ts` L10-15, `grep.ts` L10-18 and `apply_patch.ts` L18-20.
  - Permission asks use the built-ins' names and patterns: `bash`, `read`, `edit` with a `diff`, `glob` and `grep` (`edit.ts` L102-110, `shell.ts` L283-290).
  - `edit` and `write` send `metadata.diff`, which the TUI's permission dialog renders (`packages/tui/src/routes/session/permission.tsx` L199-207).
- **Refused:** `lsp` (and `list`, `multiedit`, `patch` where an OpenCode version has them), through `tool.execute.before` (`index.ts` L266-269, fired at `session/tools.ts` L106-110). They would act on your machine.
- **Pause:** the `event` hook (`plugin/index.ts` L255-262 passes `{type, properties}`) receives `session.status` with busy/idle and `session.idle` (`session/status.ts` L39-46; `packages/schema/src/session-status-event.ts` L44-45). The sandbox pauses `pauseSeconds` after the whole session tree is idle and no tool call is in flight.
- **Subagents:** a `task` subagent's session has `parentID`. The plugin walks up to the root through `client.session.get`, so a subagent works in its parent's sandbox.
- **Fork:**
  - `Session.fork` copies the messages and the tool parts with their metadata (`session/session.ts` L691-733).
  - Every tool result records `metadata.prized = { sandboxId, owner }`.
  - On a forked session's first tool call, the newest record names a sandbox whose owner is another session. The plugin forks that sandbox (`Sandbox.fork`, processes included) and labels the copy with the new session's id. This is pi-prized's "inherited entry, different owner, copy it", unchanged.
- **Setup errors fail closed:** `server()` never throws. OpenCode 1 drops a plugin that fails to load without a word (`plugin/index.ts` L224-241), and its local tools would then run. With a bad option, the tools still replace the built-ins and refuse with the reason.
- **Restart:** sandboxes carry the labels `created-by=opencode-prized` and `opencode-session=<root>`. A new OpenCode process finds the session's sandbox with `Sandbox.list({labels})` and keeps no local state.
- **Commands:**
  - The `config` hook adds `/prized` and `/prized-pull` to `cfg.command` (`plugin/index.ts` L245-253; `command/index.ts` L90-103 reads them).
  - `command.execute.before` (`session/prompt.ts` L1460-1473) runs the command in plugin code, not in the model. It then replaces the prompt with the result. OpenCode 1 always sends a command to the model, so the model replies with a sentence about it.
- **Exit:** the `dispose` hook (`plugin/index.ts` L265-278) runs when OpenCode exits, and the live test confirmed it. A sandbox with nothing to pull is deleted. One with changes is paused and kept, and the note goes to stderr.
- **System prompt:** `experimental.chat.system.transform` adds one paragraph. It says where the project is in the sandbox, and that the user's env and ignored files are not there.

## OpenCode 2 (`src/v2.ts`)

- **Tools:**
  - `ctx.tool.transform(editor => editor.update(id, fn))` (`packages/plugin/src/promise/tool.ts` L26-36; `packages/core/src/tool.ts` L216-226) keeps the built-in's name, input schema and options (`codemode: false`, `permission: "edit"`) and swaps `execute`.
  - External plugins load after the built-ins (`packages/core/src/plugin/supervisor.ts` L94-98), so `shell`, `read`, `write`, `edit`, `patch`, `glob` and `grep` exist to update.
  - `read` hands `skill://` URIs back to the built-in, because OpenCode serves those from memory (`tool/plugin/read.ts` L60-73).
- **Permissions:**
  - The built-ins assert their own permissions, and so do the replacements (`ctx.permission.assert`, `packages/plugin/src/promise/permission.ts` L31). They use the same actions: `shell` (`tool/plugin/shell.ts` L127-132), `read`, `edit`, `glob` and `grep`.
  - Each assert carries `source: {type: "tool", messageID, id}`.
- **Pause:** the `experimental.idle` session hook (`packages/plugin/src/promise/session.ts` L198). A tool call marks the session busy. The `session.execution.failed` and `session.execution.interrupted` events also count as idle, since the hook does not run after those.
- **Fork:** a forked session's `fork.sessionID` names its source (`packages/schema/src/session.ts` L33-38). The plugin finds the source's sandbox by label and forks it.
- **Commands:** `ctx.command.transform(editor => editor.add({name, execute}))` (`packages/plugin/src/promise/command.ts` L7-26) runs plugin code. The result goes into the session with `ctx.session.synthetic`, and no model turn is needed.
- **Prompt:** the `context`, `compaction` and `generate` hooks add the same paragraph. They also replace the shell tool's description, which the built-in fills with your machine's OS and shell.
- **Exit:** the cleanup returned from `setup` runs on unload and server close.
- **Background:** OpenCode 2's shell takes `background`. The replacement starts the command detached with `setsid nohup`, writes its output to a file, and returns the pid at once.
- **Rename guard:** if a tool this plugin replaces is missing (OpenCode renamed it), every file or shell tool it could not replace is removed, and the plugin reports an error. It never leaves such a tool running locally.
- **Off:** built-ins that act on your machine are removed with `editor.remove`: `experimental_image_generate` (`tool/plugin/image-generate.ts` L27), worktree create/remove and session move (`tool/plugin/opencode.ts` L139-190), and `lsp`.
- **Found live, fixed:**
  - Result metadata must be plain JSON. An `undefined` value made OpenCode 2 drop the call's result, so the model saw "Tool result missing". `Tools.meta()` now round-trips through JSON.
  - Progress is not sent. The runner kills a call whose progress lands before it is marked called or after it settled (`packages/core/src/session/runner/publish-llm-event.ts` L548-551), and its TUI streams shell output only from its local shell service.
  - A command's result is posted with `resume: true`. With `resume: false` it waited in the session inbox until the next prompt, so the user saw nothing.

## Shared core (ported from `@prized/pi`, integrations branch `pi-prized` 4d4b73b)

- `src/workspace.ts` is pi's file with OpenCode's names. It does three things:
  1. It packs the git working tree with `git ls-files --cached --others --exclude-standard`, so ignored files stay home.
  2. It uploads one tarball and commits a baseline in the sandbox.
  3. It pulls by diffing the sandbox tree against that baseline and running `git apply` locally. `git apply --check` comes first, so a conflict applies nothing.
- **The review comes from git on your machine.** The checks below all run on your machine, against the patch that is then applied. The sandbox, which the agent controls (its `git` too), cannot show one change and apply another.
  1. `changes()` asks git in the sandbox for a plain patch: `--no-ext-diff --no-textconv --no-renames`, fixed `a/` `b/` prefixes, `--full-index --binary`, in a scratch folder per call.
  2. `patchFiles()` reads that patch's headers strictly, as git writes them. It refuses renames, copies, a path listed twice, CR in a header, and a mode named twice.
  3. `verifyPatch()` checks with `git apply --numstat -z` that git would touch exactly those files. A plain unified-diff section with no `diff --git` line cannot slip through.
  4. `appliedModes()` puts the touched files, as they are in your checkout, into a scratch index, with blobs in a scratch object folder so your repository is untouched. It applies the patch there with `git apply --cached` and reads each file's resulting mode with `git ls-files -s`. The review list comes from git's own answer, so a header that seems to say "100644" but makes git create a symlink or an executable is still listed.
  5. `localStat()` (`git apply --stat`) and `diffOf()` show the same bytes.
- `risks()` is pi's review list, with more added: `.opencode/`, `opencode.json(c)`, `AGENTS.md`, `CLAUDE.md` (OpenCode loads them as instructions), `.gitignore` (dropping `.env` from it uploads `.env` next time), commitlint and lint-staged configs, `.idea/`, `.zed/`, `.cargo/`, `.circleci/` and `.gitlab-ci.yml`. Names are matched case-insensitively, because on macOS `.Husky/` lands in `.husky/`. It flags a symlink, an executable that is new, made executable or **edited in place** ("a change to an executable file: X (read the diff)"), and a file tools run on their own. `/prized-pull` refuses to apply such changes until `--all` and shows their diff, capped at 200 lines.
- `--all` applies only the patch the last review showed. A sha256 of the patch bytes is recorded on your machine, in `$XDG_STATE_HOME/opencode-prized/reviewed.json`, never in the sandbox. If the agent changes anything after the review, `--all` shows a new review instead of applying.
- `--branch` runs every git command with `core.hooksPath=/dev/null` and commits with `--no-verify`. A pulled `.husky/pre-commit` or commitlint config never runs on your machine.
- Upload refuses a file that contains your `PRIZED_API_KEY`, such as an `.envrc` git does not ignore.
- `src/sandboxes.ts` holds one sandbox per root session:
  - it reconnects by label, forks on an inherited record, or creates and uploads;
  - the `Power` chain (pause and resume one at a time, each checking state first) is pi's;
  - a gone sandbox gets an error that says so, and the next call starts fresh;
  - it warns 10 minutes before the run time ends.
- `src/paths.ts`: the model passes paths under your project (OpenCode tells it the local path), and they map to `/home/prized/work/<name>`. `~` maps to `/home/prized`, sandbox paths pass through, and anything else is a path inside the sandbox. Results show project paths as the local path, so the model and the TUI stay consistent.
- `src/tools.ts` holds the tool bodies. `bash` streams through the SDK's exec. `read` uses OpenCode's output format and limits. `edit` matches with OpenCode's error texts and its whitespace-tolerant fallbacks (`src/edit.ts`). `glob` uses git's file list and `grep` uses `git grep -P`, both skipping ignored files. `apply_patch` has its own parser (`src/patch.ts`).

## Keys and secrets

- The key comes only from `PRIZED_API_KEY` in the environment of the OpenCode process, on your machine. It is never read from `opencode.json`: a project's config is checked in. The options parser refuses `apiKey`, `baseUrl` and `edgeUrl` and points at the environment.
- `PRIZED_BASE_URL` and `PRIZED_EDGE_URL` are environment-only too. A URL in a cloned repo's config could otherwise send your key elsewhere.
- Commands in the sandbox get no environment from your machine. OpenCode's own shell tool passes `...process.env` (`tool/shell.ts` L413-426); the replacement does not. In the live test, `env | grep -c PRIZED_` in the sandbox printed 0.
- **The live test kept our long-lived key out of the sandbox that ran OpenCode.** Prized mints API keys only from a dashboard session (prized-dev/doppel `docs/specs/api-keys.md` §3), so a throwaway or short-lived key could not be made for the test. A box-limited key cannot reach sandboxes at all (K9). The test therefore used a broker instead:
  - `test/live/relay.mjs` runs in the keyless sandbox A. The plugin's SDK points at it (`PRIZED_BASE_URL=http://127.0.0.1:4021`, `PRIZED_EDGE_URL=.../__edge`) with the placeholder key `dcp_relay_placeholder_not_a_key`, and the relay drops the `Authorization` header.
  - `test/live/broker.mjs` runs on the machine that holds the key and pulls the queued requests through A's own preview URL. That URL needs the key in `Prized-Key`, and the edge strips the header: the relay's record of received headers shows no `prized-key` and no `authorization`. The broker adds the key and calls Prized.
  - The broker allows only what the plugin needs, and only for sandboxes this run created or forked:
    - create, which must carry `created-by=opencode-prized`, gets a run label, and is capped at 2 vCPU, 4 GB and 2 h;
    - a list narrowed to the run label;
    - get, pause, resume, fork and delete;
    - exec and files on the edge.
    Everything else gets 403. A probe of `GET /api/v1/me` from A got 403.
  - Hand-offs are at-least-once with dedupe by job id. The preview edge rate-limits bursts (429), so replies retry.
  - The broker learns sandbox ids only from the API's own create, fork, get and list answers, never from the edge, whose answers carry what the sandbox wrote.
  - It sends the key only to `https://*.prized.dev`, and allows only the `process/` and `files/` edge routes. It refuses `..` and encoded slashes, rebuilds the list query from label selectors, and forwards only an allowlist of headers.
- The same relay pattern could let an agent in a sandbox drive other sandboxes without holding a key. It is test scaffolding here, not a product feature.
- **Scoped keys replace the broker** where the Prized API offers them. `src/scoped.ts` (`mintSandboxKey`, exported as `opencode-prized/scoped`) runs on the machine that holds the long-lived key and mints a key that lives 4 hours, does only what the plugin does (`read`, `create`, `exec`, `pause`, `resume`, `fork`, `delete`) and reaches only the sandboxes it makes, their forks and any selected by `labels`. Only that key goes into the sandbox that runs OpenCode, whose plugin uses it like any key.
  - It fails closed: an installed `@prized/sdk` without `Sandbox.scopedKey`, or a 404 from the mint (scoped keys off, or an API from before them), is `{ available: false }` and nothing is minted. Any other refusal throws. An answer that carries the long-lived key back is refused.
  - `test/live/scoped-run.mjs` runs the live test this way, with no relay and no broker: it mints the key, creates the runner with only that key, runs `e2e.mjs` with `E2E_KEY=scoped` (the relay checks read the API instead, plus: the key is scoped, it gets 403 for boxes, secrets and minting, it lists no sandbox it did not make), then deletes what the key made, revokes it and checks it is refused. `test/scoped.test.ts` covers the helper offline against a stand-in for the API.

## Tests

- **Offline** (`npm test`, 62 tests):
  - unit tests: paths, glob, edit, patch, config, the patch-header parser, the review list;
  - the tools against a fake sandbox that runs the real git, tar and bash commands;
  - the OpenCode 1 plugin through its hooks, with a fake client;
  - the OpenCode 2 setup against a stand-in Context;
  - the review fixes: a symlink hidden behind an index-line mode, a CR in a header, an unlisted unified-diff section; a bad option fails closed; a file holding the key stops the upload; `--all` refuses a tree it did not review; an executable on your machine counts whatever the patch says; `--branch` runs no hooks; background shell; reads outside `/home/prized`; non-UTF-8 files are left alone.

  `npm run typecheck` checks the OpenCode 1 plugin against the real `Hooks`, `PluginInput` and `PluginModule` types of `@opencode-ai/plugin` 1.19.5 (`test/api-types.ts`).
- **Live, OpenCode 1.19.5** (`test/live/e2e.mjs`): **30 of 30**. The packed plugin ran in the keyless sandbox A with a scripted model (`mock-model.mjs`; no model key), driving a second sandbox through the broker.
  1. The first turn (sandbox create, upload, 9 tool calls) took 8 to 9 s:
     - the model was offered one `bash`, with only `command` required;
     - bash ran in `/home/prized/work/app`, and no `PRIZED_` variables reached it;
     - a server started in the background;
     - edit, write, read, glob and grep worked, with local paths in their results;
     - `.env` was not uploaded;
     - the checkout was untouched.
  2. On exit the sandbox was kept paused with its changes, and OpenCode printed how to get them.
  3. A new `opencode run --session` reconnected, and the server from turn 1 still answered HTTP 200.
  4. In `opencode serve`, the sandbox paused by itself 4 s after the session went idle. The next turn resumed it, with the server alive.
  5. `--fork` forked the sandbox, with the server alive in the copy. The parent did not see the fork's file.
  6. `/prized-pull` held back, naming "a change to an executable file: bin/run (read the diff)" and "an executable: deploy.sh", and showed their diff. `--all` applied everything unstaged, with the exec bit kept. On exit, the parent sandbox, with nothing left to pull, was deleted.
  7. Relay calls had a median of 30 ms, and none were denied.
- **Live, OpenCode 2.2.5** (`test/live/e2e-v2.mjs`, `opencode run --standalone`): **14 of 14**.
  1. OpenCode offered its own `shell`, `read`, `glob` and `grep`, once each, with our executors behind them.
  2. Turn 1 ran 9 tools in the sandbox, with no host env and no `.env`.
  3. A new process reconnected, with the server still up.
  4. `--fork` forked the sandbox, and the parent stayed clean.
  5. `/prized-pull`, through `opencode api session.command`, held back with the diff, then applied with `--all`.
- Independent check from outside, after each run: `prized sandbox ls --label oc-e2e=<run>` showed only the fork's sandbox left, paused.
- **Not tested:** macOS and Windows hosts, and the interactive TUIs. They render the same tool parts and prompts that `run` and `serve` produce.
