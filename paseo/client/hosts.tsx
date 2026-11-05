import type { PluginScreenProps } from "@getpaseo/plugin/client";
import { useRpc } from "@getpaseo/plugin/client";
import { copyText, ScrollView, TextInput, useToast } from "@getpaseo/plugin/client/ui";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Pressable, Text, View } from "react-native";

import { actRpc, addHostRpc, createRpc, revealPairingRpc, statusRpc, type PublicHost, type StatusOutput } from "../shared/rpc.ts";

const PHASE_LABEL: Record<PublicHost["phase"], string> = {
  creating: "Creating the sandbox",
  installing: "Installing Paseo",
  starting: "Starting the daemon",
  ready: "Running",
  pausing: "Stopping",
  paused: "Stopped (paused: memory and agents kept)",
  resuming: "Resuming",
  deleting: "Deleting",
  gone: "Gone",
  failed: "Failed",
};

// Paseo wraps a handler's error as "Request failed: <message> requestType=... code=...": keep the message.
export function message(error: unknown): string {
  const raw = error instanceof Error && error.message ? error.message : "";
  const text = raw.replace(/^Request failed:\s*/, "").replace(/\s+requestType=\S+(\s+code=\S+)?\s*$/, "").trim();
  return text || "The request failed. Check the plugin's logs.";
}

type Styles = {
  button: object;
  primary: object;
  text: object;
  primaryText: object;
};

// Defined once, outside the screen, so a 3-second refresh never remounts a button under a press.
function ActionButton({ label, onPress, primary, disabled, styles, pending }: { label: string; onPress: () => void; primary?: boolean; disabled?: boolean; styles: Styles; pending: boolean }) {
  const off = Boolean(disabled || pending);
  return (
    <Pressable accessibilityRole="button" disabled={off} onPress={onPress} style={[primary ? styles.primary : styles.button, { opacity: off ? 0.5 : 1 }]}>
      <Text style={primary ? styles.primaryText : styles.text}>{label}</Text>
    </Pressable>
  );
}

