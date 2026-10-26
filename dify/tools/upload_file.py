import plugin_tool


class UploadFileTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.upload_file)
