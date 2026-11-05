#!/usr/bin/env node
// The Prized Sandbox adapter for Orca. Orca runs `create`, `suspend`, `resume` and `destroy` (see
// vm-recipe.json); ssh runs `cert-hook` and `proxy` (see src/sshconf.mjs); people run `setup`,
// `doctor`, `list`, `pull` and `uninstall`.
//
// Contract: create and resume print exactly one JSON object on stdout, the result Orca reads.
// Everything else goes to stderr.

import { HELP, certHook, create, destroy, doctor, list, proxy, pull, resume, setup, suspend, uninstall } from "../src/commands.mjs";
import { UserError, VERSION, redact, say, trace } from "../src/util.mjs";

// Keep stdout for the recipe result, whatever else prints.
console.log = (...args) => console.error(...args);
console.info = console.log;

const LIFECYCLE = new Set(["create", "suspend", "resume", "destroy"]);
const [cmd, ...rest] = process.argv.slice(2);

const commands = {
  create: () => create(),
  suspend: () => suspend(),
  resume: () => resume(),
  destroy: () => destroy(),
  "cert-hook": () => certHook(rest[0]),
  proxy: () => proxy(rest[0]),
  setup: () => setup(rest),
  doctor: () => doctor(),
  list: () => list(),
  pull: () => pull(rest),
  uninstall: () => uninstall(rest),
};

async function main() {
  if (cmd === "--version" || cmd === "version") {
    process.stderr.write(`${VERSION}\n`);
    return 0;
  }
  if (!cmd || cmd === "help" || cmd === "--help" || cmd === "-h") {
    process.stderr.write(`${HELP}\n`);
    return cmd ? 0 : 2;
  }
  const run = commands[cmd];
  if (!run) {
    process.stderr.write(`unknown command ${JSON.stringify(cmd)}\n\n${HELP}\n`);
    return 2;
  }
  const started = Date.now();
  if (LIFECYCLE.has(cmd)) trace({ cmd, start: true, instance: process.env.ORCA_VM_INSTANCE_ID ?? null });
  try {
    const code = await run();
    if (LIFECYCLE.has(cmd)) trace({ cmd, ok: true, ms: Date.now() - started });
    return typeof code === "number" ? code : 0;
  } catch (error) {
    const known = error instanceof UserError || error?.name === "PrizedApiError" || typeof error?.status === "number";
    if (cmd === "cert-hook") return 0;
    if (cmd === "proxy") {
      process.stderr.write(`prized-orca: ${redact(error.message)}\n`);
      return 1;
    }
    say(known ? error.message : redact(error.stack || error.message));
    if (error?.hint) say(error.hint);
    if (LIFECYCLE.has(cmd)) trace({ cmd, ok: false, ms: Date.now() - started, error: error.message });
    return 1;
  }
}

main().then((code) => {
  process.exitCode = code;
  // proxy's child holds the stdio pipes; once it has exited there is nothing left to wait for.
  if (cmd === "proxy" || cmd === "cert-hook") process.exit(code);
});
