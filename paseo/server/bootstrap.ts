// What runs inside the sandbox: install the pinned Paseo CLI into the home (sandboxes have no root),
// start its daemon, and ask it for a relay pairing link. The relay is outbound only, which is all a
// sandbox allows: nothing reaches a sandbox from the internet.

export const PASEO_CLI_VERSION = "0.14.0";
export const PASEO_HOME = "/home/prized/.paseo";
export const WORKSPACE = "/home/prized/work";

const PREAMBLE = [
  "set -euo pipefail",
  'export NPM_CONFIG_PREFIX="$HOME/.npm-global"',
  'export PATH="$HOME/.npm-global/bin:$PATH"',
  `export PASEO_HOME=${PASEO_HOME}`,
].join("\n");

// flock: a Retry while an earlier install still runs waits for it instead of racing it.
export function installScript(version = PASEO_CLI_VERSION): string {
  return [
    PREAMBLE,
    'exec 9>"$HOME/.paseo-plugin.lock"',
    "flock -w 600 9",
    `if [ "$(paseo --version 2>/dev/null | grep -o '[0-9][0-9.]*' | head -1)" != "${version}" ]; then`,
    `  npm install -g --no-audit --no-fund --loglevel=error @getpaseo/cli@${version}`,
    "fi",
    'mkdir -p "$PASEO_HOME" ' + WORKSPACE,
    "paseo --version",
    "for a in claude codex; do printf '%s: ' \"$a\"; command -v \"$a\" || echo missing; done",
  ].join("\n");
}

// Starts the daemon unless it already runs (as after a resume: a paused sandbox keeps its processes).
// `paseo daemon status` exits 0 for a stopped daemon too, so the JSON decides.
export function startDaemonScript(): string {
  const running = `paseo daemon status --home "$PASEO_HOME" --json 2>/dev/null | grep '"localDaemon": *"running"' >/dev/null`;
  return [
    PREAMBLE,
    'exec 9>"$HOME/.paseo-plugin.lock"',
    "flock -w 300 9",
    `cd ${WORKSPACE}`,
    `if ! ${running}; then`,
    // 9>&-: the daemon must not inherit the lock, or it would hold it for as long as it runs.
    '  paseo daemon start --home "$PASEO_HOME" 9>&- >> "$PASEO_HOME/daemon-start.log" 2>&1 || true',
    "fi",
    "for i in $(seq 1 90); do",
    `  if ${running}; then echo running; exit 0; fi`,
    "  sleep 1",
    "done",
    'tail -20 "$PASEO_HOME/daemon-start.log" >&2 || true',
    "exit 1",
  ].join("\n");
}

export function pairScript(): string {
  return [PREAMBLE, 'paseo daemon pair --relay --json --home "$PASEO_HOME"'].join("\n");
}

// A pairing link carries its offer in the fragment: a version, the daemon's server id and public key,
// and the relay endpoint. Anything else is refused before it is stored or shown.
export function parsePairingUrl(output: string): string {
  let url: string | undefined;
  try {
    url = (JSON.parse(output.trim()) as { url?: string }).url;
  } catch {
    throw new Error("pairing did not return JSON");
  }
  if (!url) throw new Error("pairing returned no link");
  const parsed = new URL(url);
  if (parsed.protocol !== "https:") throw new Error("pairing link is not https");
  if (!parsed.hash.startsWith("#offer=")) throw new Error("pairing link has no offer");
  const offer = JSON.parse(Buffer.from(parsed.hash.slice("#offer=".length), "base64url").toString("utf8")) as {
    serverId?: unknown;
    daemonPublicKeyB64?: unknown;
    relay?: { endpoint?: unknown };
  };
  if (typeof offer.serverId !== "string" || !offer.serverId) throw new Error("pairing offer has no server id");
  if (typeof offer.daemonPublicKeyB64 !== "string" || Buffer.from(offer.daemonPublicKeyB64, "base64").length !== 32) {
    throw new Error("pairing offer has no daemon key");
  }
  if (typeof offer.relay?.endpoint !== "string" || !offer.relay.endpoint) throw new Error("pairing offer has no relay");
  return url;
}

export function serverIdOf(url: string): string {
  const hash = new URL(url).hash.slice("#offer=".length);
  return (JSON.parse(Buffer.from(hash, "base64url").toString("utf8")) as { serverId: string }).serverId;
}

// The last lines of a failed step, for the host's error line. Never more than a few hundred bytes.
export function tail(text: string, lines = 4): string {
  return text.trim().split("\n").slice(-lines).join("\n").slice(-400);
}
