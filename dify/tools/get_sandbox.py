import plugin_tool


class GetSandboxTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.get_sandbox)
