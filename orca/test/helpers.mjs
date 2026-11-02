// Test scaffolding: a throwaway HOME, a git repository, a local sshd set up like a sandbox guest, and
// a way to run the adapter the way Orca and ssh run it.

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const PKG = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const FAKES = path.join(PKG, "test", "fakes");
export const SSHD = process.env.FAKE_SSHD_BIN || "/usr/sbin/sshd";

export function haveSshKit() {
  const keygen = spawnSync("ssh-keygen", ["-?"], { encoding: "utf8" });
  const ssh = spawnSync("ssh", ["-V"], { encoding: "utf8" });
  const setpriv = spawnSync("setpriv", ["--help"], { encoding: "utf8" });
  return !keygen.error && !ssh.error && !setpriv.error && fs.existsSync(SSHD);
}

export function tempDir(prefix = "prized-orca-test-") {
  return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), prefix)));
}

export function sh(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { encoding: "utf8", ...opts });
  if (r.status !== 0) throw new Error(`${cmd} ${args.join(" ")} failed: ${r.stderr || r.stdout}`);
  return r.stdout.trim();
}

// A sshd for inetd mode (sshd -i, as this user) that trusts what a sandbox guest trusts: user
// certificates from one CA naming box:<uuid>, forced to the account's own uid, with a host
// certificate for box:<uuid>. Mirrors controlplane/core/src/sshca.ts sandboxSshForcedCommand.
export function makeSshdKit(dir) {
  fs.mkdirSync(dir, { recursive: true });
  const uuid = "0f0e0d0c-0b0a-4908-8706-050403020100";
  for (const name of ["user_ca", "host_ca", "hostkey"]) sh("ssh-keygen", ["-q", "-t", "ed25519", "-N", "", "-C", name, "-f", path.join(dir, name)]);
  sh("ssh-keygen", ["-q", "-s", path.join(dir, "host_ca"), "-I", "host", "-h", "-n", `box:${uuid}`, "-V", "-5m:+2h", path.join(dir, "hostkey.pub")]);
  const forced =
    `[ "$(id -u)" = ${process.getuid()} ] || { echo 'prized: this certificate opens the sandbox user only' >&2; exit 1; }; ` +
    'c=$SSH_ORIGINAL_COMMAND; unset SSH_ORIGINAL_COMMAND; ' +
    '[ -n "$c" ] && exec setpriv --no-new-privs "$SHELL" -c "$c"; exec setpriv --no-new-privs "$SHELL" -l';
  fs.writeFileSync(path.join(dir, "forced-command"), forced);
  fs.writeFileSync(path.join(dir, "uuid"), uuid);
  fs.writeFileSync(path.join(dir, "principals"), `box:${uuid}\n`);
  const sftp = ["/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server", "/usr/libexec/sftp-server"].find((p) => fs.existsSync(p));
  fs.writeFileSync(
    path.join(dir, "sshd_config"),
    [
      `HostKey "${path.join(dir, "hostkey")}"`,
      `HostCertificate "${path.join(dir, "hostkey-cert.pub")}"`,
      `TrustedUserCAKeys "${path.join(dir, "user_ca.pub")}"`,
      `AuthorizedPrincipalsFile "${path.join(dir, "principals").replace(/%/g, "%%")}"`,
      "AuthorizedKeysFile none",
      "PasswordAuthentication no",
      "KbdInteractiveAuthentication no",
      "UsePAM no",
      "StrictModes no",
      "PidFile none",
      "LogLevel ERROR",
      "AllowTcpForwarding no",
      sftp ? `Subsystem sftp ${sftp}` : "",
      "",
    ].join("\n"),
    { mode: 0o600 },
  );
  return dir;
}

// A repository with two commits on main, and an origin whose URL carries credentials (which must
// never reach a sandbox).
export function makeRepo(dir) {
  fs.mkdirSync(dir, { recursive: true });
  const git = (...args) => sh("git", ["-C", dir, ...args], { env: { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: path.join(dir, "..", "gitconfig") } });
  git("init", "-q", "-b", "main");
  git("config", "user.name", "Ada Tester");
  git("config", "user.email", "ada@example.com");
  fs.writeFileSync(path.join(dir, "README.md"), "# app\n");
  git("add", "README.md");
  git("commit", "-q", "-m", "first");
  fs.writeFileSync(path.join(dir, "main.js"), "console.log('hi')\n");
  git("add", "main.js");
  git("commit", "-q", "-m", "second");
  git("remote", "add", "origin", "https://ada:ghp_secret123@example.com/acme/app.git");
  return { dir, head: git("rev-parse", "HEAD"), first: git("rev-parse", "HEAD~1") };
}

// The environment Orca's recipe commands see in these tests. HOME is a fresh directory, so the
// recipe's own command ("$HOME/.prized-orca/bin/prized-orca") reaches the adapter setup installed.
export function testEnv(root, extra = {}) {
  const home = path.join(root, "home");
  fs.mkdirSync(path.join(home, ".ssh"), { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(root, "tmp"), { recursive: true });
  return {
    TMPDIR: path.join(root, "tmp"),
    PATH: process.env.PATH,
    HOME: home,
    LANG: "C.UTF-8",
    SHELL: path.join(FAKES, "login-shell"),
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: path.join(root, "gitconfig"),
    PRIZED_ORCA_SSH_CONFIG: path.join(home, ".ssh", "config"),
    PRIZED_BIN: path.join(FAKES, "prized"),
    FAKE_CLI_LOG: path.join(root, "cli.log"),
    ...extra,
  };
}

export function runAdapter(args, { env, stdin, cwd } = {}) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [path.join(PKG, "bin", "prized-orca.mjs"), ...args], { env, cwd, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.on("close", (status) => resolve({ status, stdout, stderr }));
    child.stdin.end(stdin ?? undefined);
  });
}

// Every file under dir, as text, for "the key is in none of them" checks.
export function allText(dir) {
  const out = [];
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.isFile()) out.push([p, fs.readFileSync(p).toString("latin1")]);
    }
  };
  if (fs.existsSync(dir)) walk(dir);
  return out;
}
