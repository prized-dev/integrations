import type { PluginClientContext, PluginScreenProps, PluginSidebarItemProps } from "@getpaseo/plugin/client";
import { SidebarRow } from "@getpaseo/plugin/client/ui";

import { HostsScreen } from "./client/hosts.tsx";
import { SettingsScreen } from "./client/settings.tsx";

function HostsItem({ currentScreen, openScreen }: PluginSidebarItemProps) {
  return <SidebarRow icon="Boxes" active={currentScreen?.screenId === "hosts"} onPress={() => openScreen({ screenId: "hosts" })} />;
}

export default function contribute(client: PluginClientContext) {
  const Hosts = (props: PluginScreenProps) => <HostsScreen {...props} openSettings={client.openSettings} />;
  const removeScreen = client.addScreen({ id: "hosts", title: "Prized sandboxes", Component: Hosts });
  const removeItem = client.addSidebarHeaderItem({ id: "hosts", title: "Prized sandboxes", Component: HostsItem });
  const removeSettings = client.addSettingsScreen({ id: "prized", title: "Prized Sandbox", icon: "CloudCog", Component: SettingsScreen });
  return () => {
    removeSettings();
    removeItem();
    removeScreen();
  };
}
