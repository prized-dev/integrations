// The pieces on their own: the key, the API client's refusals, the ssh files, the payload, the
// manifest against Orca's rules.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { describe, test } from "node:test";
import { PrizedApi, PrizedApiError, apiBase, canWriteSandboxes } from "../src/api.mjs";
import { versionOk } from "../src/cli.mjs";
import { pauseFirmly, runtimeRefusal } from "../src/commands.mjs";
import { keyFromLoginShell, resolveKey } from "../src/key.mjs";
import { checkoutScript, parsePayload, recipeResult, sourceFromOrca, stripCredentials } from "../src/recipe.mjs";
import {
  INCLUDE_MARK,
  addUserInclude,
  certValidBefore,
  hasUserInclude,
  parseCliConfig,
  proxyCommandFor,
  removeUserInclude,
  renderInclude,
  sshWord,
} from "../src/sshconf.mjs";
import { redact } from "../src/util.mjs";
import { API_KEY } from "./fake-api.mjs";
import { FAKES, PKG, makeRepo, tempDir } from "./helpers.mjs";
import { checkoutModeFailure, parseRecipe, parseResult, resumeIntegrityError, validateManifest } from "./orca-harness.mjs";

describe("the key", () => {
  test("comes from the environment first", () => {
    const r = resolveKey({ PRIZED_API_KEY: ` ${API_KEY}\n`, SHELL: "/nonexistent" });
    assert.deepEqual(r, { key: API_KEY, source: "environment" });
  });

  test("else from the login shell, past whatever the profile prints", () => {
    const env = { SHELL: path.join(FAKES, "login-shell"), FAKE_LOGIN_KEY: API_KEY, PATH: process.env.PATH };
    assert.equal(keyFromLoginShell(env), API_KEY);
    assert.deepEqual(resolveKey(env), { key: API_KEY, source: "login shell" });
  });

  test("a missing key says how to set one, and a malformed one is refused without echoing it", () => {
    const env = { SHELL: path.join(FAKES, "login-shell"), FAKE_LOGIN_KEY: "", PATH: process.env.PATH };
    assert.throws(() => resolveKey(env), /PRIZED_API_KEY is not set/);
    assert.throws(() => resolveKey({ PRIZED_API_KEY: "sk-not-prized-at-all" }), (e) => /not a Prized API key/.test(e.message) && !e.message.includes("sk-not"));
  });

  test("redact hides a remembered key and anything shaped like one", () => {
    resolveKey({ PRIZED_API_KEY: API_KEY });
    assert.equal(redact(`Bearer ${API_KEY}`), "Bearer [key]");
    assert.equal(redact("dcp_abcdef0123456789"), "dcp_[key]");
  });
});

