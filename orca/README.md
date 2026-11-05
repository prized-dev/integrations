# Prized Sandbox for Orca

Run each [Orca](https://www.onorca.dev) workspace in its own [Prized sandbox](https://prized.dev/docs/sandboxes?via=orca). Orca reaches the sandbox over SSH and runs your agents there. Sleep the workspace and the sandbox pauses with its memory, so agents and processes are where you left them when you wake it.

Status: experimental. Tested against a stand-in for Orca 1.4.245 built from its published recipe contract, and against a real sandbox. Not yet run inside Orca itself.

## Set up

You need Orca 1.4.245 or later on macOS or Linux, Node.js 22 or later, Git, OpenSSH, and the `prized` CLI (0.21.9 or later).

1. Install the CLI and sign in. Shells in a sandbox open with your sign-in, never with an API key.

   ```bash
   curl -fsSL https://prized.dev/install.sh | sh
   prized login
   ```

2. Put an API key from the same workspace in your shell profile (`~/.zprofile` or `~/.bash_profile`), so Orca finds it even when you start it from the Dock. Make one at https://prized.dev/dashboard/workspace/api-keys?via=orca.

   ```bash
   export PRIZED_API_KEY=dcp_...
   ```

3. Run setup. It checks the CLI and the key, installs the adapter in `~/.prized-orca`, and asks before it adds one line to `~/.ssh/config`.

   ```bash
   npx -y github:prized-dev/orca-prized-plugin#v0.1.0 setup
   ```

4. In Orca, turn on **Settings > Experimental > Cloud VM**, then install the plugin: search **Prized Sandbox** in the community catalog, or use **Settings > Plugins > Install from git URL** with `https://github.com/prized-dev/orca-prized-plugin.git#v0.1.0`.

5. Create a workspace and pick **Prized Sandbox** under **Run on > Per-Workspace Environment**.

`~/.prized-orca/bin/prized-orca doctor` checks all of this without creating anything. It also names any sandbox the plugin made on this machine that no workspace uses (a create killed outright), so you can delete it.

## What happens

| Orca | The plugin |
| --- | --- |
| Create | Creates a sandbox (`prized-agent-v1`: Ubuntu 24.04 with Git, Node.js, Claude Code and Codex), uploads the exact commit Orca picked as a Git bundle (no Git credential goes in), checks it out on the workspace branch in `/home/prized/work/<repo>` with your Git name and email, opens a shell once with your login, and checks the SSH connection Orca will use. If a step fails, or you cancel in Orca, it deletes the sandbox. |
| Sleep | Pauses the sandbox. Its memory, processes and files are kept, and only storage is billed. Orca's reconnects are refused while it sleeps, so nothing wakes it by accident. |
| Wake | Resumes the sandbox and the same processes carry on. |
| Delete | Deletes the sandbox. |

While Orca is connected, the sandbox counts as in use and keeps running (and billing); sleep the workspace to pause it. Closing Orca stops nothing: with no connection open, the sandbox pauses itself after 30 idle minutes and keeps its memory, and the next connection from Orca resumes it.

## How the SSH connection works

Orca dials `prized@<sandbox id>` with your system `ssh`. Setup adds this line at the top of `~/.ssh/config` (and keeps a copy of the old file in `~/.ssh/config.prized-orca.bak`):

```
Include "/Users/you/.prized-orca/ssh_config"
```

That file holds one block per sandbox in use. It renews the sandbox's short-lived certificate before ssh reads it, refuses connections while the workspace sleeps, and includes the files `prized sandbox ssh` writes for that sandbox in `~/.config/prized/sandboxes/`. If `~/.ssh/config` is a link (a dotfiles repo), setup edits the file it points to.

`PRIZED_API_KEY` stays in memory. It goes to `api.prized.dev` and the sandbox's own command address in an `Authorization` header, and never into a file, a log, a command line, the `prized` CLI or the sandbox. `~/.prized-orca/lifecycle.log` records ids, timings and errors only.

## Settings

Pass them to setup (`setup --vcpu 4`), or edit `~/.prized-orca/config.json`. New workspaces use them.

| Setting | Default | Meaning |
| --- | --- | --- |
| `--template` | `prized-agent-v1` | Sandbox template (`prized sandbox templates`) |
| `--vcpu`, `--memory-gb`, `--disk-gb` | 2, 4, 10 | Sandbox size |
| `--auto-pause-min` | 30 | Pause after this many idle minutes; 0 turns it off |
| `--max-runtime-min` | 10080 | Delete after this many minutes of running (7 days); on a paid plan each wake restarts the clock. Refused above your workspace's limit, the plugin uses that limit instead |

## Limits

- The workspace's agents start signed out. Sign Claude Code or Codex in from the workspace's terminal the first time; each new workspace is a fresh sandbox.
- To push from a workspace, sign in to GitHub in its terminal (`gh auth login`, then `gh auth setup-git`).
- Orca installs its own server in each new sandbox on the first connection, which can take a few minutes.
- The connection carries no port forwarding, so Orca's port forwards don't reach a sandbox. `prized sandbox port <id> <port>` gives an https URL for a port instead.
- No root and no Docker: agents run as `prized`, without sudo.
- The repository's history has to bundle to 100 MB or less.
- Sandboxes run in Oregon (`us-west-2`).
- On the free tier a sandbox is 2 vCPU and 4 GB at most and runs 60 minutes in all, then it is deleted; paused time doesn't count ([Free tier](https://prized.dev/docs/sandboxes?via=orca#free-tier)). For agents that run for hours, add a Prized box as an SSH host instead: [Run Orca's agents on a Prized box](https://prized.dev/docs/orca?via=orca).
- Your `prized login` and `PRIZED_API_KEY` must be in the same workspace.
- macOS and Linux only.

## Uninstall

Delete the workspaces in Orca first (that deletes their sandboxes). Then remove the plugin under **Settings > Plugins**, and run:

```bash
~/.prized-orca/bin/prized-orca uninstall
```

It takes the `Include` line out of `~/.ssh/config` and removes the plugin's files.

## Tests

```bash
scripts/test.sh orca
```

The offline tests run Orca's side from a stand-in written from Orca's published recipe contract (`test/orca-harness.mjs`), against a stand-in for the Prized API on `127.0.0.1` and a fake `prized` CLI. The SSH side is real OpenSSH: `ssh` with the plugin's include, and a local `sshd` set up like a sandbox (a certificate authority for users, a host certificate and the forced command). Without `sshd` the lifecycle tests are skipped.

A live check creates one sandbox in your workspace, runs it through create, sleep, wake and delete, and checks that a process started before sleep is the same one after wake. It bills a few cents.

```bash
PRIZED_ORCA_LIVE=1 PRIZED_API_KEY=dcp_... node test/live.mjs
```