export function HostsScreen({ theme, layout, openSettings }: PluginScreenProps & { openSettings: (id: string) => void }) {
  const getStatus = useRpc(statusRpc);
  const create = useRpc(createRpc);
  const act = useRpc(actRpc);
  const reveal = useRpc(revealPairingRpc);
  const addHost = useRpc(addHostRpc);
  const toast = useToast();
  const [status, setStatus] = useState<StatusOutput | null>(null);
  const [label, setLabel] = useState("");
  const [pending, setPending] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState("");

  const refresh = useCallback(
    async (remote: boolean) => {
      try {
        setStatus(await getStatus({ refresh: remote }));
      } catch (error) {
        toast.error(message(error));
      }
    },
    [getStatus, toast],
  );

  // Local state every 3 seconds; Prized's view of each sandbox every 30 (paused elsewhere, run time over).
  useEffect(() => {
    void refresh(true);
    let tick = 0;
    const timer = setInterval(() => void refresh(++tick % 10 === 0), 3000);
    return () => clearInterval(timer);
  }, [refresh]);

  // A delete confirmation closes when its host moves on (deleted, busy, or into another phase).
  useEffect(() => {
    const host = status?.hosts.find((h) => h.id === confirming);
    if (confirming && (!host || host.busy || !["ready", "paused", "failed"].includes(host.phase))) setConfirming(null);
  }, [status, confirming]);

  async function run(work: () => Promise<unknown>, done?: string) {
    setPending(true);
    try {
      await work();
      if (done) toast.show(done, { variant: "success" });
      await refresh(false);
    } catch (error) {
      toast.error(message(error));
    } finally {
      setPending(false);
    }
  }

  async function copyPairing(host: PublicHost) {
    await run(async () => {
      const { url } = await reveal({ hostId: host.id });
      await copyText(url);
    }, "Pairing link copied. Paste it into Hosts, then Add host.");
  }

  const s = useMemo(
    () => ({
      screen: { flexGrow: 1, padding: layout.compact ? 16 : 24, gap: 16, backgroundColor: theme.colors.surface0 },
      title: { fontSize: 20, fontWeight: "700" as const, color: theme.colors.foreground },
      muted: { color: theme.colors.foregroundMuted },
      text: { color: theme.colors.foreground },
      danger: { color: theme.colors.statusDanger },
      card: { padding: 16, gap: 10, borderRadius: 12, borderWidth: 1, borderColor: theme.colors.border, backgroundColor: theme.colors.surface1 },
      row: { flexDirection: "row" as const, flexWrap: "wrap" as const, gap: 8, alignItems: "center" as const },
      button: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 8, borderWidth: 1, borderColor: theme.colors.border, backgroundColor: theme.colors.surface2 },
      primary: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 8, backgroundColor: theme.colors.accent },
      primaryText: { color: theme.colors.accentForeground, fontWeight: "600" as const },
      input: { borderWidth: 1, borderRadius: 8, borderColor: theme.colors.border, paddingHorizontal: 10, paddingVertical: 8, color: theme.colors.foreground, minWidth: 200 },
    }),
    [theme, layout.compact],
  );


  const settings = status?.settings;
  const hosts = status?.hosts ?? [];

  return (
    <ScrollView contentContainerStyle={s.screen}>
      <Text style={s.title}>Prized sandboxes</Text>
      <Text style={s.muted}>
        Each host is a Prized sandbox running its own Paseo daemon with Claude Code and Codex. Stop pauses it: its memory and processes (the daemon, terminals, agents) are kept,
        and only storage is billed. Resume carries on where it stopped.
      </Text>

      {settings?.apiKeySource === "none" ? (
        <View style={s.card}>
          <Text style={s.text}>Add a Prized API key to create hosts.</Text>
          <View style={s.row}>
            <ActionButton styles={s} pending={pending} label="Open settings" primary onPress={() => openSettings("prized")} />
          </View>
        </View>
      ) : (
        <View style={s.card}>
          <Text style={s.muted}>
            New hosts: {settings?.vcpu ?? 2} vCPU, {settings?.memoryGb ?? 4} GB,{" "}
            {settings?.maxRuntimeHours ? `up to ${settings.maxRuntimeHours} h of running per resume` : "Prized's default running time"}, in Oregon.
            {settings?.workspace ? ` Workspace: ${settings.workspace}.` : ""}
          </Text>
          <View style={s.row}>
            <TextInput style={s.input} placeholder="Name (optional)" placeholderTextColor={theme.colors.foregroundMuted} value={label} onChangeText={setLabel} />
            <ActionButton styles={s} pending={pending} label="Create host" primary onPress={() => run(() => create({ label: label || undefined }).then(() => setLabel("")), "Creating. Installing Paseo takes about a minute.")} />
            <ActionButton styles={s} pending={pending} label="Settings" onPress={() => openSettings("prized")} />
          </View>
        </View>
      )}

      {hosts.map((host) => (
        <View key={host.id} style={s.card}>
          <Text style={s.title}>{host.label}</Text>
          <Text style={host.phase === "failed" ? s.danger : s.text}>
            {PHASE_LABEL[host.phase]}
            {host.busy ? "..." : ""}
          </Text>
          {host.sandboxId ? (
            <Text style={s.muted}>
              Sandbox {host.sandboxId}
              {host.phase === "ready" && host.expiresAt ? `, deleted at ${new Date(host.expiresAt).toLocaleString()} unless stopped before then` : ""}
            </Text>
          ) : null}
          {host.error ? <Text style={s.danger}>{host.error}</Text> : null}
          {host.otherWorkspace ? null : (
          <View style={s.row}>
            {host.paired && (host.phase === "ready" || host.phase === "paused") ? (
              <>
                <ActionButton styles={s} pending={pending} label="Copy pairing link" onPress={() => copyPairing(host)} />
                {!host.addedAsHost ? <ActionButton styles={s} pending={pending} label="Add to this daemon's hosts" onPress={() => run(() => addHost({ hostId: host.id }), "Added to this daemon's hosts.")} /> : null}
              </>
            ) : null}
            {host.phase === "ready" ? <ActionButton styles={s} pending={pending} label="Stop" onPress={() => run(() => act({ hostId: host.id, action: "stop" }), "Stopping. Memory and agents are kept.")} /> : null}
            {host.phase === "paused" ? <ActionButton styles={s} pending={pending} label="Resume" primary onPress={() => run(() => act({ hostId: host.id, action: "resume" }), "Resuming.")} /> : null}
            {host.phase === "failed" && !host.busy ? <ActionButton styles={s} pending={pending} label="Retry" onPress={() => run(() => act({ hostId: host.id, action: "retry" }))} /> : null}
            {host.phase === "gone" ? <ActionButton styles={s} pending={pending} label="Remove from list" onPress={() => run(() => act({ hostId: host.id, action: "forget" }))} /> : null}
            {["ready", "paused", "failed"].includes(host.phase) && !host.busy ? (
              <ActionButton
                styles={s}
                pending={pending}
                label="Delete"
                onPress={() => {
                  setConfirming(host.id);
                  setConfirmText("");
                }}
              />
            ) : null}
          </View>
          )}
          {confirming === host.id ? (
            <View style={s.row}>
              <Text style={s.text}>Type {host.sandboxId ?? host.label} to delete the sandbox, its files and its agents for good.</Text>
              <TextInput style={s.input} value={confirmText} onChangeText={setConfirmText} autoCapitalize="none" autoCorrect={false} />
              <ActionButton
                styles={s}
                pending={pending}
                label="Delete for good"
                disabled={confirmText.trim() !== (host.sandboxId ?? host.label)}
                onPress={() => run(() => act({ hostId: host.id, action: "delete", confirm: confirmText }).then(() => setConfirming(null)), "Deleting.")}
              />
              <ActionButton styles={s} pending={pending} label="Cancel" onPress={() => setConfirming(null)} />
            </View>
          ) : null}
        </View>
      ))}
    </ScrollView>
  );
}
