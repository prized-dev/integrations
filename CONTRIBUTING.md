# Contributing

Issues and pull requests are welcome. Say which folder (integration) they're about in the title.

## Run the tests

```sh
scripts/test.sh <folder>            # lint + offline tests, in a fresh virtualenv or temp copy
FLOOR=1 scripts/test.sh <folder>    # against the oldest framework versions the package supports
PYTHON=python3.10 scripts/test.sh <folder>
```

The script needs Python 3.10+ (3.11+ for `pydantic-ai`, 3.12+ for `harbor` and `dify`), Node.js 20+ for `langchain-js`, and `jq` for `claude-managed-agents`. It unsets `PRIZED_API_KEY` and, where it can (sudo or user namespaces), cuts the network off while the tests run, so a test that reaches the real API fails.

Until the Prized SDKs are on PyPI and npm (2026-11-05), folders that need them are skipped unless `PRIZED_SDK_DIST` points at a directory with the SDK's wheel and tarball.

## Live tests

Each folder's README says how to run its tests against the real API. They create real sandboxes, billed to your Prized workspace, and delete them at the end. Use your own API key, from the environment only.

## Pull requests

- Keep a change to one folder where you can, and add or update its offline tests.
- Never commit a key, a `.env` file or a token, even a revoked one. Tests use obvious fakes (`dcp_000…`).
- CI must pass. It runs the same `scripts/test.sh` for every folder, with no secrets.

## Add an integration

1. A new folder with its package, `README.md` (install, usage, behaviour, limits, tests), `LICENSE` (Apache-2.0) and offline tests that need no key.
2. A case for it in `scripts/test.sh`, rows in `.github/workflows/ci.yml`, and a row in the root README.
3. For a Python package, add it to the `package` choices in `.github/workflows/publish-pypi.yml`.

## Releases

Maintainers publish a Python package by dispatching `publish-pypi.yml` from `main` with its folder (dry run first). Bump the version in a commit before that.

## Security

Report a vulnerability to team@prized.dev rather than in a public issue.
