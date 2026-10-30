# Bring your Jules export to a Prized box

Google's Jules runs sessions until November 11, 2026, then turns read-only and shuts down on December 31 ([Google's notice](https://jules.google/docs/changelog/2026-10-29)). `jules_import.py` takes the folder Google's export writes (`~/jules-export`) and sets up a [Prized box](https://prized.dev/docs/boxes?via=jules) the way Jules had it. A box is a cloud Linux machine that keeps its disk. Your scheduled tasks keep running there with your laptop closed, as Claude Code or Codex, and still open pull requests.

## Run it

1. **Export from Jules**, on your computer. Install Jules Tools and sign in:

   ```bash
   npm install -g @google/jules
   jules login
   ```

   Then open Claude Code (or another CLI agent) in your home folder and paste the export prompt from [Google's notice](https://jules.google/docs/changelog/2026-10-29). It writes `~/jules-export`.

2. **Get a box.** [Sign up](https://prized.dev/sign-in?via=jules); it starts a box on $30 of free credit, no card. Then install the CLI and sign it in:

   ```bash
   curl -fsSL https://prized.dev/install.sh | sh
   prized login
   ```

3. **Copy the export and your sign-ins to the box.** `BOX` is its name, from `prized box ls`. `--gh` also copies your GitHub CLI token and git name and email, which the box needs to clone private repos and open pull requests.

   ```bash
   prized cp -r ~/jules-export BOX:jules-export
   prized agents handoff claude --gh
   ```

4. **Run the importer on the box.** Do a dry run first: it prints the plan and changes nothing.

   ```bash
   prized ssh BOX
   curl -fsSLO https://raw.githubusercontent.com/prized-dev/integrations/main/jules/jules_import.py
   python3 jules_import.py ~/jules-export --dry-run
   python3 jules_import.py ~/jules-export
   ```

5. **Keep the box awake for scheduled tasks.** A paused box runs nothing. A run it missed starts once when the box wakes. To run on time, from your computer:

   ```bash
   prized box auto-pause BOX off
   ```

   An awake Nano box costs $9.86 for 30 days ([what a schedule costs](https://prized.dev/docs/run-claude-code-on-a-schedule?via=jules#what-it-costs)).

`systemctl --user list-timers 'jules-*'` lists the next runs. `systemctl --user start jules-NAME` runs one now. Logs go to `~/.jules-import/logs`.

## Label an issue `jules`, get a pull request

Jules took a task from any GitHub issue you labeled `jules`. This brings that back, with or without an export. On the box, after `prized agents handoff claude --gh` from your laptop (it copies your Claude Code and GitHub sign-ins):

```bash
curl -fsSLO https://raw.githubusercontent.com/prized-dev/integrations/main/jules/jules_import.py
python3 jules_import.py issues OWNER/REPO
```

It clones the repo, creates the `jules` label if the repo lacks it, and checks for labeled issues every 2 minutes. Add the label to an issue, from the GitHub app if you like. The box comments on the issue, runs Claude Code (or Codex, with `--agent codex`) on it in a fresh worktree, the way scheduled tasks run, and opens a pull request that closes the issue. If nothing changed or the run failed, it says so on the issue.

- **Who can hand it work.** Only labels you added, or that someone with write access added, count. A label from an issue template or a triager is skipped. Labels added before you ran `issues` don't count, so issues Jules already worked on stay as they are. To run an issue again, remove the label and add it back.
- **What the agent reads.** It gets the issue's title and text as github.com shows them, so HTML comments and invisible characters stay out. If the title or text changed after the label went on, the issue doesn't run, and the box comments so; add the label again to run it as it reads now.
- **One at a time.** Each check runs at most one issue, for up to 3 hours, and the next check takes the next one. If the repo was imported from your export, its setup script and variables are used too.
- **Keep the box awake.** A paused box checks nothing: `prized box auto-pause BOX off`.
- `python3 ~/.jules-import/jules_import.py issues` lists what ran. `issues --off` stops it. Logs are in `~/.jules-import/logs`.

## What goes where

| In the Jules export | On the box |
| --- | --- |
| `sources.json`, the connected repos | Each repo with Jules settings or sessions is cloned into `~/code/REPO`. `--all-repos` clones the others too, and `--repos owner/a,owner/b` picks repos by name |
| `repos/*/setup.sh` | Run once in the clone. Runtimes the Jules VM had but the box lacks (Node.js 22, Bun, Go, Rust, uv) are installed first, for your user only. It runs again in each scheduled run's fresh worktree, as Jules ran it per task |
| `env-variables.json` | `~/.jules-import/env/OWNER__REPO.env`, mode 600, outside the repo, loaded for setup and scheduled runs. The importer prints names only, never values. To use them in a shell: `set -a; . FILE; set +a` |
| `repos/*/memories.json`, `test-command.txt` | A block per repo in `~/.claude/CLAUDE.md` (Claude Code) and `~/.codex/AGENTS.md` (Codex). No repo file changes. Archived memories are left out |
| `scheduled-tasks.json` | For each task, a script `~/.jules-import/tasks/NAME.sh` and a systemd user timer in the task's time zone. Tasks paused in Jules are written but left off. A run makes a fresh git worktree from the task's branch, runs the setup script, then runs `claude -p --dangerously-skip-permissions` (or `codex exec --dangerously-bypass-approvals-and-sandbox` with `--agent codex`) with the task's prompt. If the agent changed anything, the script makes one commit with the agent's message, pushes a `jules/NAME-DATE` branch and opens a pull request with `gh`. What the setup script left in the worktree is never committed unless the agent changed it again. A run that fails keeps its worktree in `~/.jules-import/runs` for a week |
| `sessions/*` | `~/.jules-import/sessions.md` lists every session, unfinished ones first, with the branch and `summary.md` of each. To pick one up, ask your agent in the repo to read that summary and finish the work. Its patches are in the same folder |
| `user-settings.json` | Not used |

Running the importer again updates clones, files and timers in place. It keeps variables you added to the `.env` files, keeps a task's name when it was renamed in Jules, and leaves a task script you edited alone (its new version goes next to it as `NAME.sh.new`).

## Or hand it to your agent

Paste this into Claude Code or Codex on your computer:

```text
Move my Jules setup to my Prized box BOX. Copy ~/jules-export there with `prized cp -r ~/jules-export BOX:jules-export`, then ask me to run `prized agents handoff claude --gh` myself. On the box (`prized exec BOX -- ...`), download https://raw.githubusercontent.com/prized-dev/integrations/main/jules/jules_import.py, run `python3 jules_import.py ~/jules-export --dry-run`, show me the plan, and run it without --dry-run when I say so. Then go through its "Needs you" list with me. If it couldn't read a scheduled task's schedule, read that entry in ~/jules-export/scheduled-tasks.json and set it with `python3 ~/.jules-import/jules_import.py schedule NAME 'SPEC'`. Never print the values in env-variables.json.
```

## Limits

- **The export's format is unpublished.** `env-variables.json` and `scheduled-tasks.json` are raw responses from Jules' internal API, and Google publishes no schema for them. The importer finds variables by a name and a value, and tasks by a prompt and a schedule (cron, RRULE, or a frequency with a time, days and a time zone), wherever they sit in the file. When it has to guess a time or a day, or can't read a schedule, it says so under **Needs you**. To set a schedule by hand: `python3 ~/.jules-import/jules_import.py schedule NAME 'Mon *-*-* 09:00:00 America/New_York'`, or a cron line such as `'0 9 * * 1' --tz America/New_York`. Field names in `sample-export/` follow the documented parts of the format. The rest are our best reading.
- **GitHub only.** The importer clones from GitHub, as Jules did. Pull requests need `gh` signed in on the box.
- **Agent usage is on your plan.** Scheduled runs use your Claude or ChatGPT plan's limits, or your API key, not Jules' daily task quota. A long-lived token from `claude setup-token`, set as `CLAUDE_CODE_OAUTH_TOKEN` in `~/.jules-import/env/_all.env`, keeps unattended runs from needing a fresh `/login`.
- **Unattended means no permission prompts.** A scheduled run can run any command your user on the box can, the way Jules ran tasks in its own VM. To limit it, edit the agent line in the task's script, for example `claude -p "$PROMPT" --allowedTools "Bash(npm test)" Edit`.
- **An issue's text is the prompt.** Unlike Jules' VM, the box holds your GitHub sign-in, and the agent can read it. Label only issues whose text you would paste into Claude Code yourself, including on public repos where anyone can write the issue.

## Tests

```sh
scripts/test.sh jules
```

They run offline, against `sample-export/`, a local git remote, and stand-ins for `systemctl`, `gh`, `claude` and `codex` in `tests/bin`. They cover the schedule parsers, other shapes the export's files could take, a full import into a throwaway home folder, one scheduled run through to the pull request, and labeled issues: who may hand one over, re-runs, text that tries to break out of the script, and runs that change nothing or fail.
