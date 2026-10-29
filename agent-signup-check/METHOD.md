# Method

How the agent sign-up check reads a platform, how the 100 were chosen, and what the results can and can't show. The code is [`agent-signup-check.mjs`](agent-signup-check.mjs); where this page and the code disagree, the code is right and this page is a bug.

## The platforms

[`targets.json`](targets.json) lists 100 developer platforms that AI agents sign up for or call, in eight categories: sandboxes and compute (28), LLM APIs (20), email, phone and messaging (11), browsers and scraping (10), search and data (9), databases and backend (8), dev tools (8), payments and billing (6). Prized picked them. Sandboxes and compute is the largest group because it is Prized's own market: Prized and the platforms it competes with are all in it.

For each platform the list names one documented API URL that needs a key (`api`; listed as `apiProbed` in the results), and, where the docs live elsewhere, the docs llms.txt (`docs`). Five platforms have no API probe and are scored out of six: GraphQL-only APIs (Railway, Linear), platforms with no public REST API (Modal, Replit), and Stripe, whose API the tooling that made the first run was not allowed to call.

## The requests

GET only, with `Accept` set for what is expected (Markdown, JSON). In order, stopping early where a step has nothing to follow:

1. `https://<domain>/auth.md`, `https://<domain>/llms.txt` and the API URL, signed out.
2. A second llms.txt (the docs one, or `/docs/llms.txt`) when the root one is missing or doesn't start with an H1.
3. Protected resource metadata (RFC 9728): the URL the API's `401` names in `resource_metadata`, then `/.well-known/oauth-protected-resource` on the site, then the same path on the API host, stopping at the first one a client may use.
4. Authorization server metadata (RFC 8414) for the first authorization server that usable metadata names, else `https://<domain>/.well-known/oauth-authorization-server`.
5. When `/auth.md` is missing, the auth.md that `agent_auth.skill` points to.
6. When `/auth.md` is Markdown but not in the spec's shape, one made-up path (`/agent-signup-check-no-such-page.md`) to tell a real auth.md from a site that answers every path with the same page.
7. x402 discovery: the `/.well-known/x402` URL the platform's auth.md or llms.txt names on its own site, else `https://<domain>/.well-known/x402`.

At most 16 requests per platform, redirect hops included; most use 7 to 9. https on port 443 only; every address a name resolves to must be public, checked again on each connection and redirect; at most 4 redirects; 1 MB and 8 seconds per response. The index runs 4 platforms at a time with 300 ms between requests to the same platform.

## The verdicts

Pass, partly or fail, per check; the README has the rule for each. Prose verdicts (a partly that rests on auth.md or llms.txt text) quote the sentence that matched. Rows are ranked by checks passed; ties share a rank and are listed by name; partly never breaks a tie. "Who acts at sign-up" comes from the identity types in `agent_auth` metadata, or, marked as such, from prose.

## Review and corrections

Before the first publication a separate fact-check re-fetched every URL (all matched what the run saw) and read every partial verdict and the top rows. What it found was fixed in the checker, and the run was repeated. Two cells the checker still reads wrong were corrected by hand: xAI's spend limits (its llms.txt matches on a consumer X subscription page, not the API) and Stripe's payment handoff (its llms.txt describes payment products Stripe sells, not a way for an agent to pay Stripe). Each is marked "Corrected after review" in its reason, with the checker's own verdict beside it, and each applies only while the checker still sees the same match on the same URL: if the page changes, the checker's verdict stands and the weekly diff lists the correction as stale.

As an outside check, on the 37 platforms that Cloudflare's isitagentready.com also scanned (October 27 and 28, 2026), the auth.md pass and fail verdicts agreed on 36. NanoGPT is the difference: Cloudflare passes it, this check says partly, because its auth.md documents OAuth and device login rather than a registration endpoint. Protected resource metadata agreed on 32; the 5 differences are platforms whose metadata sits on the API host or behind the API's `401`, which this checker follows (RFC 9728 section 5) and a scan of the site root does not.

## Prized's part

Prized wrote this checker, picked the platforms, and wrote auth-md-spend-limits, a draft extension that the spend limits and payment handoff checks accept alongside the IETF AAuth budgets draft, x402 and the `Payment` HTTP authentication scheme. Prized is in the list, scored by the same code, and sorted with everyone else. No platform, Prized included, passed either check through the Prized draft's fields. The `billing_url` and `billing_required` prose patterns were added to the payment check after reading Prized's and Boat's auth.md during the review; they apply to every platform.

## Limits

- A check sees what a platform publishes, not what its sign-up does after a POST. A platform that lets agents in through a CLI or an SDK it never describes in a public file scores lower here than it would in use.
- One API URL per platform. A platform whose other endpoints answer differently is scored on the one listed.
- Bot walls count against a platform: if a plain client gets a challenge page at `/auth.md`, an agent does too.
- A platform that can't be reached in a run (every request failed twice) keeps its last row, marked as not checked with the date of the row kept, so a bad network day never reads as a regression.
- Results change when platforms change their files. Each run is dated, and the weekly diff says what moved.
