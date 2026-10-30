# Agent sign-up check

Can an AI agent sign up for a developer platform, pay for it and use it on its own? Seven checks, each read from a public file with a plain GET request and each held to a published spec. One Node file, no dependencies.

The dated results for 100 platforms are in [`results/`](results) (newest: [`results.json`](results.json)), with every response they came from in [`evidence/`](evidence). The page is [prized.dev/agent-signup-check](https://prized.dev/agent-signup-check?via=agent-signup-check).

## Run it on your domain

Node 18 or later:

```sh
curl -fsSLO https://raw.githubusercontent.com/prized-dev/integrations/main/agent-signup-check/agent-signup-check.mjs
node agent-signup-check.mjs example.com --api https://api.example.com/v1/me
```

The same file is served at `https://prized.dev/agent-signup-check.mjs`. `--api` is one endpoint of your API that needs a key. The checker calls it without one, to see how a signed-out agent is turned away. Without `--api` it tries `https://api.<your domain>/`.

From a run on 2026-10-30:

```text
prized.dev: 5 of 7 pass, 1 partial  (agent-signup-check/0.2.2, 2026-10-30T17:48:56.792Z)
  PASS  auth.md                      auth.md documents agent registration (https://api.prized.dev/api/v1/agent/identity)
  PASS  Protected resource metadata  Names 1 authorization server
  PASS  Sign-up by API               agent_auth names a registration endpoint (service_auth, identity_assertion)
  PASS  Key works without a browser  401 with a Bearer challenge that points to resource metadata (RFC 9728 section 5.1)
  FAIL  Spend limits                 No spend cap in metadata, auth.md or llms.txt
  PART  Payment handoff              auth.md mentions a payment path (billing_url) in prose; nothing machine-readable
  PASS  Docs in Markdown             llms.txt found
  who has to act at sign-up: the person confirms an email code
```

Options: `--docs URL` tries a second llms.txt (default `https://<domain>/docs/llms.txt`), `--json` prints the full result, and `--save DIR` writes every response to a file, one per request.

## What it sends

GET requests only: no POST, no sign-up, no form, no credential. At most 16 requests per domain, redirects included (usually 7 to 9). https on port 443 only, and every address a name resolves to must be public (checked on each connection, redirects included). 1 MB and 8 seconds per response. It names itself in its User-Agent: `PrizedAgentSignupCheck/<version> (+https://prized.dev/agent-signup-check; read-only GET)`.

## The seven checks

Each check passes, passes partly, or fails. Partly means the platform says it in prose an agent can read, or publishes it in a shape the spec tells clients not to use, but not in the machine-readable form the spec defines. Ranking counts passes only; partly never breaks a tie.

| Check | Passes when | Partly when | Spec |
| --- | --- | --- | --- |
| **auth.md** | `/auth.md` is Markdown about agent sign-in, its heading names auth.md, and it documents a registration endpoint | it is published but says agents cannot register yet, or its heading doesn't name auth.md, or it documents no registration endpoint | [auth.md (WorkOS), AUTH.md v0.6.0](https://github.com/workos/auth.md/blob/main/AUTH.md) |
| **Protected resource metadata** | RFC 9728 metadata (from the API's 401 `resource_metadata`, or the well-known path on the site or API host) has an https `resource` that matches where it was found, and names an authorization server | it names no authorization server, or its `resource` doesn't match the URL it came from | [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) sections 2, 3, 3.3, 5.1 |
| **Sign-up by API** | the authorization server's RFC 8414 metadata has an `agent_auth` block with a registration endpoint and identity types, and its `issuer` checks out | the endpoint is really the token endpoint, the issuer doesn't match, or a sign-up API is documented only in auth.md or llms.txt prose | [auth.md Step 1b](https://github.com/workos/auth.md/blob/main/AUTH.md#1b-fetch-the-authorization-server-metadata), [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) section 3.3 |
| **Key works without a browser** | a signed-out call to the API gets `401` with a `WWW-Authenticate` challenge (or `402`) | `401` with no challenge, or `403`, or `400` or `422` asking for a key | [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#section-15.5.2) section 15.5.2, [RFC 6750](https://www.rfc-editor.org/rfc/rfc6750) section 3 |
| **Spend limits** | metadata declares a spend cap the agent's key will have (`agent_auth.billing.default_spend_cap` in the draft's shape, with its `version`, `pricing_uri`, `limits_endpoint` and `manage_uri`; or AAuth `budget_units` / `usage_endpoint`) | a `default_spend_cap` out of that shape, an `owner_limits_uri` (the owner can set limits, agents get no default cap), vendor fields about money or limits, or spend limits described in prose | [auth-md-spend-limits 0.1](https://github.com/prized-dev/integrations/blob/auth-md-spend-limits/auth-md-spend-limits/README.md) (draft, Prized), [draft-hardt-aauth-budgets](https://datatracker.ietf.org/doc/draft-hardt-aauth-budgets/) |
| **Payment handoff** | the API answers `402` with an x402 requirement, or `/.well-known/x402` is an x402 discovery document, or the API challenges with the `Payment` scheme or answers `402` with a billing object (top level, or JSON:API `meta`), or `agent_auth.billing` names a `pricing_uri` | a payment path (`402`, x402, `billing_url`, a checkout link) is described in prose | [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#section-15.5.3) section 15.5.3, [x402 v2 HTTP transport](https://github.com/x402-foundation/x402/blob/main/specs/transports-v2/http.md), auth-md-spend-limits section 4 |
| **Docs in Markdown** | `/llms.txt` (or the docs one) is text that starts with an H1 | a text file is there, but it doesn't start with an H1 | [llms.txt](https://llmstxt.org/) |

Every verdict carries its reason, the URL it was read from and, for prose, the sentence that matched, so you can judge it yourself. [METHOD.md](METHOD.md) has the rest: how the 100 were picked, which API URL was probed for each, the request order and the limits of what a public file can show.

Prized wrote this checker, and the spend-limits draft two of the checks accept. Prized is scored by the same code as everyone else and sorted with them. No platform, Prized included, has passed a check through that draft's fields.

## Think a row is wrong?

Open an issue titled `agent-signup-check: <platform>` with the check, what you expected, and the URL that shows it. Or email team@prized.dev. If the checker misread a page, we fix the checker and re-run; a cell is corrected by hand only when no fair rule can tell the difference, and then it says so in its reason ("Corrected after review", with the checker's own verdict beside it). A hand correction applies only while the page it was made about is unchanged (see `corrections` in [`targets.json`](targets.json)).

To add a platform, open a pull request that adds it to `targets.json`: its name, category, domain, and one documented API URL that needs a key. It joins the next weekly run.

## Re-run the 100

```sh
node agent-signup-check/weekly.mjs
```

It checks every platform in `targets.json` again (4 at a time, 300 ms between requests to one platform; about 2 minutes) and writes `results/<date>.json` and `.md`, `results.json`, `diffs/<date>.md` (who improved, who regressed, what else moved) and `evidence/`. It commits nothing. A platform whose request failed on the network is checked a second time, alone. A platform that still can't be reached keeps its last row, marked as not checked with the date, rather than dropping to zero. It stops without writing anything if more than 10 platforms are unreachable (then the problem is the machine's network, not the platforms), or if it can't learn the machine's public address to scrub from the evidence (pass `--client-ip` to give it). Exit 0 means written, and 1 means written with something to read in the diff: a stale correction, a replay mismatch, or a platform not checked.

Other tools in this folder:

- `node replay.mjs targets.json evidence/ out.json` re-scores saved evidence offline. Use it to see what a checker change does to a past run, or to confirm a published row from the responses it came from.
- `node build-index.mjs run.json targets.json results/` turns one `--targets` run into the published JSON and Markdown.
- `node scrub.mjs raw/ evidence/ --client-ip ADDR` prepares saved responses for publishing (below).

## Evidence

`evidence/<domain>/` holds one file per request of the newest run: the request, the status, the headers and the body, as the platform served them to an anonymous client. Git history keeps every earlier run, so `git log -p agent-signup-check/evidence/<domain>` shows what a platform changed and when. Before a run is committed, `scrub.mjs` drops cookies and the headers that echo the client's address. It replaces the machine's own address, in any spelling. In HTML pages it also replaces per-visitor values: anti-forgery tokens, session ids and Cloudflare challenge tokens. Then it cuts HTML pages (sign-in pages, 404 pages, bot walls) to their first 16 KB, noting the page's length and the SHA-256 of the body as published. Agent documents (Markdown, text, JSON) are kept byte for byte, except for the machine's own address if one ever appears. A replay of the scrubbed evidence gives the same verdicts as the live run, and `weekly.mjs` checks that on every run.

The responses are the platforms' own content, kept as a record of what each URL answered on the date shown. They are not covered by this folder's license.

## Tests

```sh
scripts/test.sh agent-signup-check
```

Offline, with a stub in place of the network: the address guards, the scrubber, corrections, the diff, a weekly run end to end from saved evidence, hostile 1 MB bodies, and a replay of the published evidence against the published results.

## License

[Apache-2.0](LICENSE), except the files in `evidence/` (see above).
