import type { PluginSurfaceProps } from "@getpaseo/plugin/client";
import { useRpc } from "@getpaseo/plugin/client";
import { ExternalLink, SettingsAction, SettingsCard, SettingsInput, SettingsSection, SettingsSelect, useToast } from "@getpaseo/plugin/client/ui";
import { useEffect, useState } from "react";
import { Text, View } from "react-native";

import { saveSettingsRpc, statusRpc, type PublicSettings } from "../shared/rpc.ts";
import { message } from "./hosts.tsx";

const SHAPES = [
  { label: "2 vCPU, 4 GB (fits the free tier)", value: "2x4" },
  { label: "4 vCPU, 8 GB", value: "4x8" },
  { label: "8 vCPU, 16 GB", value: "8x16" },
  { label: "16 vCPU, 32 GB", value: "16x32" },
] as const;

const RUNTIMES = [
  { label: "1 hour", value: "1" },
  { label: "24 hours", value: "24" },
  { label: "3 days", value: "72" },
  { label: "7 days", value: "168" },
] as const;

export function SettingsScreen({ theme }: PluginSurfaceProps) {
  const getStatus = useRpc(statusRpc);
  const save = useRpc(saveSettingsRpc);
  const toast = useToast();
  const [settings, setSettings] = useState<PublicSettings | null>(null);
  const [prizedKey, setPrizedKey] = useState("");
  const [anthropicKey, setAnthropicKey] = useState("");
  const [openaiKey, setOpenaiKey] = useState("");
  const [saving, setSaving] = useState(false);

  async function load() {
    try {
      setSettings((await getStatus({})).settings);
    } catch (error) {
      toast.error(message(error));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function submit(input: Parameters<typeof save>[0], done: string) {
    setSaving(true);
    try {
      const result = await save(input);
      toast.show(result.workspace ? `${done} Workspace: ${result.workspace}.` : done, { variant: "success" });
      setPrizedKey("");
      setAnthropicKey("");
      setOpenaiKey("");
      await load();
    } catch (error) {
      toast.error(message(error));
    } finally {
      setSaving(false);
    }
  }

  const muted = { color: theme.colors.foregroundMuted };
  const keyHint =
    settings?.apiKeySource === "saved"
      ? `A key is saved${settings.workspace ? ` (workspace ${settings.workspace})` : ""}. Enter a new one to replace it.`
      : settings?.apiKeySource === "environment"
        ? "Using PRIZED_API_KEY from the daemon's environment. A key saved here takes its place."
        : "Needs Sandboxes: Write. It is stored on the daemon's machine in a file only its user can read, and sent only to Prized.";

  return (
    <View style={{ gap: 16 }}>
      <SettingsSection title="Prized">
        <SettingsCard>
          <SettingsInput label="Prized API key" hint={keyHint} placeholder="dcp_..." secureTextEntry onChangeText={setPrizedKey} disabled={saving} />
          <SettingsAction label="Save the key" actionLabel="Save" disabled={saving || !prizedKey.trim()} onPress={() => submit({ prizedApiKey: prizedKey }, "Key saved.")} />
          {settings?.apiKeySource === "saved" ? <SettingsAction label="Remove the saved key" actionLabel="Remove" disabled={saving} onPress={() => submit({ prizedApiKey: "" }, "Key removed.")} /> : null}
        </SettingsCard>
        <ExternalLink href="https://prized.dev/dashboard/workspace/api-keys?via=paseo">
          <Text style={{ color: theme.colors.accent }}>Make a key at prized.dev</Text>
        </ExternalLink>
      </SettingsSection>

      <SettingsSection title="Agent sign-in">
        <Text style={muted}>
          Optional. Set on each sandbox created after you save them, as its environment variables, so Claude Code and Codex start signed in. Without them,
          sign the agent in from a terminal on the host.
        </Text>
        <SettingsCard>
          <SettingsInput
            label="ANTHROPIC_API_KEY"
            hint={settings?.anthropicKeySaved ? "Saved. Enter a new one to replace it." : "For Claude Code."}
            secureTextEntry
            onChangeText={setAnthropicKey}
            disabled={saving}
          />
          <SettingsInput
            label="OPENAI_API_KEY"
            hint={settings?.openaiKeySaved ? "Saved. Enter a new one to replace it." : "For Codex."}
            secureTextEntry
            onChangeText={setOpenaiKey}
            disabled={saving}
          />
          <SettingsAction
            label="Save agent keys"
            actionLabel="Save"
            disabled={saving || (!anthropicKey.trim() && !openaiKey.trim())}
            onPress={() => submit({ ...(anthropicKey.trim() ? { anthropicApiKey: anthropicKey } : {}), ...(openaiKey.trim() ? { openaiApiKey: openaiKey } : {}) }, "Agent keys saved.")}
          />
          {settings?.anthropicKeySaved || settings?.openaiKeySaved ? (
            <SettingsAction label="Remove the agent keys" actionLabel="Remove" disabled={saving} onPress={() => submit({ anthropicApiKey: "", openaiApiKey: "" }, "Agent keys removed.")} />
          ) : null}
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="New hosts">
        <SettingsCard>
          <SettingsSelect
            label="Size"
            value={settings ? `${settings.vcpu}x${settings.memoryGb}` : "2x4"}
            options={SHAPES}
            disabled={saving || !settings}
            onValueChange={(value) => {
              const [vcpu, memoryGb] = value.split("x").map(Number);
              void submit({ vcpu, memoryGb }, "Size saved.");
            }}
          />
          <SettingsSelect
            label="Running time per resume"
            hint="A sandbox is deleted when its running time ends. Stopped (paused) time does not count, and each resume starts it again."
            value={String(settings?.maxRuntimeHours ?? 24)}
            options={RUNTIMES}
            disabled={saving || !settings}
            onValueChange={(value) => void submit({ maxRuntimeHours: Number(value) }, "Running time saved.")}
          />
        </SettingsCard>
      </SettingsSection>
    </View>
  );
}
