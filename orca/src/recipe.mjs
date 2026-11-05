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
  for (const [what, v] of [["repository URL", repoUrl], ["ref", ref], ["branch", branch]]) {
    if (v.startsWith("-") || /[\0\r\n]/.test(v)) throw new UserError(`not a usable ${what}: ${JSON.stringify(v)}`);
  }
  if (git(["check-ref-format", "--branch", branch], { cwd: repoPath }).status !== 0) throw new UserError(`not a branch name: ${branch}`);
  const repoName = path.basename(path.resolve(repoPath)).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+/, "").slice(0, 64) || "repo";
  return { repoPath, fromOrca, head, repoUrl, ref, branch, repoName };
}

// Make sure the commit is here, fetching it with the user's own git credentials if needed.
export function ensureCommit(src) {
  const have = () => git(["cat-file", "-e", `${src.head}^{commit}`], { cwd: src.repoPath }).status === 0;
  if (have()) return;
  if (!src.repoUrl) throw new UserError(`commit ${src.head.slice(0, 12)} is not in ${src.repoPath} and the repository has no remote`);
  gitOk(["fetch", "--quiet", "--end-of-options", src.repoUrl, src.ref], { cwd: src.repoPath });
  if (!have()) throw new UserError(`commit ${src.head.slice(0, 12)} is not in ${src.repoUrl} ${src.ref}`);
}

// A git bundle of exactly that commit and its history, made under a temporary ref that is removed
// at once. The bundle goes to the sandbox, so no git credential does.
export function makeBundle(src) {
  dropStaleRefs(src.repoPath);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "prized-orca-"));
  const file = path.join(dir, "repo.bundle");
  const ref = `refs/prized-orca/tmp-${process.pid}-${Date.now()}`;
  try {
    gitOk(["update-ref", ref, src.head], { cwd: src.repoPath });
    gitOk(["bundle", "create", "--quiet", file, ref], { cwd: src.repoPath });
  } catch (error) {
    fs.rmSync(dir, { recursive: true, force: true });
    throw error;
  } finally {
    git(["update-ref", "-d", ref], { cwd: src.repoPath });
  }
  return { dir, file, ref, bytes: fs.statSync(file).size };
}

// A create killed mid-bundle leaves its temporary ref behind; drop those whose process is gone.
function dropStaleRefs(repoPath) {
  const refs = git(["for-each-ref", "--format=%(refname)", "refs/prized-orca/"], { cwd: repoPath }).stdout.split("\n");
  for (const ref of refs) {
    const pid = Number(/^refs\/prized-orca\/tmp-(\d+)-\d+$/.exec(ref)?.[1]);
    if (!pid) continue;
    try {
      process.kill(pid, 0);
    } catch (error) {
      if (error.code === "ESRCH") git(["update-ref", "-d", ref], { cwd: repoPath });
    }
  }
}

export function gitIdentity(repoPath) {
  return {
    name: git(["config", "user.name"], { cwd: repoPath }).stdout.trim(),
    email: git(["config", "user.email"], { cwd: repoPath }).stdout.trim(),
  };
}

// origin as the sandbox can use it. A sandbox has no SSH key, and its connection carries no agent
// forwarding, so an SSH remote (git@host:owner/repo, ssh://…) can't sign in there: it gets the same
// repository over HTTPS, which fetches a public repository as it is and pushes with a token.
// hostnameOf turns an alias from ~/.ssh/config (github-work, github.com-work) into the real host.
// A host that still isn't a domain, Azure DevOps (whose HTTPS path differs), and every other URL
// stay as they are.
export function sandboxRemoteUrl(url, hostnameOf = (host) => host) {
  const s = String(url ?? "");
  const m = /^ssh:\/\/(?:[^@/]+@)?([A-Za-z0-9._-]+)(?::\d+)?\/+(.+)$/.exec(s) ?? /^(?:[^@/:]+@)?([A-Za-z0-9._-]{2,}):(?!\/)(.+)$/.exec(s);
  if (!m) return s;
  let host = String(hostnameOf(m[1]) || m[1]).toLowerCase();
  if (host === "ssh.github.com") host = "github.com";
  if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(host) || /(^|\.)(dev\.azure\.com|visualstudio\.com)$/.test(host)) return s;
  return `https://${host}/${m[2].replace(/^\/+/, "")}`;
}

const COMMIT = /^[0-9a-f]{40}([0-9a-f]{24})?$/;

// Runs in the sandbox (bash, as its user): makes a bundle at BUNDLE of the branch checked out at ROOT,
// holding only the commits after BASE (the commit the workspace started from, which the user's own
// repository has), or its whole history without one. Prints the branch, its commit, how many
// uncommitted changes the checkout has, and how many commits are in the bundle (0: no bundle).
export function pullScript({ root, bundlePath, base }) {
  if (base && !COMMIT.test(base)) throw new UserError(`not a commit id: ${base}`);
  return [
    "set -e",
    `cd ${sq(root)}`,
    'ref=$(git symbolic-ref -q HEAD) || { echo "the checkout is not on a branch (a detached HEAD): check out a branch in it first" >&2; exit 3; }',
    'case $ref in refs/heads/*) ;; *) echo "the checkout is on $ref, not a branch" >&2; exit 3 ;; esac',
    'b=${ref#refs/heads/}',
    "head=$(git rev-parse HEAD)",
    "dirty=$(git status --porcelain | wc -l | tr -d ' ')",
    `rm -f ${sq(bundlePath)}`,
    base
      ? `if git cat-file -e ${base}^{commit} 2>/dev/null; then n=$(git rev-list --count ${base}..HEAD); not="--not ${base}"; else n=$(git rev-list --count HEAD); not=""; fi`
      : 'n=$(git rev-list --count HEAD); not=""',
    `if [ "$n" -gt 0 ]; then git bundle create -q ${sq(bundlePath)} "$ref" $not; fi`,
    `printf '%s\\n%s\\n%s\\n%s\\n' "$b" "$head" "$dirty" "$n"`,
  ].join("\n");
}