describe("the API client", () => {
  test("sends the key to api.prized.dev or this machine only", () => {
    assert.equal(apiBase({}), "https://api.prized.dev/api/v1");
    assert.equal(apiBase({ PRIZED_ORCA_API_URL: "http://127.0.0.1:9/api/v1/" }), "http://127.0.0.1:9/api/v1");
    for (const bad of ["https://evil.example/api/v1", "http://api.prized.dev/api/v1", "https://api.prized.dev.evil.example/api/v1"]) {
      assert.throws(() => apiBase({ PRIZED_ORCA_API_URL: bad }), /refusing to send PRIZED_API_KEY/);
    }
  });

  test("refuses an edge address that is not https", () => {
    const api = new PrizedApi(API_KEY, { base: "https://api.prized.dev/api/v1" });
    assert.throws(() => api.edge({ id: "sbx_1", toolboxBase: "http://edge.example/v1/sandbox/sbx_1" }), /not https/);
    assert.equal(api.edge({ id: "sbx_1", toolboxBase: "https://edge.example/v1/sandbox/sbx_1/" }), "https://edge.example/v1/sandbox/sbx_1");
  });

  test("errors carry the API's code and message, never the key, and the bearer goes in a header", async () => {
    const seen = [];
    const fetchImpl = async (url, init) => {
      seen.push({ url, init });
      return new Response(JSON.stringify({ error: "key_access_denied", message: `nope for ${API_KEY}` }), { status: 403 });
    };
    const api = new PrizedApi(API_KEY, { base: "https://api.prized.dev/api/v1", fetchImpl });
    await assert.rejects(api.me(), (e) => e instanceof PrizedApiError && e.status === 403 && e.code === "key_access_denied" && !e.message.includes(API_KEY));
    assert.equal(seen[0].init.headers.Authorization, `Bearer ${API_KEY}`);
    assert.equal(seen[0].init.redirect, "error");
    assert.equal(seen[0].url.includes(API_KEY), false);
  });

  test("a delete of a sandbox that is already gone counts as done", async () => {
    const api = new PrizedApi(API_KEY, { base: "https://api.prized.dev/api/v1", fetchImpl: async () => new Response('{"error":"not_found"}', { status: 404 }) });
    assert.equal(await api.remove("sbx_1"), false);
  });

  test("a sandbox write key: full access, Sandboxes write, never a box-limited or contractor key", () => {
    assert.equal(canWriteSandboxes({}), true);
    assert.equal(canWriteSandboxes({ access: { sandboxes: "write" } }), true);
    assert.equal(canWriteSandboxes({ access: { sandboxes: "read" } }), false);
    assert.equal(canWriteSandboxes({ key: { boxIds: ["b1"] } }), false);
    assert.equal(canWriteSandboxes({ user: { role: "contractor" } }), false);
  });

  test("a refused run time is recognized, with the limit it names", () => {
    const free = new PrizedApiError(429, "sandbox_quota_exceeded", "On the free tier…", { reason: "max_runtime", limit: 60, tier: "free" });
    assert.deepEqual(runtimeRefusal(free), { limit: 60 });
    assert.deepEqual(runtimeRefusal(new PrizedApiError(422, "bad_request", "x", { field: "maxRuntimeMin" })), { limit: null });
    assert.equal(runtimeRefusal(new PrizedApiError(409, "insufficient_balance", "top up", {})), null);
    assert.equal(runtimeRefusal(new Error("boom")), null);
  });
});

