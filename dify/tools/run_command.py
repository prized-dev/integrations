import plugin_tool


class RunCommandTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.run_command)