// What Remove saves, in two steps run in the sandbox (bash, as its user).
//
// saveListScript: everything at ROOT that could hold work. Every branch, the latest stash, and for
// each worktree (the checkout, and any worktree made in it, as Orca does for a new workspace on
// this host) a detached HEAD and its uncommitted changes: one commit on top of its HEAD (untracked
// files too, as .gitignore allows), made from a temporary index, so the worktree, its index and its
// branch are untouched, and only when its tree differs from HEAD's (no reliance on git status).
// Prints "worktree <n> <path>" (0 is ROOT), "ref <commit> <refname>" lines, "nested <n>" (nested
// repositories and submodules, whose own work is not saved) and "end <number of refs>".
export const SAVE_NS = "refs/prized-orca";
export function saveListScript({ root }) {
  return [
    "set -e",
    `cd ${sq(root)}`,
    `git for-each-ref --format='%(refname)' ${SAVE_NS} | while read -r r; do git update-ref -d "$r"; done`,
    "if ! git var GIT_AUTHOR_IDENT >/dev/null 2>&1 || ! git var GIT_COMMITTER_IDENT >/dev/null 2>&1; then",
    "  export GIT_AUTHOR_NAME=prized-orca GIT_AUTHOR_EMAIL=prized-orca@localhost GIT_COMMITTER_NAME=prized-orca GIT_COMMITTER_EMAIL=prized-orca@localhost",
    "fi",
    "nested=0",
    "snap() {",
    '  local head idx tree wip',
    '  head=$(git -C "$1" rev-parse -q --verify "HEAD^{commit}") || return 0',
    `  git -C "$1" symbolic-ref -q HEAD >/dev/null || git update-ref "${SAVE_NS}/detached/$2" "$head"`,
    '  idx=$(mktemp)',
    '  GIT_INDEX_FILE=$idx git -C "$1" read-tree HEAD',
    '  GIT_INDEX_FILE=$idx git -C "$1" add -A',
    '  tree=$(GIT_INDEX_FILE=$idx git -C "$1" write-tree)',
    "  nested=$((nested + $(GIT_INDEX_FILE=$idx git -C \"$1\" ls-files -s | awk '$1 == \"160000\"' | wc -l)))",
    '  rm -f "$idx"',
    '  if [ "$tree" != "$(git -C "$1" rev-parse "HEAD^{tree}")" ]; then',
    '    wip=$(git -C "$1" commit-tree "$tree" -p HEAD -m "prized-orca: uncommitted changes when the workspace was removed")',
    `    git update-ref "${SAVE_NS}/uncommitted/$2" "$wip"`,
    "  fi",
    "}",
    'mapfile -t wts < <(git worktree list --porcelain | sed -n "s/^worktree //p")',
    'for i in "${!wts[@]}"; do snap "${wts[$i]}" "$i" </dev/null; printf \'worktree %s %s\\n\' "$i" "${wts[$i]}"; done',
    `git for-each-ref --format='ref %(objectname) %(refname)' refs/heads refs/stash ${SAVE_NS}`,
    `printf 'nested %s\\nend %s\\n' "$nested" "$(git for-each-ref refs/heads refs/stash ${SAVE_NS} | wc -l | tr -d ' ')"`,
  ].join("\n");
}

// saveBundleScript: a bundle at BUNDLE of REFS, without what NOT already covers (commits the user's
// repository has). Prints "empty" when there is nothing to bundle; refuses more than one download
// takes (100 MiB).
export function saveBundleScript({ root, bundlePath, refs, not }) {
  for (const r of refs) if (!/^refs\/[^\x00-\x20\x7f~^:?*[\\]+$/.test(r) || r.includes("..") || r.includes("@{")) throw new UserError(`not a ref: ${r}`);
  for (const c of not) if (!COMMIT.test(c)) throw new UserError(`not a commit id: ${c}`);
  const want = refs.map(sq).join(" ");
  const exclude = not.length ? `--not ${not.join(" ")}` : "";
  return [
    "set -e",
    `cd ${sq(root)}`,
    `rm -f ${sq(bundlePath)}`,
    `if [ "$(git rev-list --count ${want} ${exclude})" = 0 ]; then echo empty; exit 0; fi`,
    `git bundle create -q ${sq(bundlePath)} ${want} ${exclude}`,
    `if [ "$(wc -c < ${sq(bundlePath)})" -gt 104857600 ]; then rm -f ${sq(bundlePath)}; echo "what the workspace has that this machine doesn't comes to more than 100 MB, more than one download takes" >&2; exit 4; fi`,
    "echo bundled",
  ].join("\n");
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
