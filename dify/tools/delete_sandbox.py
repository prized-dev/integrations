import plugin_tool


class DeleteSandboxTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.delete_sandbox)
