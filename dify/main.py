from dify_plugin import DifyPluginEnv, Plugin

# The longest call is a run in a one-off sandbox: its create, the upload, a command of up to
# 240 s and the delete. 600 s matches Dify's default plugin execution limit.
plugin = Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=600))

if __name__ == "__main__":
    plugin.run()
