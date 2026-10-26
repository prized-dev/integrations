import plugin_tool


class CreateSandboxTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.create_sandbox)
