import plugin_tool


class RunCodeTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.run_code)
