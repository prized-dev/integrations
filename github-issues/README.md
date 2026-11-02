# Label a GitHub issue, and your agent works it on your Prized box

`prized_issues.py` is a small poller you run on a [Prized box](https://prized.dev/docs/boxes?via=integrations). Add the `prized` label to an issue and, within about a minute, the box runs your own Claude Code or Codex on it in a fresh git worktree, pushes a branch and opens a pull request that closes the issue. Comment `/prized <request>` on that pull request and the same agent session continues on the same worktree, with everything it already read and changed.

It is not a GitHub App: nothing is installed on GitHub, and nothing runs anywhere but your box. The agent uses the sign-in already on the box, so runs count against your Claude or ChatGPT plan, and the box bills like any other.

Step-by-step guide: [prized.dev/docs/github-issues](https://prized.dev/docs/github-issues?via=integrations).

## Set it up

1. A box that stays awake (a sleeping box checks nothing). A Nano is enough for most repos. A box used only for this keeps your other sign-ins out of the agent's reach. On your laptop:

   ```sh
   prized box auto-pause BOX off
   prized agents handoff claude      # or codex
   ```

2. A fine-grained GitHub token for just this repo, with **Contents**, **Issues** and **Pull requests** set to read and write. [This link](https://github.com/settings/personal-access-tokens/new?name=prized-issues&expires_in=90&contents=write&issues=write&pull_requests=write) fills in the permissions; pick "Only select repositories" and the repo. Leave out **Workflows**, so GitHub itself refuses any push that changes `.github/workflows`.

3. On the box (`prized ssh BOX`):

   ```sh
   curl -fsSLO https://raw.githubusercontent.com/prized-dev/integrations/main/github-issues/prized_issues.py
   python3 prized_issues.py watch OWNER/REPO
   ```

   It asks for the token, creates the `prized` label, clones the repo into `~/.prized-issues/repos/` and starts a systemd user timer that polls every minute.

4. Label an issue `prized`.

## Commands

| On GitHub | What happens |
| --- | --- |
| Add the `prized` label | A run: comment "working on this", the agent in `~/.prized-issues/work/OWNER__REPO-N` on branch `prized/issue-N`, then a pull request with `Closes #N` |
| `/prized <request>` on the pull request (or the issue) | The same session continues (`claude --resume`, `codex exec resume`) on the same worktree; the new commit is pushed to the branch. If someone else pushed to the branch, the box builds on it, edits only, and holds the result |
| `/prized push` | Pushes a held change (below) and opens the pull request |
| `/prized drop` | Throws a held change away; the label then starts fresh |
| `/prized` | A short help comment |
| Remove and re-add the label | A fresh run, once the issue's pull request is closed; while it is open, a pointer to it |

| On the box | What it does |
| --- | --- |
| `python3 ~/.prized-issues/prized_issues.py` | What it watches and the last 20 runs |
| `python3 ~/.prized-issues/prized_issues.py watch OWNER/REPO --members alice,bob` | Add a repo (each with its own token), or change who counts; `--agent codex`, `--label` and `--base` too. Running it again changes only what you name |
| `python3 ~/.prized-issues/prized_issues.py poll --dry-run` | Which labeled issues would run, and why the others don't |
| `python3 ~/.prized-issues/prized_issues.py off` | Stop the timer (`off OWNER/REPO` stops one repo) |

Logs are in `~/.prized-issues/logs/`, one per run. A repo with `.prized/setup.sh` on its base branch gets that script run in each new worktree (installs, a build) before the agent starts.

## Replay

Optional: every pull request links a replay of the run, the agent's steps, every command it ran with its exit code, and the files it changed, on one timeline. Secrets are masked on the box, and the link opens only for members of your Prized workspace.

1. On the box, beside `prized_issues.py`:

   ```sh
   curl -fsSLO https://raw.githubusercontent.com/prized-dev/integrations/main/github-issues/prized_replay.py
   python3 prized_issues.py watch OWNER/REPO --replay
   ```

2. On your laptop, share the replay server's port with your workspace (or click **+** in the box page's **Ports** row):

   ```sh
   prized --box BOX ports share 7707
   ```

3. Back on the box, give it the link that printed:

   ```sh
   python3 ~/.prized-issues/prized_issues.py watch OWNER/REPO --replay https://BOX-7707-xxxxxx.p.prized.dev/
   ```

Each pull request then starts with **▶ Replay this run** and three lines: files changed, commands run, and the exit code of the agent's last test run as the box saw it, so `pytest | tail` can't hide a failure. Follow-ups, held changes and runs that changed nothing get their own. `--replay off` turns it off.

- **How it records.** `prized_replay.py` runs the agent under ptrace, which reports each process it starts, its command line and its exit code, and nothing in between, so the agent runs at full speed. The agent's own event stream gives its steps; git gives the diff. Setuid programs such as `sudo` start untraced, so they keep working.
- **What's masked.** Before anything is written: the values of secret-looking variables in every process the agent started, the worktree's `.env` files, the usual credential files in your home directory (`~/.claude`, `~/.codex`, `~/.config/gh`, `~/.netrc` and the like), this poller's GitHub tokens, and anything shaped like a key or token, in plain text, base64, JSON or URL form. A page that would still hold one isn't written, and the pull request says so.
- **Where it lives.** One page per turn in `~/.prized-issues/replays/`, kept 30 days, served on the box's `127.0.0.1:7707` by the `prized-issues-replays` systemd user service, with no network access from the page.
- **What reaches GitHub** is fixed words and numbers only: the link, file names, counts, and the test runner's name (`pytest`, `npm test`), never a command line.
- **Processes the agent leaves running end with its turn,** so nothing from the run can change the replay after it's checked. A dev server the agent started isn't there for the follow-up.
- **What it costs.** Under a second of CPU for a typical turn. A turn that starts thousands of processes takes up to twice as long for that stretch, about 1 ms per process.
- **What doesn't work while recording:** tools that trace processes themselves (`strace`, `gdb`, LeakSanitizer in ASan builds), a setuid program a program starts by exec'ing it directly instead of forking (Python's `os.execv("/usr/bin/sudo", ...)`), and `kill -STOP` (the process keeps running). Turn replays off for a repo that needs these.
- **A box that doesn't allow ptrace** still gets replays of the agent's steps; the pull request says the exit codes are as the agent reported them.

## Who can start a run

- **Members only.** The token's owner, plus anyone named with `--members`, matched by GitHub user id. A label or a `/prized` comment from anyone else is ignored, write access or not, so a collaborator can't spend your plan or run commands on your box.
- **Members' own text runs in full auto** (`claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox`), so the agent can install, build and run tests. Label only issues you'd paste into the agent yourself.
- **Text anyone else wrote, edited or retitled runs edits only,** in Claude Code's `--restricted` mode: no commands, no web fetches, no skills or subagents, file tools confined to the worktree, no settings or MCP servers from the repo. The issue text goes in as data, not instructions. The change is committed on the box and held: the poller comments the list of changed files, and nothing is pushed until a member comments `/prized push` (or `/prized drop`). A session that started this way stays edits-only for its follow-ups. Codex has no such mode (its sandbox runs commands and reads the whole disk), so with `--agent codex` outside text doesn't run at all.
- **A follow-up on a branch someone else pushed to runs edits only too,** and is held the same way: anyone with write access can push to `prized/issue-N`, and nothing says who did. Once a member has pushed the held change, the next turn has full permissions again.
- **An edit history too long to check counts as outside text,** and so does an issue GitHub can't fully answer for.
- **An issue changed after the label went on doesn't run.** Remove the label and add it again to run it as it reads now. Edited `/prized` comments don't count either.

## What it checks before every push

Every commit not yet on GitHub is checked, and the push is refused (with a comment saying why) if it changes `.github/workflows`, adds a symlink or a submodule, adds a file over 5 MB, or holds this poller's token or something shaped like a GitHub, Anthropic, OpenAI, AWS or Prized key or a private key, in a file's content (binary files and files marked `-diff` included), its name or a commit message. Commits are made with git hooks off. Pushes never force.

## Limits

- **The token is on the box.** Each repo's sits in `~/.prized-issues/tokens/` (mode 0600) and is never in the agent's environment, and an edits-only run can't read it, but a full-auto run on members' text runs as you and could. That is why it should reach only this repo. A GitHub App would keep it off the box; this poller doesn't.
- **Full auto runs the repo's own code:** its tests, `.prized/setup.sh`, and Claude Code or Codex settings in the repo. Anyone who can push to the base branch can therefore run code on the box when a member labels an issue. Protect the base branch.
- **A held change is untested.** Edits-only runs can't run the tests. Read the diff on the box (the comment gives the `git diff` command) before `/prized push`.
- **One run at a time per box.** A follow-up or a new label waits for the run in progress.
- **Up to about a minute before a run starts** (the poll), then the agent's own time. Each turn stops after 2 hours.
- **Comments only.** `/prized` in a pull request review or an inline review comment isn't read.
- **Pull requests carry a footer** linking to the guide ("Opened by prized-issues"); delete it from the pull request if you'd rather not have it.

## Tests

```sh
scripts/test.sh github-issues
```

Offline: a stand-in for the GitHub REST and GraphQL calls on loopback, a local bare repo for the git side, and stand-ins for `claude`, `codex` and `systemctl` (`tests/bin`). They cover label to pull request, follow-ups resuming the same session, members and non-members, held changes and `/prized push`, edits after the label, refused pushes, setup output kept out of commits, and a branch someone else pushed to or rewrote. `tests/test_replay.py` covers replays: the pull request lines, an env var, a `.env` file and a token echoed to the terminal that never leave the box, a hostile turn, `sudo` under the recorder, leftovers, timeouts, a box without ptrace, and the replay server.
