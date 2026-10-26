import plugin_tool


class PauseSandboxTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.pause_sandbox)
