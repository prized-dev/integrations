# Plugin Submission

## Plugin information

- **Author**: prized
- **Plugin name**: prized_sandboxes (Prized Sandboxes)
- **Version**: 0.1.0
- **Source repository**: https://github.com/prized-dev/integrations/tree/main/dify
- **Contact**: team@prized.dev

## Submission type

- [x] New plugin
- [ ] Version update

## What changed

Adds ten tools for Prized Sandboxes (prized.dev), Linux microVMs over an API: create a sandbox, run a shell command, run a Python, JavaScript or bash snippet, write a file, upload a workflow file, read a file (as text or as a workflow file), get its state, pause (keeping its memory and running processes), resume, and delete. Run command and Run code without a sandbox ID use a one-off sandbox that is deleted after the run. Each tool returns its fields as workflow output variables and as JSON. It is for workflow and agent steps that need a whole Linux machine: installing packages, running a build or a test suite, keeping a process running, or keeping files between steps. A Prized account and API key are required; Prized bills sandbox use, and the plugin has no charge of its own.

## Risk level

- [ ] Low risk
- [ ] Medium risk
- [x] High risk

## Required checks

- [x] I have read and followed the [Marketplace submission requirements](https://github.com/langgenius/dify-plugins/blob/main/docs/plugin-submission-requirements.md).
- [x] I have read and comply with the [Plugin Development Guidelines](https://docs.dify.ai/en/develop-plugin/publishing/standards/contributor-covenant-code-of-conduct).
- [x] I tested this plugin on Dify Community Edition and Dify Cloud, or documented any limitation below.
- [x] The package contains only files needed at runtime.
- [x] The package does not contain secrets, local credentials, `.env` files, `.git` directories, virtual environments, caches, logs, or IDE files.
- [x] The package does not contain executables or bundled binaries, or I explained why they are required below.
- [x] The plugin README includes setup steps, usage instructions, required APIs or credentials, connection requirements, and the source repository link.
- [x] The plugin includes `PRIVACY.md` or a hosted privacy policy, and `manifest.yaml` references it.
- [x] All user-facing text is primarily in English, with any localized README files following the [i18n guidance](https://docs.dify.ai/en/develop-plugin/features-and-specs/plugin-types/multilingual-readme).

## Security and privacy notes

- Sandbox control with command and code execution and file operations, inside a remote Prized sandbox, never on the Dify host. Commands run as the unprivileged user `prized` (no sudo); file paths are limited to `/home/prized`, checked by the plugin and by the API. Code in a sandbox can reach the internet; the plugin does not restrict guest egress.
- The Prized key goes only to the fixed hosts `api.prized.dev` and `term.prized.dev` (the command endpoint is always `https://term.prized.dev`; a sandbox document naming another host is ignored), over HTTPS, in the `Authorization` header, with timeouts and no redirects. It is never put in a sandbox, a tool result, an error, or a log. Errors carry the API's error code with a fixed sentence or the API's own short message (key-shaped text removed), never a raw HTTP body. The README asks for a Custom key with only Sandboxes: Write and an expiry.
- The plugin acts only on sandboxes labelled `integration=dify`, the label it puts on every sandbox it creates; every other tool reads the sandbox first and refuses one without it.
- Upload file fetches a user-selected workflow file from the URL Dify passes (arbitrary URL fetching). It refuses IP literals and hosts with any address that is not global (private, loopback, link-local, and IPv6 forms that reach IPv4: mapped, compatible, NAT64, 6to4, Teredo, site-local) or is a known cloud metadata address, then connects only to an address it checked (Host header and TLS SNI carry the name, and the certificate is verified for the name), with no proxy from the environment and no redirects. It asks for an uncompressed body, refuses a compressed one, refuses a Content-Length over 50 MiB, stops reading at 50 MiB and after 240 s, sends no Prized key, and never repeats the URL in errors. An optional provider setting, Dify file URL, is for a self-hosted Dify whose `INTERNAL_FILES_URL` is a private address (such as `http://api:5001`): it admits that one origin, only under its `/files/` path, and never a link-local or known cloud metadata address.
- Input constraints: vCPU, memory, run time and auto-pause/delete are form parameters (set by the builder, not the model), with defaults of 2 vCPU, 4 GB and 60 minutes; one-off sandboxes are 2 vCPU, 4 GB, deleted after the run or by their 15-minute run time. Commands time out at 1 to 240 s; output returned is capped at 64 KiB per stream, text reads at 256 KiB, file reads at 15 MiB, text writes at 5 MiB, uploads at 50 MiB. A command or upload is never re-sent when it may have reached the sandbox: a lost answer, a timeout or a server error after sending is reported as "may or may not have run" (or "may or may not have been written"), and a success whose body cannot be read as "ran, but its answer could not be read".
- `PRIVACY.md` says what goes where and links the Prized privacy policy (https://prized.dev/privacy).

## Local validation

```sh
cd dify
ruff check . && ruff format --check .
pytest                                                          # offline, against a fake Prized API
DIFY_CLI_PATH=/path/to/dify pytest tests/test_dify_runtime.py   # packaged, under `dify plugin run`
PRIZED_LIVE=1 pytest tests/test_live.py                         # real API; key exported in the environment
cd ..
dify plugin package ./dify -o prized_sandboxes-0.1.0.difypkg
python3 dify-marketplace-toolkit/validator/validate-difypkg.py prized_sandboxes-0.1.0.difypkg --pr-body-file pr-body.md
```

Results (2026-11-01, source commit [`727c9d4`](https://github.com/prized-dev/integrations/tree/727c9d4c6864054622984ec87164b74501d6f3e6/dify) on main, on the archive in this PR, SHA-256 `a410dce59c8e23a63a4d2e1f33fa07a2f0f0cac1652c139299904e9a324429e9`):

- Lint clean; 205 offline tests pass with the network cut, including the Dify SDK loading the manifest, the provider and all ten tools, Upload file's address checks, and a local TLS server proving the certificate is checked for the name when the connection goes to the pinned IP.
- Under `dify plugin run` (CLI 0.6.12) against a local fake of the Prized API, the packaged plugin validated credentials, created a sandbox, wrote, ran and read files, returned a file blob, refused a loopback file URL, then fetched the same file once the Dify file URL setting named that server (and still refused a path outside its `/files/`), ran a one-off snippet, refused a bad sandbox ID and a wrong key (without echoing it), and deleted the sandbox. The package holds only runtime files.
- Against the live Prized API (2026-10-28, same `dify/` tree), every tool ran on one sandbox (including pause, wake by command, resume, a command timeout, upload of a public HTTPS file over the pinned connection, and a one-off run), and no sandbox was left behind.
- `validate-difypkg.py` with this PR body: 0 blocking failures (one recommendation: `meta.minimum_dify_version`). The pre-check workflow's other steps, run locally with the same toolkit: package path check, dependency install, plugin install test, and `uploader --test` packaging check all passed.

## Reviewer notes

- Tested with Dify's plugin runtime (`dify plugin run`, CLI 0.6.12, through `dify_plugin`'s `PluginRunner`) and the live Prized API. Not yet installed in a Dify Community Edition or Dify Cloud workspace, and Upload file not yet tried with a real Dify's file URLs: a documented limitation.
- Upload file reads the file with its own `httpx` client (`client.stream`), not `dify_plugin`'s `File.blob`, so that it can pin the checked address and cap the size.
- `network.domains` lists the two fixed Prized hosts that receive the key. Upload file also connects to the file's own URL as given by Dify, which cannot be listed in advance.
- Dependencies: `dify-plugin==0.10.2` and `httpx==0.28.1` (already a dependency of `dify-plugin`). Python 3.12, amd64 and arm64. No bundled binaries.
- `meta.minimum_dify_version` is omitted: no minimum version has been established.
