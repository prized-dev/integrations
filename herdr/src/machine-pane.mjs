#!/usr/bin/env node
// "Add a Prized box as a machine": pick a box, wake it if it is paused, check plain ssh reaches it,
// put its Claude Code and Codex on the path herdr's remote panes get, then hand over to
// `herdr machine add`, which asks before it installs herdr on the box.

import { spawnSync } from "node:child_process";

import { PluginError, explain, herdrBin, pickBox, prizedBin, prizedOk, readKey, readLine, say } from "./lib.mjs";

const LINK_AGENTS =
  'for a in claude codex; do if [ -x "$HOME/.local/bin/$a" ] && [ ! -e "/usr/local/bin/$a" ]; then sudo -n ln -s "$HOME/.local/bin/$a" "/usr/local/bin/$a" && echo "linked $a"; fi; done; true';

function sshBin(env = process.env) {
  return env.PRIZED_HERDR_SSH || "ssh";
}

function ssh(box, command) {
  return spawnSync(sshBin(), ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20", box, command], { encoding: "utf8" });
}

async function yes(question) {
  const answer = (await readLine(`${question} [Y/n] `)).trim().toLowerCase();
  return answer === "" || answer === "y" || answer === "yes";
}

async function main() {
  say("Add a Prized box to herdr's sidebar");
  say("");
  const listed = prizedOk(["box", "ls", "--json"]);
  let boxes;
  try {
    boxes = JSON.parse(String(listed.stdout)).boxes ?? [];
  } catch {
    throw new PluginError("prized box ls --json printed something that is not JSON");
  }
  const name = await pickBox(boxes);
  const box = boxes.find((b) => b.hostname === name);
  say(`Box ${name}`);

  if (box && box.observedState !== "running") {
    if (!(await yes(`${name} is ${box.observedState}. Wake it?`))) return say("Nothing changed.");
    const woke = spawnSync(prizedBin(), ["box", "resume", name, "--wait"], { stdio: "inherit" });
    if (woke.status !== 0) throw new PluginError(`prized box resume ${name} failed (exit ${woke.status})`);
  }

  process.stdout.write(`  checking that ssh ${name} connects... `);
  const check = ssh(name, "true");
  if (check.status !== 0) {
    say("no");
    say(String(check.stderr).trim().split("\n").slice(-3).join("\n"));
    throw new PluginError(
      `plain ssh does not reach ${name} from this machine`,
      `run \`prized init --box ${name}\` in a terminal to add it to your SSH config, then try again`,
    );
  }
  say("yes");

  say("herdr's panes on the box get the system PATH, which leaves out ~/.local/bin where the box keeps");
  if (await yes("Claude Code and Codex. Link them into /usr/local/bin (uses the box's passwordless sudo)?")) {
    const linked = ssh(name, LINK_AGENTS);
    say(linked.stdout.trim() || "  already on the path");
  }

  say("");
  say(`Running herdr machine add ${name}. It asks before it installs herdr on the box.`);
  const added = spawnSync(herdrBin(), ["machine", "add", name, "--label", name], { stdio: "inherit" });
  if (added.status !== 0) throw new PluginError(`herdr machine add ${name} did not finish (exit ${added.status})`);
  say("");
  say(`${name} is in the sidebar. Click its + to open a space there; its agents keep running when you close`);
  say("herdr or your laptop sleeps. Sign the agents in on the box with `prized agents handoff claude codex --gh`.");
}

try {
  await main();
} catch (error) {
  say("");
  say(explain(error));
  process.exitCode = 1;
}
say("");
say("Press any key to close.");
await readKey();
