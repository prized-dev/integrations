import type { PluginServerContext } from "@getpaseo/plugin/server";

import { HostService } from "./server/service.ts";
import { defaultStateDir } from "./server/store.ts";
import { actRpc, addHostRpc, createRpc, revealPairingRpc, saveSettingsRpc, statusRpc } from "./shared/rpc.ts";

export default function contribute(server: PluginServerContext) {
  const service = new HostService({ stateDir: defaultStateDir() });
  server.handle(statusRpc, (input) => service.status(input.refresh ?? false));
  server.handle(createRpc, (input) => service.create(input.label));
  server.handle(actRpc, (input, context) => service.act(input, context.paseo.hosts));
  server.handle(revealPairingRpc, (input) => service.revealPairing(input.hostId));
  server.handle(addHostRpc, (input, context) => service.addHost(input.hostId, context.paseo.hosts));
  server.handle(saveSettingsRpc, (input) => service.saveSettings(input));
  return () => service.dispose();
}