describe("ssh files", () => {
  test("the include has a hook, a proxy and the CLI's own file per sandbox", () => {
    const text = renderInclude(
      { sandboxes: { sbx_00000000000000aa: { cliConfig: "/h/.config/prized/sandboxes/sbx_00000000000000aa/ssh_config" }, sbx_bb: { asleep: true } } },
      "/h/.prized-orca/bin/prized-orca",
    );
    assert.match(text, /\nMatch originalhost sbx_00000000000000aa exec "'\/h\/.prized-orca\/bin\/prized-orca' cert-hook sbx_00000000000000aa"\nHost sbx_00000000000000aa\n {4}ProxyCommand '\/h\/.prized-orca\/bin\/prized-orca' proxy sbx_00000000000000aa\nInclude "\/h\/.config\/prized\/sandboxes\/sbx_00000000000000aa\/ssh_config"\n/);
    assert.doesNotMatch(text, /sbx_bb/, "no block until the CLI has written its files");
  });

  test("a path ssh_config can't carry is refused, not quoted wrong", () => {
    for (const bad of ["/h/it's/prized-orca", "/h/50%/prized-orca", "/h/$x/prized-orca", 'rel/prized-orca']) {
      assert.throws(() => proxyCommandFor(bad, "sbx_1"), /can't carry|plain path/);
    }
    assert.throws(() => proxyCommandFor("/h/prized-orca", "sbx_1; rm -rf /"), /not a sandbox id/);
  });

  test("the Include line goes first, once, and comes out cleanly", () => {
    const dir = tempDir();
    try {
      const env = { PRIZED_ORCA_HOME: path.join(dir, "po"), PRIZED_ORCA_SSH_CONFIG: path.join(dir, "ssh", "config") };
      assert.equal(hasUserInclude(env), false);
      assert.match(addUserInclude(env), /added/);
      assert.equal(fs.statSync(env.PRIZED_ORCA_SSH_CONFIG).mode & 0o777, 0o600, "a new file is private");
      assert.equal(removeUserInclude(env), `removed from ${env.PRIZED_ORCA_SSH_CONFIG}`);
      assert.equal(fs.readFileSync(env.PRIZED_ORCA_SSH_CONFIG, "utf8"), "");
      const mine = "Host *\r\n    ServerAliveInterval 30\r\n";
      fs.writeFileSync(env.PRIZED_ORCA_SSH_CONFIG, mine);
      addUserInclude(env);
      assert.equal(addUserInclude(env), "already there");
      const text = fs.readFileSync(env.PRIZED_ORCA_SSH_CONFIG, "utf8");
      assert.ok(text.startsWith(`${INCLUDE_MARK}\nInclude "${env.PRIZED_ORCA_HOME}/ssh_config"\n\nHost *`));
      assert.equal(fs.readFileSync(`${env.PRIZED_ORCA_SSH_CONFIG}.prized-orca.bak`, "utf8"), mine, "a backup of the original");
      removeUserInclude(env);
      assert.equal(fs.readFileSync(env.PRIZED_ORCA_SSH_CONFIG, "utf8"), mine);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  test("a symlinked ~/.ssh/config stays a link, and its target gets the line", () => {
    const dir = tempDir();
    try {
      const target = path.join(dir, "dotfiles", "ssh_config");
      fs.mkdirSync(path.dirname(target));
      fs.writeFileSync(target, "Host a\n", { mode: 0o600 });
      const link = path.join(dir, "ssh", "config");
      fs.mkdirSync(path.dirname(link));
      fs.symlinkSync(target, link);
      const env = { PRIZED_ORCA_HOME: path.join(dir, "po"), PRIZED_ORCA_SSH_CONFIG: link };
      addUserInclude(env);
      assert.ok(fs.lstatSync(link).isSymbolicLink());
      assert.match(fs.readFileSync(target, "utf8"), /^# Added by the Prized Orca plugin.*\nInclude ".*\/po\/ssh_config"\n\nHost a\n$/);
      assert.equal(hasUserInclude(env), true);
      removeUserInclude(env);
      assert.ok(fs.lstatSync(link).isSymbolicLink());
      assert.equal(fs.readFileSync(target, "utf8"), "Host a\n");
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  test("an ssh_config word loses the quotes the CLI puts round a path with a space", () => {
    assert.equal(sshWord('"/Users/Ada Lovelace/.config/prized/sandboxes/sbx_1/cert.pub"'), "/Users/Ada Lovelace/.config/prized/sandboxes/sbx_1/cert.pub");
    assert.equal(sshWord("'/a b/c'"), "/a b/c");
    assert.equal(sshWord("/plain/path # note"), "/plain/path");
    assert.equal(parseCliConfig('Host x\n    CertificateFile "/A B/cert.pub"\n').certificateFile, "/A B/cert.pub");
  });

  test("the CLI's ssh_config is read for user, bridge and certificate", () => {
    const cfg = parseCliConfig(
      [
        "# Managed by `prized sandbox ssh`",
        "Host sbx_1",
        "    HostName sbx_1",
        "    User prized",
        "    HostKeyAlias box:0f0e0d0c-0b0a-4908-8706-050403020100",
        '    ProxyCommand "/Users/a/50%%/prized" sandbox _stdio --every 10m0s sbx_1',
        "    CertificateFile /Users/a/.config/prized/sandboxes/sbx_1/cert.pub",
      ].join("\n"),
    );
    assert.deepEqual(cfg, {
      user: "prized",
      hostKeyAlias: "box:0f0e0d0c-0b0a-4908-8706-050403020100",
      proxyCommand: '"/Users/a/50%/prized" sandbox _stdio --every 10m0s sbx_1',
      certificateFile: "/Users/a/.config/prized/sandboxes/sbx_1/cert.pub",
    });
  });

  test("a certificate's end is read from the certificate itself", { skip: spawnSync("ssh-keygen", ["-?"]).error ? "no ssh-keygen" : false }, () => {
    const dir = tempDir();
    try {
      spawnSync("ssh-keygen", ["-q", "-t", "ed25519", "-N", "", "-f", path.join(dir, "ca")]);
      spawnSync("ssh-keygen", ["-q", "-t", "ed25519", "-N", "", "-f", path.join(dir, "id")]);
      spawnSync("ssh-keygen", ["-q", "-s", path.join(dir, "ca"), "-I", "t", "-n", "box:x", "-V", "20300101000000:20300101001500", "-O", "force-command=true", path.join(dir, "id.pub")], {
        env: { ...process.env, TZ: "UTC" },
      });
      const before = certValidBefore(fs.readFileSync(path.join(dir, "id-cert.pub"), "utf8"));
      assert.equal(before.toISOString(), "2030-01-01T00:15:00.000Z");
      assert.equal(certValidBefore("ssh-ed25519 AAAA"), null);
      assert.equal(certValidBefore("garbage"), null);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("Orca's side", () => {
  test("the manifest and recipe pass Orca's checks for a plugin listed under prized-dev", () => {
    const { manifest, errors } = validateManifest(PKG, "prized-dev");
    assert.deepEqual(errors, []);
    assert.equal(manifest.id, "prized-sandbox");
    const recipe = parseRecipe(fs.readFileSync(path.join(PKG, "vm-recipe.json"), "utf8"));
    assert.equal(recipe.checkoutMode, "provisioned-root");
    for (const phase of ["create", "suspend", "resume", "destroy"]) assert.equal(recipe[phase], `"$HOME/.prized-orca/bin/prized-orca" ${phase}`);
    const pkg = JSON.parse(fs.readFileSync(path.join(PKG, "package.json"), "utf8"));
    assert.equal(pkg.version, manifest.version);
    assert.deepEqual(Object.keys(pkg.dependencies ?? {}), [], "no dependencies: nothing to audit but this folder");
    assert.match(fs.readFileSync(path.join(PKG, "src", "util.mjs"), "utf8"), new RegExp(`VERSION = "${manifest.version}"`));
  });

  test("the harness catches what Orca would refuse", () => {
    assert.throws(() => parseRecipe(JSON.stringify({ schemaVersion: 1, id: "x", name: "X", create: "c", suspend: "s" })), /together/);
    assert.throws(() => parseRecipe(JSON.stringify({ schemaVersion: 1, id: "X!", name: "X", create: "c" })), /recipe id/);
    assert.throws(() => parseResult('{"schemaVersion":2,"checkoutMode":"provisioned-root","connection":{"type":"ssh","projectRoot":"rel","target":{"label":"l","host":"h","port":22,"username":"u"}}}'), /NotAbsolute/);
    assert.throws(() => parseResult('{"schemaVersion":1,"connection":{"type":"ssh","projectRoot":"/r","target":{"label":"l","host":"h","port":22,"username":"u","certificateFile":"/c"}}}'), /unknown key certificateFile/);
  });

  test("the result is one Orca accepts, and the same on resume", () => {
    const r = recipeResult({ id: "sbx_00000000000000aa", user: "prized", projectRoot: "/home/prized/work/app", proxyCommand: "'/h/bin/prized-orca' proxy sbx_00000000000000aa" });
    const { result } = parseResult(JSON.stringify(r));
    assert.equal(checkoutModeFailure({ checkoutMode: "provisioned-root" }, result), null);
    assert.equal(resumeIntegrityError(result, structuredClone(result)), null);
    const moved = structuredClone(result);
    moved.connection.target.proxyCommand = "'/elsewhere/prized-orca' proxy sbx_00000000000000aa";
    assert.match(resumeIntegrityError(result, moved), /ownership changed/);
  });

  test("the lifecycle payload names the sandbox; anything else is refused", () => {
    const r = recipeResult({ id: "sbx_00000000000000aa", user: "prized", projectRoot: "/r", proxyCommand: "p" });
    assert.equal(parsePayload(JSON.stringify({ schemaVersion: 1, mode: "suspend", recipeResult: r })).id, "sbx_00000000000000aa");
    const noUserData = structuredClone(r);
    delete noUserData.userData;
    assert.equal(parsePayload(JSON.stringify({ recipeResult: noUserData })).id, "sbx_00000000000000aa");
    assert.throws(() => parsePayload(""), /no lifecycle payload/);
    assert.throws(() => parsePayload("{"), /not JSON/);
    assert.throws(() => parsePayload(JSON.stringify({ recipeResult: { userData: { resourceId: "sbx_1;reboot" } } })), /names no Prized sandbox/);
  });

  test("a value that would read as a git option is refused", () => {
    const dir = tempDir();
    try {
      const repo = makeRepo(path.join(dir, "repo"));
      const base = { ORCA_REPO_PATH: repo.dir, ORCA_REPO_REF_HEAD: repo.head, ORCA_REPO_URL: "https://example.com/a.git", ORCA_REPO_REF: "main", ORCA_REPO_BRANCH: "feat/x" };
      assert.equal(sourceFromOrca(base).branch, "feat/x");
      assert.throws(() => sourceFromOrca({ ...base, ORCA_REPO_REF: "--upload-pack=touch${IFS}X;false" }), /not a usable ref/);
      assert.throws(() => sourceFromOrca({ ...base, ORCA_REPO_URL: "-oProxyCommand=x" }), /not a usable repository URL/);
      assert.throws(() => sourceFromOrca({ ...base, ORCA_REPO_BRANCH: "--orphan" }), /not a usable branch/);
      assert.throws(() => sourceFromOrca({ ...base, ORCA_REPO_REF_HEAD: "HEAD~1" }), /not a commit id/);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  test("the CLI version check: 0.21.9 or later", () => {
    assert.equal(versionOk([0, 21, 9]), true);
    assert.equal(versionOk([0, 22, 0]), true);
    assert.equal(versionOk([1, 0, 0]), true);
    assert.equal(versionOk([0, 21, 8]), false);
    assert.equal(versionOk(null), false);
    assert.equal(versionOk([0, 0, 1]), true, "a build from source");
  });

  test("a pause is repeated until the sandbox stays paused, and a gone sandbox is fine", async () => {
    const states = ["running", "running", "paused"];
    let posts = 0;
    const client = { get: async () => ({ state: states.shift() ?? "paused" }), pause: async () => posts++, sleep: async () => {} };
    assert.equal(await pauseFirmly(client, "sbx_1"), "paused");
    assert.equal(posts, 2);
    assert.equal(await pauseFirmly({ get: async () => null }, "sbx_1"), "gone");
    const stubborn = { get: async () => ({ state: "running" }), pause: async () => {}, sleep: async () => {} };
    await assert.rejects(pauseFirmly(stubborn, "sbx_1"), /keeps waking up/);
    const deleting = { get: async () => ({ state: "running" }), pause: async () => { throw new PrizedApiError(410, "sandbox_deleted", "deleting"); }, sleep: async () => {} };
    assert.equal(await pauseFirmly(deleting, "sbx_1"), "gone");
  });

  test("credentials never ride along in a remote URL", () => {
    assert.equal(stripCredentials("https://ada:ghp_x@github.com/acme/app.git"), "https://github.com/acme/app.git");
    assert.equal(stripCredentials("git@github.com:acme/app.git"), "git@github.com:acme/app.git");
  });

  test("the checkout script lands the exact commit on the branch, from a bundle", () => {
    const dir = tempDir();
    try {
      const repo = makeRepo(path.join(dir, "repo"));
      const ref = "refs/prized-orca/tmp-test";
      spawnSync("git", ["-C", repo.dir, "update-ref", ref, repo.first]);
      spawnSync("git", ["-C", repo.dir, "bundle", "create", "--quiet", path.join(dir, "b.bundle"), ref]);
      const root = path.join(dir, "sandbox", "work", "app");
      const script = checkoutScript({ root, bundlePath: path.join(dir, "b.bundle"), bundleRef: ref, head: repo.first, branch: "feat/x", repoUrl: "https://example.com/a.git", identity: { name: "Ada O'Tester", email: "ada@example.com" } });
      const r = spawnSync("bash", ["-c", script], { encoding: "utf8", env: { PATH: process.env.PATH, HOME: dir, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: path.join(dir, "gc") } });
      assert.equal(r.status, 0, r.stderr);
      assert.equal(r.stdout.trim().split("\n").pop(), repo.first);
      const g = (...a) => spawnSync("git", ["-C", root, ...a], { encoding: "utf8" }).stdout.trim();
      assert.equal(g("branch", "--show-current"), "feat/x");
      assert.equal(g("config", "user.name"), "Ada O'Tester");
      assert.equal(fs.existsSync(path.join(dir, "b.bundle")), false);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
