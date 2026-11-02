// Orca's side of the contract (Orca 1.4.245, provisioned-root, SSH): what create and resume print,
// what suspend, resume and destroy read on stdin, and the checkout a new sandbox gets.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { UserError, isSandboxId, sq } from "./util.mjs";
import { git, gitOk } from "./cli.mjs";

// The one JSON object Orca reads from stdout. Every field of the SSH target that Orca compares on
// resume (username, proxyCommand, and no identity fields) is the same each time for a sandbox.
export function recipeResult({ id, user, projectRoot, proxyCommand }) {
  return {
    schemaVersion: 2,
    checkoutMode: "provisioned-root",
    connection: {
      type: "ssh",
      projectRoot,
      target: {
        label: `Prized sandbox ${id}`,
        host: id,
        port: 22,
        username: user,
        proxyCommand,
      },
    },
    userData: { provider: "prized-sandbox", resourceId: id },
  };
}

export async function readStdin(stream = process.stdin) {
  if (stream.isTTY) return "";
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8").trim();
}

// The lifecycle payload Orca pipes to suspend, resume and destroy: the sandbox id is the result's
// userData.resourceId (or, if that is missing, its SSH host).
export function parsePayload(raw) {
  if (!raw) throw new UserError("Orca sent no lifecycle payload on stdin");
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new UserError("Orca's lifecycle payload is not JSON");
  }
  const result = payload?.recipeResult ?? {};
  const target = result?.connection?.target ?? {};
  const id = result?.userData?.resourceId ?? target.host;
  if (!isSandboxId(id)) throw new UserError(`the lifecycle payload names no Prized sandbox (got ${JSON.stringify(id ?? null)})`);
  return {
    id,
    mode: payload?.mode,
    instanceId: payload?.instanceId,
    workspaceId: payload?.workspaceId,
    workspaceName: payload?.workspaceName,
    projectRoot: result?.connection?.projectRoot,
    user: target.username,
  };
}

// ---------------------------------------------------------------- the source Orca picked

export function stripCredentials(url) {
  try {
    const u = new URL(url);
    if (u.username || u.password) {
      u.username = "";
      u.password = "";
    }
    return u.toString();
  } catch {
    // scp-style git@host:path carries no secret beyond the user name
    return String(url);
  }
}

// The exact commit and branch Orca asked for. `orca vm recipe doctor --provision` passes these empty,
// so then it is the local checkout's HEAD.
export function sourceFromOrca(env = process.env) {
  const repoPath = env.ORCA_REPO_PATH || process.cwd();
  const fromOrca = Boolean(env.ORCA_REPO_REF_HEAD);
  const head = env.ORCA_REPO_REF_HEAD || gitOk(["rev-parse", "HEAD"], { cwd: repoPath });
  if (!/^[0-9a-f]{40}([0-9a-f]{24})?$/.test(head)) throw new UserError(`not a commit id: ${head}`);
  const repoUrl = stripCredentials(env.ORCA_REPO_URL || git(["remote", "get-url", "origin"], { cwd: repoPath }).stdout.trim());
  const ref = env.ORCA_REPO_REF || "HEAD";
  const suffix = String(env.ORCA_VM_INSTANCE_ID || Date.now()).replace(/^orca-/, "").replace(/[^A-Za-z0-9]/g, "").slice(0, 12);
  const branch = env.ORCA_REPO_BRANCH || `orca-${suffix}`;
  if (git(["check-ref-format", "--branch", branch], { cwd: repoPath }).status !== 0) throw new UserError(`not a branch name: ${branch}`);
  const repoName = path.basename(path.resolve(repoPath)).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+/, "").slice(0, 64) || "repo";
  return { repoPath, fromOrca, head, repoUrl, ref, branch, repoName };
}

// Make sure the commit is here, fetching it with the user's own git credentials if needed.
export function ensureCommit(src) {
  const have = () => git(["cat-file", "-e", `${src.head}^{commit}`], { cwd: src.repoPath }).status === 0;
  if (have()) return;
  if (!src.repoUrl) throw new UserError(`commit ${src.head.slice(0, 12)} is not in ${src.repoPath} and the repository has no remote`);
  gitOk(["fetch", "--quiet", src.repoUrl, src.ref], { cwd: src.repoPath });
  if (!have()) throw new UserError(`commit ${src.head.slice(0, 12)} is not in ${src.repoUrl} ${src.ref}`);
}

// A git bundle of exactly that commit and its history, made under a temporary ref that is removed
// at once. The bundle goes to the sandbox, so no git credential does.
export function makeBundle(src) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "prized-orca-"));
  const file = path.join(dir, "repo.bundle");
  const ref = `refs/prized-orca/tmp-${process.pid}-${Date.now()}`;
  gitOk(["update-ref", ref, src.head], { cwd: src.repoPath });
  try {
    gitOk(["bundle", "create", "--quiet", file, ref], { cwd: src.repoPath });
  } finally {
    git(["update-ref", "-d", ref], { cwd: src.repoPath });
  }
  return { dir, file, ref, bytes: fs.statSync(file).size };
}

export function gitIdentity(repoPath) {
  return {
    name: git(["config", "user.name"], { cwd: repoPath }).stdout.trim(),
    email: git(["config", "user.email"], { cwd: repoPath }).stdout.trim(),
  };
}

// Runs in the sandbox (bash, as its user): a fresh repository at ROOT holding the bundle's commit on
// the workspace branch, with the real remote as origin and the user's git name and email. Prints the
// checked-out commit last.
export function checkoutScript({ root, bundlePath, bundleRef, head, branch, repoUrl, identity }) {
  return [
    "set -e",
    `rm -rf ${sq(root)}`,
    `mkdir -p ${sq(path.posix.dirname(root))}`,
    `git -c init.defaultBranch=${sq(branch)} init --quiet ${sq(root)}`,
    `cd ${sq(root)}`,
    `git fetch --quiet ${sq(bundlePath)} ${sq(`${bundleRef}:refs/prized-orca/source`)}`,
    repoUrl ? `git remote add origin ${sq(repoUrl)}` : "true",
    `git checkout --quiet -B ${sq(branch)} ${sq(head)}`,
    "git update-ref -d refs/prized-orca/source",
    identity.name ? `git config user.name ${sq(identity.name)}` : "true",
    identity.email ? `git config user.email ${sq(identity.email)}` : "true",
    `rm -f ${sq(bundlePath)}`,
    "git rev-parse HEAD",
  ].join("\n");
}
