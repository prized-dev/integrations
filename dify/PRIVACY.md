# Privacy

Prized Inc. maintains this Dify plugin. Questions: team@prized.dev.

## What the plugin collects and stores

The plugin itself collects no personal data, keeps no database, and has no analytics or tracking. It does not log commands, file contents, outputs, or your API key.

## What it sends, and where

- **Your Prized API key**, from Dify's credential store, goes over HTTPS to `api.prized.dev` and `term.prized.dev` in the `Authorization` header, to authenticate each request. It is never sent anywhere else, put in a sandbox, returned in a tool result, or written in an error message.
- **Tool inputs and results**, such as commands, code, file paths, file contents, and command output, pass between Dify and Prized to run in your sandbox. For Upload file, the plugin first fetches the chosen workflow file from the URL Dify gives it (a public address, or the Dify file server set in the plugin's settings), without your Prized key, then sends it to the sandbox.
- **At Prized**, this is Customer Content, processed under the [Prized privacy policy](https://prized.dev/privacy?via=dify), which says: "We don't use Customer Content to train AI models. We don't sell personal information." A deleted sandbox's saved disk and memory are kept up to 30 days after deletion, and server logs up to 1 year; the policy lists every retention period and the sub-processors.
- **Dify** may keep workflow inputs, outputs, and logs according to your Dify workspace's settings.

## Sandboxes

A sandbox holds the files and processes your workflow puts there until it is deleted: by the Delete sandbox tool, when its run time ends, or after it has been paused for the time set on Create sandbox. A one-off sandbox (Run command or Run code with no Sandbox ID) is deleted after its run, or at the end of its 15-minute run time if that delete does not happen. Code running in a sandbox can reach the internet and send data to any destination it chooses; the plugin does not restrict that traffic. Do not put secrets in commands, code, or files unless the code needs them.
