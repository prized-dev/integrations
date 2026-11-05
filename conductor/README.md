# Move from Conductor to a Prized box

One command puts a [Conductor](https://www.conductor.build) repo on a [Prized box](https://prized.dev/docs/boxes?via=conductor) and sets it up the way Conductor set up each workspace: a git worktree per branch, the repo's setup script run in each with Conductor's variables, and Claude Code or Codex started in each in tmux, so the agents keep working with your laptop closed.

Conductor Cloud goes read-only on November 13, 2026 and shuts down on November 20. The full guide is [Moving from Conductor Cloud](https://prized.dev/docs/moving-from-conductor-cloud?via=conductor).

## Run it

1. Sign up at [prized.dev](https://prized.dev/sign-in?via=conductor). The tour starts a box on $30 of free credit and gives you one command that installs the `prized` CLI and signs this machine in.

2. In your repo's main checkout on your Mac (pushed to GitHub), run:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/prized-dev/integrations/main/conductor/move.sh | bash
   ```

   That makes two workspaces on new branches, `ws-1` and `ws-2`. To bring the branches your Conductor workspaces were on instead, name them:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/prized-dev/integrations/main/conductor/move.sh | bash -s -- -w auth -w billing
   ```

3. Open an agent with the `prized ssh -s claude-auth` line it prints, and leave with Ctrl-b, then d.

It asks before it copies anything secret: prized setup's own questions (your environment, `.env` files, gh), then the gitignored files Conductor copies. `-y` answers yes to all of them. Re-running it is safe: workspaces already set up stay as they are, and a setup that failed runs again.

## What it does

| In Conductor | On the box |
| --- | --- |
| A workspace per task, on its own branch | A git worktree per branch at `~/code/<repo>-<branch>`, beside the clone at `~/code/<repo>`. A branch origin has is checked out; a new one starts from origin's default branch, as Conductor's do |
| `scripts.setup` in `.conductor/settings.toml`, or `conductor.json` | Run once in each new worktree, in a login shell, logged to `~/.local/share/prized-conductor/<repo>/<branch>-setup.log` |
| `CONDUCTOR_WORKSPACE_NAME`, `_PATH`, `CONDUCTOR_ROOT_PATH`, `CONDUCTOR_DEFAULT_BRANCH`, `CONDUCTOR_IS_LOCAL` | Set for the setup script, the agent and every shell in the workspace's tmux sessions |
| `CONDUCTOR_PORT`, 10 ports per workspace | 4100 to 4109 for the first workspace, 4110 for the next, unique across the box's repos. Box ports from 3000 to 9999 open at localhost on your Mac ([Ports](https://prized.dev/docs/ports?via=conductor)) |
| `[environment_variables]` and its `.local` table | Set in each workspace the same way |
| Files to copy: `.worktreeinclude`, `file_include_globs`, or `.env*` | The matching gitignored files are copied from your Mac's checkout to the clone, then into each new worktree; `node_modules` and virtualenvs stay out |
| `.conductor/settings.local.toml`, and your `~/.conductor/settings.toml` | Copied from your Mac and read the way Conductor reads them: the local file over the repo's settings, yours under them |
| The Run button (`scripts.run`) | `--run` starts the default action in a tmux session `run-<branch>` on the workspace's port; `run_mode = "nonconcurrent"` starts only one |
| Archive (`scripts.archive`) | `--archive BRANCH` runs it, stops the workspace's sessions, and removes the worktree; the branch stays |
| Conductor Cloud's repository setup script and Cloud Computer install script | `--setup FILE` and `--install FILE`, from the copies you saved. With `--setup`, scripts see `CONDUCTOR_IS_LOCAL=0`, the workspace as `CONDUCTOR_ROOT_PATH`, and the `.cloud` variables, as in a cloud workspace. A setup script you set in Conductor's settings on your Mac goes in with `--local-setup FILE` |
| Node 24, npm and pnpm on every Cloud machine | When the repo has a `package.json` and the box has no Node, Node (the version in `.nvmrc` or `.node-version`, else 24) through fnm, and pnpm or yarn through corepack. With a `Gemfile`, Ruby (the version in `.ruby-version`) through mise, with the libraries common gems build against. Bun and uv when the repo's lockfile or setup script names them |

Before the worktrees, `prized setup --clone` clones the repo onto the box from its origin and signs in your agent and, if you let it, gh, so the agents can push and open pull requests. It also offers to bring your environment (Node, Python, CLIs) and the project's `.env` files ([Bring your environment](https://prized.dev/docs/environment?via=conductor)).

## Options

| Option | What it does |
| --- | --- |
| `REPO` | Your checkout (default: this folder), or `owner/repo` to clone on the box without one; then nothing is copied from your Mac |
| `-w BRANCH` | A workspace on BRANCH; repeat it |
| `-n N` | N new workspaces when no `-w` is given (default 2) |
| `--agent claude\|codex\|none` | What starts in each workspace (default `claude`, in auto mode; Codex starts in its `workspace-write` sandbox, never asking) |
| `--run` | Start each workspace's run script too |
| `--setup FILE`, `--install FILE` | Your saved Conductor Cloud scripts |
| `--local-setup FILE` | A setup script from Conductor's settings on your Mac |
| `--files`, `--no-files` | Copy the gitignored files without asking, or never |
| `--box NAME` | A box other than your current one |
| `--port-base N` | Where `CONDUCTOR_PORT` starts, 3000 to 9990 (default 4100) |
| `--archive BRANCH` | Archive a workspace; with uncommitted changes it stops, unless `--force` |
| `-y` | Yes to every question |
| `-- ARGS` | Passed on to `prized setup` |

## Limits

- **Push first.** The box clones from origin, so a `-w` branch with commits only on your Mac stops the move and says what to push. Uncommitted changes stay on your Mac.
- **The box is Ubuntu 24.04 (x86_64).** Setup scripts that call Homebrew, Xcode or other Mac-only tools fail there; the log says where. Conductor Cloud's install scripts use `dnf`, which becomes `apt-get` (package names can differ).
- **Cloud environment variables and secrets don't come along:** Conductor hides their values. Set them in **Variables** on the box page's **Settings** tab, and API keys in [Secrets](https://prized.dev/docs/credentials?via=conductor#secrets-for-agents).
- **Chats don't carry over.** `prompts.general` is not passed to the agents: put it in `CLAUDE.md` or `AGENTS.md`.
- **Git LFS** is not on a fresh box: it says so when the repo uses it.
- **GitHub only** for `owner/repo`; a checkout with another origin works through `prized setup --clone`.

## Tests

```sh
scripts/test.sh conductor   # from the repo root
```

The offline tests run `move.sh` against a stand-in `prized` whose box is a folder, a stand-in `tmux`, and a local bare repo: no network, no keys.
