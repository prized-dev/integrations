import plugin_tool


class WriteFileTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.write_file)
