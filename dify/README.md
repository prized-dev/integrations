# Prized Sandboxes for Dify

Give Dify workflows and agents a Linux machine to run code on. Each sandbox is a [Prized Sandbox](https://prized.dev/docs/sandboxes): a Firecracker microVM with Ubuntu 24.04, Python 3, Node.js 22, git and build tools, that can pause with its memory and resume where it stopped.

Source: [github.com/prized-dev/integrations](https://github.com/prized-dev/integrations/tree/main/dify). Maintained by Prized (team@prized.dev).

## Setup

1. Make an API key at [prized.dev](https://prized.dev/dashboard/workspace/api-keys): a **Full access** key, or a **Custom** key with **Sandboxes: Write**. A new workspace's owner gets free credit once ([Billing](https://prized.dev/docs/billing#free-credit)); on the free credit alone, sandboxes run within the [free tier](https://prized.dev/docs/sandboxes#free-tier) (up to 2 vCPU and 4 GB each, 60 minutes of running per sandbox).
2. Build the `.difypkg` (see [Development](#development)) and upload it under **Plugins > Install plugin > Local package file**. (A self-hosted Dify that verifies plugin signatures installs local packages only with `FORCE_VERIFYING_SIGNATURE=false`.)
3. Open the plugin, choose **Authorize**, and paste the key. Dify keeps it in its credential store. The check is a read that creates nothing, so it cannot tell a read-only key from one that can write: a read-only key passes it and then fails on Create sandbox.
4. Add the tools to a workflow or an agent.

Sandboxes are billed to your Prized workspace per second at the [sandbox rates](https://prized.dev/docs/billing#sandboxes); the plugin itself is free.

## Tools

| Tool | What it does |
| --- | --- |
| Create sandbox | Starts a sandbox and returns its `sandbox_id`. The template, vCPU (default 2), memory (default 4 GB), run time (default 60 minutes), idle pause (default 10 minutes), delete-when-paused (default 1 day) and labels are set by whoever configures the tool, not by the model. |
| Run command | Runs a bash command (default timeout 60 s, at most 240 s) and returns `exit_code`, `stdout`, `stderr`, `timed_out`. |
| Run code | Runs a Python, JavaScript (Node.js) or bash snippet and returns the same fields. |
| Write file | Writes up to 5 MiB of text to a file, creating folders as needed. |
| Upload file | Copies a workflow file (a user's upload, or another node's file output) into the sandbox, up to 50 MiB. |
| Read file | Returns a file's text (up to 256 KiB), or the file itself (up to 15 MiB) as a workflow file. |
| Get sandbox | Returns its state (`running`, `paused`, ...), shape and `expires_at`. |
| Pause sandbox | Pauses it: compute stops, memory and disk are kept. |
| Resume sandbox | Resumes it, processes where they stopped. A command or file tool sent to a paused sandbox also resumes it. |
| Delete sandbox | Deletes it and erases its disk and memory. |

Every tool returns its fields as workflow output variables (for example `sandbox_id`, `stdout`, `exit_code`) and as `json`.

**Run command** and **Run code** also work without a Sandbox ID: they then run in a one-off sandbox (2 vCPU, 4 GB) that is deleted after the run, and return `one_off: true` and no `sandbox_id`. If that delete fails, or Dify cuts the request off, the one-off sandbox's 15-minute run time deletes it. Use a sandbox from **Create sandbox** when files or installed packages must carry over between calls.

## Example workflow

1. **Create sandbox** with the defaults.
2. **Write file**: Sandbox ID = Create sandbox's `sandbox_id`, path `main.py`, content `print(6 * 7)`.
3. **Run command**: the same Sandbox ID, command `python3 main.py`. Its `stdout` is `42` and `exit_code` is `0`.
4. **Delete sandbox** with the same Sandbox ID.

Connect **Delete sandbox** to the failure branches too, so a failed run does not leave a sandbox behind. If one is left, it pauses when idle and is deleted when its run time or its delete-when-paused time runs out.

In an **Agent** node, **Run code** alone works as a code interpreter: with no Sandbox ID, each call runs in a fresh sandbox that is deleted afterwards. For work that spans calls, add Create sandbox, Run code (or Run command), Read file and Delete sandbox, and tell the agent to create one sandbox, work in it, and delete it at the end.

To work on a file a user uploads, pass the Start node's file variable to **Upload file**; it lands in `/home/prized/work` under its own name. Upload file fetches the file from the URL Dify gives it, and only from a public address: it refuses IP addresses and hosts that resolve to private, loopback or link-local addresses. On a self-hosted Dify whose `FILES_URL` is an internal address, Upload file is refused; use Write file for text instead.

## Behaviour and limits

- **Where things run.** Commands run under `bash -c` as the user `prized`, without sudo, starting in `/home/prized/work`. Relative paths start there; absolute paths must be inside `/home/prized`. Install Python packages in a venv (`python3 -m venv .venv && .venv/bin/pip install pandas`).
- **Output.** Each of `stdout` and `stderr` returns up to 64 KiB: the start and the end, with `truncated: true` when something was cut. Write large output to a file and read it.
- **Long jobs.** A command can run at most 240 s per call. Start longer jobs in the background (`nohup ./job.sh > job.log 2>&1 &`) and read `job.log` with later calls. A background job is not activity: a sandbox with no commands or file transfers for its idle pause time (10 minutes by default) pauses, and the job with it. Check on the job more often than that, or raise **Pause when idle** on Create sandbox.
- **Labelled sandboxes only.** The plugin labels each sandbox it creates `integration=dify` and refuses to act on a sandbox without that label, so an agent cannot touch the workspace's other sandboxes. (Any sandbox someone else labels `integration=dify` in the same workspace is reachable too.)
- **Internet.** Code in a sandbox can reach the internet. The plugin does not filter that traffic.
- **Run time.** A sandbox is deleted when its run time runs out (60 minutes by default here). On a paid workspace a resume restarts the clock; on the free tier a sandbox gets 60 minutes of running in all. Copy out what you need first.
- **Retries.** Reads and state changes are retried on transient errors. A command or upload is retried only when the API shows it never reached the sandbox, so nothing runs twice. When a command's answer is lost on the way back, the tool says the command may or may not have run.

## Credentials and connections

The plugin sends your API key only to `api.prized.dev` and to `term.prized.dev` (each sandbox's command endpoint), over HTTPS, in the `Authorization` header. It does not follow redirects, put the key in a sandbox, return it in a tool result, or include it in error messages or logs. The only other connection is **Upload file** fetching the chosen file from the URL Dify gives it (a public address only, without the key). Errors show the Prized API's error code with a fixed sentence or with the API's own short message, with anything shaped like a key removed; never a raw HTTP body.

See [PRIVACY.md](PRIVACY.md) for what data goes where.

## Development

The tests run offline against a fake Prized API:

```sh
pip install -r requirements.txt pytest ruff
ruff check . && ruff format --check .
pytest
```

With the [Dify plugin CLI](https://github.com/langgenius/dify-plugin-daemon/releases) and `uv` installed, `DIFY_CLI_PATH=/path/to/dify pytest tests/test_dify_runtime.py` also packages the plugin and runs it in Dify's plugin runtime (`dify plugin run`) against the fake API.

A live test creates one labelled sandbox in your workspace, uses every tool on it, and deletes it. Export your key in the environment first (never in a file):

```sh
PRIZED_LIVE=1 pytest tests/test_live.py
```

Package it with the Dify plugin CLI: `dify plugin package ./dify -o prized_sandboxes-0.1.0.difypkg`.

## Support

Open an issue at [prized-dev/integrations](https://github.com/prized-dev/integrations/issues), or email team@prized.dev. Report a security issue to team@prized.dev, not in a public issue.

## License

Apache-2.0
