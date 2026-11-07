# Docs patches for open-source coding agents (11-07)

Each folder holds one `git format-patch` commit for another project's repo, authored as Hudson with no trailer. Apply it to a branch of your fork with `git am`. This branch is not for merging.

| Folder | Repo, base | Change | Before filing |
| --- | --- | --- | --- |
| `openhands-docs/` | OpenHands/docs `main` @ bde5151 | New page `openhands/usage/agent-canvas/backend-setup/prized.mdx`: the Agent Canvas backend on a Prized box, a systemd service on loopback port 8100, reached with `prized proxy`. Also a nav entry and a row in `remote.mdx` | Nothing. Tested live on a box 11-07, `mint broken-links` green |
| `pi/` | earendil-works/pi `main` @ a4ccccb | "Route tools through a Prized sandbox" section and table row in `packages/coding-agent/docs/containerization.md` | `@prized/pi` on npm and `pi/` on this repo's main. An issue with a maintainer's `lgtm` (pi auto-closes PRs from new contributors) |
| `opencode/` | anomalyco/opencode `dev` @ c33eaa1 | One row in the ecosystem plugins table | `opencode-prized` on npm and `opencode/` on this repo's main |

How the OpenHands page was tested, on a box running agent-canvas 1.37.0 and agent-server 1.60.1:
- Through the tunnel: 401 without the key and 200 with it, and CORS from `localhost:8000` allowed.
- Bash and a 3 MB upload and download through the agent-server API.
- One real OpenHands agent turn on the box.
- The service came back after a pause/resume and after a resize.

`prized proxy` does not wake a suspended box; the page says to run `prized box resume`.
