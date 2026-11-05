# Prized for T3 Code

Run [T3 Code](https://github.com/pingdotgg/t3code)'s threads on a [Prized box](https://prized.dev/docs/boxes?via=t3code): its agents build and test on a cloud Linux machine and keep going with your laptop closed. `prized-t3` gets the box ready before you add it in T3 Code, and forks a box into another machine for work you want to keep apart.

## Install

You need the `prized` CLI, signed in, and T3 Code's desktop app, on macOS or Linux:

```bash
curl -fsSL https://prized.dev/install.sh | sh
prized login
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/prized-dev/integrations/main/t3code/prized-t3 -o ~/.local/bin/prized-t3
chmod +x ~/.local/bin/prized-t3
```

## Use

```bash
prized-t3 up          # your default box; or: prized-t3 up mango, or prized-t3 up --new
```

Then, in T3 Code: **Settings › Environments › Add environment › SSH**, and pick the box from the hosts it found in your SSH config. **Add project** › the box › **Local folder** › a path such as `~/code/myproject`. New threads in that project run on the box.

`up` does the box's side of that first, so the first connect works and is quick:

- wakes the box if it is paused (T3 Code's ssh gives a box 10 seconds, and a paused one refuses plain ssh);
- adds the box to your SSH config if it isn't there (`prized init --box`);
- installs T3 Code's server for your desktop app's version where T3 Code looks for it (`~/.t3/runtime/versions/<version>`, checked against the release's SHA256SUMS), so the first connect doesn't download it. The version comes from the app in `/Applications`, else the `t3` command, else the latest release; `--t3-version` picks one;
- allows the browser panel's Chrome sandbox and installs its fonts (`t3 browser setup`, with the box's passwordless sudo; Ubuntu 24.04 blocks the sandbox otherwise);
- shows whether Claude Code and Codex are on the login shell's `PATH`, where T3 Code looks for them, and signed in. If not, it asks (default no) whether to run `prized agents handoff claude codex --gh`, which copies your sign-ins and GitHub login from this computer;
- opens T3 Code (macOS).

`--auto-pause 2h` (or `off`) sets the box's idle pause. `prized-t3 check [BOX]` shows the same findings without changing anything.

## Another machine for a parallel task: `fork`

```bash
prized-t3 fork mango --name mango-2
```

The new box is a copy of `mango`'s disk: repos, dependencies, databases, sign-ins, and T3 Code's projects and threads. Two threads can then each run `npm run dev` on port 3000 with their own database, which two worktrees on one machine can't. Add `mango-2` in T3 Code like any box. A fork takes minutes: a disk copy, then a boot (6 minutes for a fresh 150 GB box in our test; a first fork of a large, busy disk can take over an hour).

Why not plain `prized fork`: T3 Code knows a machine by the environment id in `~/.t3/userdata`, and a copy of the disk carries the source's. Add the copy as is and T3 Code files it as a second route to the original machine, so it connects to whichever answers first. `fork` (or `prized-t3 reset-identity BOX --yes` after a `prized fork` of your own) stops T3 Code's server on the copy and moves aside what identifies the machine, into `~/.t3/identity-reset-<time>` there: the environment id, its signing keys, its T3 Connect link, the usage host id, and the old server's pid files. Projects, threads, settings and your saved secrets stay. T3 Code makes a new identity on its next start there.

## Two people, one box

Two T3 Code apps added to the same box share one T3 Code server there (the box's login user has one `~/.t3`): both see every project and thread, and either can act on any of them. T3 Code has no per-person view inside one machine. To run each person's agents on their own Claude sign-in at once, give each a Claude instance with its own config directory (T3 Code's [separate accounts](https://github.com/pingdotgg/t3code/blob/main/docs/user/providers-claude.md#separate-accounts-or-configurations)), signed in on the box with `CLAUDE_CONFIG_DIR=~/.claude-NAME claude auth login`, and pick it when starting a thread. Nothing stops one person from picking the other's instance. For separate threads and sign-ins, give each person a box of their own.

## When the box sleeps

Closing the lid drops T3 Code's connection, not its server: the server keeps running on the box, and the app reconnects when it comes back. A paused box normally keeps T3 Code's server in memory, so a resume picks up where it was; if the box restarted instead, T3 Code starts its server again on the next connect.

A box on the free credit pauses after 2 hours idle. Plain ssh (what T3 Code uses) wakes a box only when it paused itself for idleness, someone is at the Mac or Windows computer, and it has slept 10 minutes or more; otherwise T3 Code shows the connection failing until you run `prized box resume mango --wait` (about 10 seconds) or `prized-t3 up mango`. To keep it up while agents work: `prized-t3 up mango --auto-pause off`, and pause it yourself.

## Limits

- T3 Code's SSH environments are a desktop app feature. On a phone or in a browser, use T3 Code's own T3 Connect from the box (`~/.t3/runtime/versions/<version>/t3 up --connect`); `prized-t3` doesn't set that up.
- Prized sandboxes can't be T3 Code environments: their ssh carries no port forwarding.
- A forked box with T3 Code's background service (`t3code.service`) starts it at boot, before `fork` resets the identity: for those moments it can claim the source's T3 Connect link. Linked environments (a beta) are copied too; link the fork again.
- A fork carries the source's agent sign-ins. If both boxes then refresh the same Claude or Codex login, one of them may be signed out (untested); sign the copy in again if so.
- `prized-t3` works with T3 Code's default home, `~/.t3`. A box where T3 Code runs with `T3CODE_HOME` set elsewhere isn't handled.
- `up` installs the server for one version. After T3 Code updates, the app installs the new one itself on the next connect (or run `prized-t3 up` again).

## Tests

```bash
scripts/test.sh t3code
```

The tests run offline: stand-ins for `prized` and `ssh` run the script's box-side parts with the real `sh`, `tar` and `sha256sum` against a fake box home and a fake T3 Code release. No key, no network.

Tested live on 11-05 against T3 Code 0.1.7 on real boxes: `up`, `check`, `fork` and `reset-identity`, with T3 Code's own SSH launch and pairing steps run against the result over Prized's ssh (the server it installed was used as is; an unreset fork reported its source's environment id, and a forked box after `fork` reported its own). Not yet clicked through in the desktop app itself. To check it there: `prized-t3 up` a box and add it in T3 Code; `prized-t3 fork` it and check that T3 Code lists both boxes as two machines.

T3 Code is MIT-licensed, copyright T3 Tools Inc. The script's box-side install (`PREWARM_SCRIPT`) is adapted from T3 Code's SSH runner (`packages/ssh/src/tunnel.ts`); its MIT notice is in the script beside it.
