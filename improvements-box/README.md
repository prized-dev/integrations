# An improvements box on Prized

A suggestion box for your app whose suggestions a coding agent builds. Each suggestion gets a [Prized sandbox](https://prized.dev/docs/sandboxes?via=improvements-box) of its own, a Firecracker microVM with Claude Code in it. The agent makes the change there. Your tests run in a fork of that sandbox taken before the agent started, on the patch alone. If they pass you get a draft pull request, or a patch on disk. A person reviews and merges. Both sandboxes are deleted at the end.

It's one Python file, `improvements_box.py`, using the standard library and the `prized` SDK. Copy it into your own project and change what you like.

## Run it

1. Make a Prized API key with **Sandboxes: Write** at https://prized.dev/dashboard/workspace/api-keys?via=improvements-box. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=improvements-box.

2. Get the box and the SDK:

   ```bash
   git clone --depth 1 https://github.com/prized-dev/integrations
   cd integrations/improvements-box
   pip install prized
   ```

3. Export the two keys. The model key is the only one that goes into a sandbox, so use one made for this, with a spend limit, that you can revoke:

   ```bash
   export PRIZED_API_KEY=dcp_…
   export ANTHROPIC_API_KEY=sk-ant-…   # or CLAUDE_CODE_OAUTH_TOKEN, from `claude setup-token`
   ```

4. Try it on the demo app, a temperature converter with three tests:

   ```bash
   cp -r demo-app /tmp/temps && cd /tmp/temps && git init -q && git add -A && git commit -qm temps && cd -
   python improvements_box.py --repo /tmp/temps --test "python3 -m unittest -v"
   ```

   Open http://127.0.0.1:8080 and suggest "Support Kelvin too, like 300K". The page shows each step as it happens. When the tests pass, the patch is in `improvements/` and both sandboxes are gone.

5. On your own repository, with draft pull requests (this needs `git push` access and `gh` signed in on this machine):

   ```bash
   python improvements_box.py --repo https://github.com/you/app --setup "npm ci" --test "npm test" --mode pr
   ```

An app can send suggestions from its own box: `POST /api/suggestions` with `{"text": "…"}`, and `GET /api/suggestions` returns every run with its steps.

## What happens to a suggestion

1. **Snapshot.** This machine clones the repository with its own git credentials and makes a `git archive` of the base branch's last commit.
2. **Sandbox.** A new sandbox from `prized-agent-v1` (2 vCPU and 4 GB unless you say otherwise), labelled `app=improvements-box`, `suggestion=s-…` and `role=agent`. Its run time is capped, so Prized deletes it even if this process dies.
3. **Copy in.** The snapshot is unpacked into `~/work/repo` and committed as the base. `--setup` runs next, if you gave one, and what it changes joins the base.
4. **Fork.** The sandbox is forked into a clean copy (`role=tests`) and the copy is paused, so it costs no compute while the agent works. If a fork isn't possible, a fresh sandbox is set up the same way.
5. **Agent.** Claude Code runs headless (`claude -p --dangerously-skip-permissions`) with the suggestion wrapped in a prompt that says it comes from an anonymous user. It stops at `--agent-minutes` or `--max-budget-usd`, whichever comes first. The page streams what it does.
6. **Check.** The change comes out as one patch, and this machine judges the patch itself. No change ends the run. A patch is refused if it doesn't apply to the base, touches a protected path (`.github/` by default, renames and odd names included), holds the model key, or is over 512 KB.
7. **Tests.** The agent's sandbox is deleted (or paused, with `--keep pause`), so nothing it left running carries on. The clean copy resumes, the patch is applied there, and your `--test` command runs, without the model key. Nothing the agent left in its own sandbox (a changed `PATH`, a process in the background, a rewired git) can reach them. If they fail, nothing is handed over.
8. **Hand-off.** Local mode writes `improvements/s-….patch` and a JSON record of the run. PR mode commits the patch on this machine, pushes `improvements/s-…` and opens a draft pull request that quotes the suggestion.
9. **Clean up.** The clean copy is deleted too. With `--keep pause` the agent's sandbox stays paused, so you can look inside (`prized sandbox exec <id> --cwd repo -- git diff --cached improvements-base`), and deleted after `--keep-hours`.

## What it costs

Measured on the demo app on 2026-11-01, with "Support Kelvin too, like 300K", `--model sonnet` and the default 2 vCPU and 4 GB. It took 20 seconds from submit to patch, 12 of them the agent's. Claude Code reported $0.05 of model use ($0.04 to $0.05 over five runs). The two sandboxes ran about 21 seconds between them, under $0.001 at [sandbox rates](https://prized.dev/docs/billing?via=improvements-box#sandboxes). Bigger repositories and harder suggestions take longer, within the caps.

## Suggestions are untrusted input

Whoever can type into the box is giving instructions to an agent that runs code. Treat every suggestion as hostile and the agent as one that may be misled. What the template does about it:

| Guardrail | How |
| --- | --- |
| A sandbox per suggestion | A new microVM with its own kernel each time, deleted at the end. Nothing carries over from one suggestion to the next. |
| Time caps | `--agent-minutes` stops the agent and `--test-minutes` the tests. Each sandbox's own run time cap deletes it if this process goes away, and a paused leftover is deleted within the hour. |
| A spend cap | Claude Code's `--max-budget-usd`, per suggestion. Set a spend limit on the key itself too. |
| Only the model key goes in | The model key is passed to the agent's command and to nothing else, not even the tests. The repository goes in as a snapshot, and the push and the pull request happen on this machine, so your Prized key and git credentials never enter a sandbox. `--model-env` refuses `PRIZED_*`, `GH_*`, `GITHUB_*` and `GIT_*`. |
| The key stays out of what comes back | A change that contains the model key as written is refused, and the key is masked in the agent's log, its summary and the test output. |
| Protected paths | A change under `.github/`, or any `--protect` prefix, is refused before the tests run, so a suggestion can't add a workflow. The check reads the patch on this machine, so a rename or a quoted file name doesn't slip past it. |
| Tests in a clean copy | The agent saying its tests pass doesn't count. Your command runs in a fork from before the agent started, on the patch alone. |
| Nothing from the patch runs here | Every git command on this machine, the ones `gh` runs included, has hooks and fsmonitor turned off, so a hook the agent adds to the repository can't run on your machine. |
| Draft pull requests only | Nothing is merged or deployed. The pull request says the change came from an untrusted suggestion, and a person decides. |
| A careful form | It listens on 127.0.0.1, answers only to localhost names and the ones you give with `--allow-host` (no DNS rebinding), refuses posts from other sites, takes up to 2,000 characters, and holds at most `--queue` runs. |

What it can't stop:

- **The agent can read its own key,** and sandboxes have open outbound internet, so a misled agent could send the key somewhere, or encode it past the check above. That's why it should be a key with a low spend limit that you can revoke.
- **The agent can change your tests** as well as your code. Read the whole diff.
- **Your CI runs the change.** A pushed branch runs your test code on your CI with whatever secrets your workflows give it. Keep secrets out of CI runs on `improvements/*` branches, or push to a fork.
- **No accounts.** If the form is reachable by anyone but you, put it behind your app's login and rate limits.

## Settings

| Flag | Default | What it does |
| --- | --- | --- |
| `--repo` | | Git URL or local path of the repository |
| `--test` | | The command that decides whether a change is good |
| `--setup` | | Runs before the agent, e.g. `npm ci` |
| `--branch` | the repository's default | The base branch |
| `--mode` | `local` | `local` writes patches to `--out` (`improvements`); `pr` opens draft pull requests |
| `--keep` | `delete` | `pause` keeps each sandbox paused for review, deleted after `--keep-hours` (24) |
| `--agent-minutes`, `--test-minutes`, `--setup-minutes` | 10, 5, 10 | Time caps |
| `--max-budget-usd` | 1 | The agent's spend cap per suggestion |
| `--model` | Claude Code's default | e.g. `sonnet` |
| `--template`, `--vcpu`, `--memory-gb` | `prized-agent-v1`, 2, 4 | The sandbox. [Your own template](https://prized.dev/docs/sandboxes-own-template?via=improvements-box) can add toolchains |
| `--protect` | `.github/` | Path prefixes the agent may not change (repeatable) |
| `--label` | | `KEY=value` on every sandbox (repeatable) |
| `--concurrency`, `--queue` | 2, 20 | Runs at once, and runs waiting or running at most |
| `--model-env` | `ANTHROPIC_API_KEY,CLAUDE_CODE_OAUTH_TOKEN,ANTHROPIC_BASE_URL` | Variables passed to the agent |
| `--host`, `--port` | `127.0.0.1`, 8080 | Where the page listens (`--port 0` picks a free one) |
| `--allow-host` | | A name the page is reached by besides localhost, e.g. behind your proxy (repeatable) |
| `--once "…"` | | Build one suggestion, print the run as JSON, and exit |

## Limits

- **Toolchains.** `prized-agent-v1` has Python 3.12, Node 22, git and build-essential, with no sudo and no Docker. A test command that needs more needs [your own template](https://prized.dev/docs/sandboxes-own-template?via=improvements-box).
- **No history.** The agent sees the base branch's files, not its `git log`.
- **GitHub for pull requests.** PR mode uses `git push` and `gh pr create`. Local mode works with any git repository.
- **Free tier:** sandboxes there are capped at 2 vCPU and 4 GB (the default here) and 60 minutes of running in all. A top-up or a paid plan lifts both.

## Tests

```bash
scripts/test.sh improvements-box
```

Offline, with no keys: fake sandboxes that run the pipeline's own commands with bash (the snapshot, the unpack, the fork, the diff, and the demo app's real tests), a fake Claude Code that makes a known change for each case, and a fake `gh`. They cover both modes, failing tests, no change, the time and spend caps, an oversized patch, pausing for review, a failed create, fork or delete, and shutting down mid-run. They also try the attacks: a protected path reached directly, by rename or by a quoted name; tests rigged to pass in the agent's sandbox; a git hook in the patch with this machine's hooks pointed into the repository; the model key in the change; and posts from other sites and host names. And they check that only the agent's command gets the model key.

Live, against a real sandbox and the real Claude Code (billed to your workspace and your model account; the sandbox is deleted at the end):

```bash
PRIZED_LIVE=1 PRIZED_API_KEY=dcp_… ANTHROPIC_API_KEY=sk-ant-… pytest tests/test_live.py
```
