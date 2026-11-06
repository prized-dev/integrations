# Hermes plugin catalog: facts for the PR (drafter input, not PR text)

Rebuilt 11-06 because tasks/hermes-catalog-1103 (card payload, make_card.py) was lost in the 11-04 task-dir loss.

## What the PR adds
- One file: `plugin-catalog/prized.yaml` (copy of `prized.yaml` beside this file). Name `prized` is free in the catalog (checked 11-06).
- Pin: `prized-dev/integrations@6abf570a047be10c5e7a8e1cdbd868fa226550d9`, `subdir: hermes`, version 0.1.0.
  That is integrations PR #13's head. RE-PIN to #13's squash-merge commit on main before filing (a PR-branch commit can disappear after the branch is deleted).
- Submitter must be the repo owner or a major contributor (rule 5): HudsonGri has admin on prized-dev/integrations (checked 11-06). Not a public org member, so say in the PR that Prized makes and maintains it.

## Hermes surfaces used (from the code at 6abf570)
- `ctx.register_terminal_environment_provider(PrizedProvider)`: name `prized`, display "Prized Sandboxes", `is_remote=True`, `is_container=True`, `session_isolated_when_nonpersistent=False`, `default_cwd=/home/prized/work`, `cache_path_base=~/.hermes`, `strip_env_keys={PRIZED_API_KEY}`, `probe()` answers from the key alone (creates nothing), `doctor_checks()` rows.
- Environment subclasses `BaseEnvironment`; uses `FileSyncManager`, `iter_sync_files`, `quoted_rm_command` from `tools.environments.file_sync` and `hermes_constants.get_hermes_home` (expect rule-9 "undocumented core import" warnings, same as Upstash Box's entry #148115). It also falls back to the private `_ThreadedProcessHandle` from `tools.environments.base_output` / `base`: another rule-9 warning, so say why in the PR (the Upstash PR did the same).
- No tools, hooks or middleware. Key read with `agent.secret_scope.get_secret` (rule 11). No SDK: plain httpx to `https://api.prized.dev/api/v1` (setting `api_url`). User-Agent `hermes-plugin-prized/0.1.0`. No telemetry, no self-update.
- `requires_hermes: ">=0.22.5"` in both the entry and plugin.yaml (rule 14). Current Hermes release v0.23.0 (11-05 21:32Z). No commits touched tools/environments/base.py, file_sync.py, agent/terminal_env_*.py or plugins_registrars.py since 11-02 (GitHub API, 11-06), so the 11-03 test results should still hold [inferred, not re-run].

## Disclosures (rule 13), all from README/code at 6abf570
- Network: every command and the files the file tools touch go to Prized (api.prized.dev).
- Files leaving the machine: skills (incl. external and project skill folders), `~/.hermes` cache, credential files a skill or `terminal.credential_files` names (never `.env` or `auth.json`); changed skill and cache files copied back on release; credential files upload-only.
- Credentials: `PRIZED_API_KEY` stripped from every subprocess, never enters the sandbox.
- Billing/account state: sandboxes created, paused, resumed in the key's workspace and billed to it; labels `hermes=1`, `hermes_home=<digest>` (+ `hermes_ephemeral=1` for non-persistent). Persistent mode (default) never deletes a sandbox; paused = storage price until deleted (free tier: 30 days after last use). Default auto-pause after 10 idle min; default run-time 24 h (60 min total on free tier), then the next command starts a fresh sandbox.
- Approvals: dangerous-command approval skipped in the sandbox (`is_container`), like the other cloud backends; `approvals.deny` still applies.

## Evidence on file (11-03 journal, lead's): validate + catalog tests + 34 unit + 3 live + E2E on Hermes main, v0.22.6, v0.22.5 at the earlier pin 82313ad; integrations #13 CI green at 6abf570 (36/36 checks, 11-06).
## Gap before filing: re-run `hermes plugins validate --install-deps` and `scripts/validate_plugin_catalog.py` with this yaml at the FINAL pin, in a keyless sandbox (no Hermes code on agent-xp).

## Precedent: catalog merges come in maintainer batches (teknium1: ~13 on 11-02, ~35 on 11-05); Upstash Box terminal backend 3h16m; median ~51 h (11-03 count).
