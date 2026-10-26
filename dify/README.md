# Prized Sandboxes for Dify

Give Dify workflows and agents a Linux machine to run code on. Each sandbox is a [Prized Sandbox](https://prized.dev/docs/sandboxes?via=dify): a Firecracker microVM with Ubuntu 24.04, Python 3, Node.js 22, git and build tools, that can pause with its memory and resume where it stopped.

Source: [github.com/prized-dev/integrations](https://github.com/prized-dev/integrations/tree/main/dify). Maintained by Prized (team@prized.dev).

## Setup

1. Make an API key at [prized.dev](https://prized.dev/dashboard/workspace/api-keys?via=dify) with **New key**: choose **Custom** with only **Sandboxes: Write** (everything else **None**) and an expiry. On a workspace with no sandboxes yet, **Create API key** on [Sandboxes](https://prized.dev/onboarding/sandboxes?via=dify) makes such a key in one click (sandboxes only, expires after 90 days). A **Full access** key also works, but it reaches your whole workspace. A new workspace's owner gets free credit once ([Billing](https://prized.dev/docs/billing?via=dify#free-credit)); on the free credit alone, sandboxes run within the [free tier](https://prized.dev/docs/sandboxes?via=dify#free-tier) (up to 2 vCPU and 4 GB each, 60 minutes of running per sandbox). No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=dify.
2. Build the `.difypkg` (see [Development](#development)) and upload it under **Plugins > Install plugin > Local package file**. The package is not signed: a self-hosted Dify verifies plugin signatures by default and installs it only with `FORCE_VERIFYING_SIGNATURE=false` (or once you sign it with your own key). It has not yet been installed in a real Dify, Dify Cloud included; so far it has run under Dify's plugin runtime (`dify plugin run`).
3. Open the plugin, choose **Authorize**, and paste the key. On a self-hosted Dify, also fill in **Dify file URL** for Upload file (see [Files from the workflow](#files-from-the-workflow)). Dify keeps both in its credential store. The check is a read that creates nothing, so it cannot tell a read-only key from one that can write: a read-only key passes it and then fails on Create sandbox.
4. Add the tools to a workflow or an agent.

Sandboxes are billed to your Prized workspace per second at the [sandbox rates](https://prized.dev/docs/billing?via=dify#sandboxes); the plugin itself is free.

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

### Files from the workflow

To work on a file a user uploads, pass the Start node's file variable to **Upload file**; it lands in `/home/prized/work` under its own name. Upload file fetches the file from the URL Dify gives the plugin, without your Prized key, and by default only from a public address: it refuses IP addresses and hosts that resolve to private, loopback, link-local or known cloud metadata addresses, and connects only to an address it checked.

A recent self-hosted Dify gives plugins file URLs on its `INTERNAL_FILES_URL` when that is set, else on `FILES_URL`; often that is a private address such as `http://api:5001`. Put that same value in the plugin's **Dify file URL** setting, and Upload file also fetches files under its `/files/` path, private address included (never a link-local or known cloud metadata address). If neither is set, Dify gives plugins no address for files and Upload file cannot work. Upload file has not yet been tried with a real Dify's file URLs, Dify Cloud's included; for text, Write file works anywhere.

## Behaviour and limits

- **Where things run.** Commands run under `bash -c` as the user `prized`, without sudo, starting in `/home/prized/work`. Relative paths start there; absolute paths must be inside `/home/prized`. Install Python packages in a venv (`python3 -m venv .venv && .venv/bin/pip install pandas`).
- **Output.** Each of `stdout` and `stderr` returns up to 64 KiB: the start and the end, with `truncated: true` when something was cut. Write large output to a file and read it.
- **Long jobs.** A command can run at most 240 s per call. Start longer jobs in the background (`nohup ./job.sh > job.log 2>&1 &`) and read `job.log` with later calls. A background job is not activity: a sandbox with no commands or file transfers for its idle pause time (10 minutes by default) pauses, and the job with it. Check on the job more often than that, or raise **Pause when idle** on Create sandbox.
- **Labelled sandboxes only.** The plugin labels each sandbox it creates `integration=dify` and refuses to act on a sandbox without that label, so an agent cannot touch the workspace's other sandboxes. (Any sandbox someone else labels `integration=dify` in the same workspace is reachable too.)
- **Internet.** Code in a sandbox can reach the internet. The plugin does not filter that traffic.
- **Run time.** A sandbox is deleted when its run time runs out (60 minutes by default here). On a paid workspace a resume restarts the clock; on the free tier a sandbox gets 60 minutes of running in all. Copy out what you need first.
- **Retries.** Reads and state changes are retried on transient errors. A command or upload is retried only when the API shows it never reached the sandbox, so nothing runs twice. When no clear answer comes back (a lost connection, a timeout, or a server error), the tool says the command may or may not have run, or the file may or may not have been written.

## Credentials and connections

The plugin sends your API key only to `api.prized.dev` and to `term.prized.dev` (each sandbox's command endpoint), over HTTPS, in the `Authorization` header. It does not follow redirects, put the key in a sandbox, return it in a tool result, or include it in error messages or logs. The only other connection is **Upload file** fetching the chosen file from the URL Dify gives it, without the key: from a public address, or from the Dify file server named in the plugin's settings. Errors show the Prized API's error code with a fixed sentence or with the API's own short message, with anything shaped like a key removed; never a raw HTTP body.

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

Package it with the Dify plugin CLI, from the repository root: `dify plugin package ./dify -o prized_sandboxes-0.1.0.difypkg`.

## Support

Open an issue at [prized-dev/integrations](https://github.com/prized-dev/integrations/issues), or email team@prized.dev. Report a security issue to team@prized.dev, not in a public issue.

## License

Apache-2.0
