import plugin_tool


class ResumeSandboxTool(plugin_tool.PrizedTool):
    run = staticmethod(plugin_tool.resume_sandbox)
