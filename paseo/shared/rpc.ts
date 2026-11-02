import { defineRpc } from "@getpaseo/plugin";
import { z } from "zod";

// What the client sees. No credential and no pairing link is ever part of a status answer: the link
// opens the host's daemon, so it is handed out only by pairing.reveal, on an explicit press.

export const PHASES = ["creating", "installing", "starting", "ready", "pausing", "paused", "resuming", "deleting", "gone", "failed"] as const;

const host = z.object({
  id: z.string(),
  label: z.string(),
  sandboxId: z.string().optional(),
  phase: z.enum(PHASES),
  sandboxState: z.string().optional(),
  busy: z.boolean(),
  error: z.string().optional(),
  paired: z.boolean(),
  addedAsHost: z.boolean(),
  // Set when the saved key belongs to another workspace than the one that made this host.
  otherWorkspace: z.boolean().optional(),
  createdAt: z.string(),
  expiresAt: z.string().optional(),
});

const settings = z.object({
  apiKeySource: z.enum(["saved", "environment", "none"]),
  workspace: z.string().optional(),
  anthropicKeySaved: z.boolean(),
  openaiKeySaved: z.boolean(),
  vcpu: z.number().int(),
  memoryGb: z.number().int(),
  maxRuntimeHours: z.number().int().nullable(),
});

export const statusRpc = defineRpc({
  name: "prized-sandbox.status",
  input: z.object({ refresh: z.boolean().optional() }),
  output: z.object({ settings, hosts: z.array(host) }),
});

export const createRpc = defineRpc({
  name: "prized-sandbox.create",
  input: z.object({ label: z.string().max(60).optional() }),
  output: z.object({ hostId: z.string() }),
});

export const actRpc = defineRpc({
  name: "prized-sandbox.act",
  input: z.object({
    hostId: z.string(),
    action: z.enum(["stop", "resume", "retry", "delete", "forget"]),
    // Delete asks the person to type the sandbox id.
    confirm: z.string().optional(),
  }),
  output: z.object({ accepted: z.boolean() }),
});

export const revealPairingRpc = defineRpc({
  name: "prized-sandbox.pairing.reveal",
  input: z.object({ hostId: z.string() }),
  output: z.object({ url: z.string() }),
});

export const addHostRpc = defineRpc({
  name: "prized-sandbox.hosts.add",
  input: z.object({ hostId: z.string() }),
  output: z.object({ registrationId: z.string(), status: z.string() }),
});

export const saveSettingsRpc = defineRpc({
  name: "prized-sandbox.settings.save",
  input: z.object({
    // Secrets are only ever sent to the daemon, never read back. Omitted means "keep what is saved";
    // an empty string removes it.
    prizedApiKey: z.string().optional(),
    anthropicApiKey: z.string().optional(),
    openaiApiKey: z.string().optional(),
    vcpu: z.number().int().min(1).max(32).optional(),
    memoryGb: z.number().int().min(1).max(128).optional(),
    // null: back to Prized's default.
    maxRuntimeHours: z.number().int().min(1).max(168).nullable().optional(),
  }),
  output: z.object({ workspace: z.string().optional() }),
});

export type StatusOutput = z.output<typeof statusRpc.output>;
export type PublicHost = z.output<typeof host>;
export type PublicSettings = z.output<typeof settings>;
export type SaveSettingsInput = z.output<typeof saveSettingsRpc.input>;
export type ActInput = z.output<typeof actRpc.input>;
