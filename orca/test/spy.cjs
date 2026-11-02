// Loaded into every Node process of a lifecycle test (NODE_OPTIONS=--require): notes each process a
// process starts and each request it sends, with whether the test key is in it, never the values.
// The key it looks for is the fake one the tests use (test/fake-api.mjs).
const fs = require("node:fs");
const cp = require("node:child_process");
const { syncBuiltinESMExports } = require("node:module");

const KEY = "dcp_00000000000000000000000000000000000000000000beef";
const LOG = process.env.SPY_LOG;
const has = (v) => JSON.stringify(v ?? null).includes(KEY);
// The tests' own stand-ins for a login (FAKE_LOGIN_KEY, FAKE_LOGIN_BEARER) carry the key on purpose.
const envHas = (env) => has(Object.fromEntries(Object.entries(env).filter(([k]) => !k.startsWith("FAKE_"))));
const note = (o) => LOG && fs.appendFileSync(LOG, `${JSON.stringify({ pid: process.pid, ...o })}\n`);

for (const name of ["spawn", "spawnSync", "execFile", "execFileSync", "exec", "execSync", "fork"]) {
  const original = cp[name];
  cp[name] = function spied(...args) {
    const opts = args.find((a) => a && typeof a === "object" && !Array.isArray(a));
    note({
      kind: "spawn",
      file: String(args[0]).slice(0, 300),
      argvHasKey: has(args.filter((a) => typeof a === "string" || Array.isArray(a))),
      // no env given: the child inherits this process's own
      envHasKey: envHas(opts?.env ?? process.env),
    });
    return original.apply(this, args);
  };
}
syncBuiltinESMExports();

const realFetch = globalThis.fetch;
globalThis.fetch = (url, init = {}) => {
  note({ kind: "fetch", url: String(url).slice(0, 300), authHasKey: has(init.headers?.Authorization), bodyHasKey: has(typeof init.body === "string" ? init.body : null) });
  return realFetch(url, init);
};
