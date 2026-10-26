import plugin_tool


class ReadFileTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.read_file)
