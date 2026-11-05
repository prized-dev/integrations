# Contributing

Issues and pull requests are welcome. Say which folder (integration) they're about in the title.

## Run the tests

```sh
scripts/test.sh <folder>            # lint + offline tests, in a fresh virtualenv or temp copy
FLOOR=1 scripts/test.sh <folder>    # against the oldest framework versions the package supports
PYTHON=python3.10 scripts/test.sh <folder>
```

The script needs Python 3.10+ (3.11+ for `pydantic-ai`, 3.12+ for `harbor` and `dify`), Node.js 22.18+ for `langchain-js` and `mastra` (22.19+ for `pi`), and `jq` for `claude-managed-agents` and `cursor`. The tests run with an empty environment (`PATH`, a fresh `HOME` and the few paths a folder's step names, so no `PRIZED_API_KEY` or other key reaches them) and, where it can (sudo or user namespaces), with the network cut off, so a test that reaches the real API fails. Dify's runtime step keeps the network for its own install of `requirements.txt`.

Folders that need a Prized SDK install it from PyPI or npm. To test against an SDK build that isn't published yet, point `PRIZED_SDK_DIST` at a directory with its wheel and tarball.

## Live tests

Each folder's README says how to run its tests against the real API. They create real sandboxes, billed to your Prized workspace, and delete them at the end. Use your own API key, from the environment only.

## Pull requests

- Keep a change to one folder where you can, and add or update its offline tests.
- Never commit a key, a `.env` file or a token, even a revoked one. Tests use obvious fakes (`dcp_000…`).
- A prized.dev link in a README, docstring, error message or package metadata (`pyproject.toml`, `package.json`) carries `?via=<folder>` (before any `#fragment`), so a sign-up that starts from it is counted for that integration.
- CI must pass. It runs the same `scripts/test.sh` for every folder, with no secrets.

## Add an integration

1. A new folder with its package, `README.md` (install, usage, behaviour, limits, tests), `LICENSE` (Apache-2.0) and offline tests that need no key.
2. A case for it in `scripts/test.sh`, rows in `.github/workflows/ci.yml`, and a row in the root README.
3. For a Python package, add it to the `package` choices in `.github/workflows/publish-pypi.yml`.

## Releases

Maintainers publish a Python package by dispatching `publish-pypi.yml` from `main` with its folder (dry run first). Bump the version in a commit before that.

## Security

Report a vulnerability to team@prized.dev rather than in a public issue.
