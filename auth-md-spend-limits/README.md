# Spend limits and billing for auth.md

Draft extension, version 0.1, 2026-10-28. MIT license, the same as auth.md.

Editor: Hudson Griffith (Prized). Comments and co-editors welcome: open an issue in this repository with `auth-md-spend-limits` in the title, or comment on the proposal in workos/auth.md.

This extends [auth.md](https://github.com/workos/auth.md) (AUTH.md v0.6.0 on `main`, and the v0.7.0 draft in workos/auth.md#28). It only adds members to auth.md's documents and responses. It changes no existing field, endpoint or step.

## Why

An agent that signs its person up for a paid service through auth.md ends up with a credential and a set of scopes. auth.md does not say what that credential may spend, what happens when the free allowance runs out, or how the person raises the limit or stops it.

Services that let agents in already answer these questions, each with its own fields. Porkbun holds every API key on an account to a monthly spend limit ($100 unless the account holder sets one) that only the holder can raise, on the website. OpenRouter keys carry credit limits that an agent can read back, and its 402 names which limit was hit. NanoGPT lets the app that asks for a key set a USD `limit` on it per day, week or month. Financial Datasets answers 402 with a `checkout_link_endpoint` until the person pays. An agent has to learn each service separately (the table under Related work has the sources).

This extension gives those answers one shape:

1. Before registering, the agent reads the service's free allowance, default spend cap and price list from discovery, and shows them to the person at auth.md's consent step (Step 3).
2. Every agent registration carries a hard spend cap, reported in a `limits` object.
3. A request refused for money carries a `billing` object with the reason, the amount needed and a link for the person.
4. Only the account owner, signed in at the service, raises or removes a cap.

The service declares every member. The one value an agent may send, a requested cap, can only lower the default.

Out of scope: payment instruments an agent holds (x402, the HTTP Payment authentication scheme, Stripe Shared Payment Tokens, AP2 mandates). They can fund a capped registration later. Version 0.1 covers a person paying in a browser, which every paid service already supports.

The key words MUST, SHOULD and MAY are used as described in RFC 2119 and RFC 8174.

Money is always a W3C Payment Request amount: `{"currency": "USD", "value": "30.00"}`, an ISO 4217 code and a decimal string. It is never a float. A value MAY carry more decimal places than the currency's minor unit, for metered prices.

## 1. Discovery

The service adds a `billing` member to the `agent_auth` block of its authorization server metadata (auth.md Step 1b):

```json
"agent_auth": {
  "skill": "https://service.example.com/auth.md",
  "identity_endpoint": "https://auth.service.example.com/agent/identity",
  "claim_endpoint": "https://auth.service.example.com/agent/identity/claim",
  "identity_types_supported": ["service_auth"],
  "billing": {
    "version": "0.1",
    "pricing_uri": "https://service.example.com/pricing.md",
    "trial": {"credit": {"currency": "USD", "value": "30.00"}, "card_required": false},
    "default_spend_cap": {"amount": {"currency": "USD", "value": "30.00"}, "period": "total"},
    "spend_cap_periods_supported": ["total", "day"],
    "at_cap": "pause",
    "limits_endpoint": "https://api.service.example.com/agent/limits",
    "manage_uri": "https://service.example.com/settings/agents"
  }
}
```

| Member | | Meaning |
|---|---|---|
| `version` | required | `"0.1"` |
| `pricing_uri` | required | A public price list in Markdown or JSON. No sign-in. |
| `default_spend_cap` | required | The cap a new registration gets unless the person sets a different one. `period` is `total` (the registration's whole life), `day` (UTC day) or `month` (UTC calendar month). |
| `limits_endpoint` | required | Returns a registration's current `limits` (section 2). |
| `manage_uri` | required | The page where the account owner sees and changes agent registrations (section 5). |
| `trial` | optional | The free allowance a registration gets once claimed (for `anonymous`, before the claim). Absent means no free allowance. |
| `spend_cap_periods_supported` | optional | The periods a person may choose. Defaults to the period of `default_spend_cap`. |
| `at_cap` | optional | What happens to work still running when a cap is reached. `pause`: it stops and keeps its state, and can resume. `stop`: it ends and its data is kept. `refuse`: the service has no long-running work and only refuses new requests. Defaults to `refuse`. |

The service's `/auth.md` SHOULD state the same facts in prose, so an agent that reads only the Markdown gets them too.

## 2. Limits on a registration

A registration's limits are reported in a `limits` object:

```json
"limits": {
  "spend": {
    "cap": {"currency": "USD", "value": "30.00"},
    "spent": {"currency": "USD", "value": "4.12"},
    "period": "total",
    "resets_at": null
  }
}
```

- `cap` is null only when the account owner has removed the cap (section 5).
- `spent` is what the registration has been billed in the current period, including work it started that is still running, as of the response.
- `resets_at` is when the next `day` or `month` period starts. It is null for `total`.
- `scope` is optional. `registration`, the default, means this registration has a cap of its own. `account` means one cap is shared by every API credential on the account, as some services do today. Agents treat both the same way.

A registration can have more than one cap, such as $30 in total and $5 a day. `spend` is then the cap with the least left, the one that would refuse first, and an optional `spend_caps` array lists every cap in the same shape. An agent that reads only `spend` still stops in time.

The cap belongs to the registration, and every credential minted from it shares that cap: identity assertions, access tokens and API keys. It is separate from the account's balance, so a person with $500 of balance can hold an agent to $20.

`limits` appears in four places:

1. The registration response (`POST /agent/identity`), as the limits that will apply once the registration is claimed. For `anonymous`, they apply at once.
2. The claim completion response (`POST /agent/identity/claim/complete`), next to `identity`.
3. `GET limits_endpoint`, with any credential from the registration as a bearer token, answers `{"limits": {...}}`. Without a valid credential it answers 401.
4. Every refusal in section 4 that involves the cap.

An agent MAY send a lower cap when it registers:

```json
{
  "type": "service_auth",
  "login_hint": "maya@example.com",
  "requested_spend_cap": {"amount": {"currency": "USD", "value": "5.00"}, "period": "total"}
}
```

When the period of `requested_spend_cap` is supported, the service MUST add it to the registration as one more cap, on top of the default, so the registration is held to both. Otherwise it MUST ignore it. Adding a cap can only lower what the registration may spend.

Later versions MAY add other members to `limits`, such as rate or concurrency limits. Agents MUST ignore members they don't recognize.

## 3. Enforcement

The rule: the person is never billed more than `cap` in a period for what the registration did. The one allowed exception, still open, is storage kept by paused work (rule 4).

A service:

1. MUST attribute everything billable that a registration starts to that registration, for the thing's whole life, including after its credential is revoked or expires.
2. MUST refuse a request that would start billable work the remaining cap can't cover, before creating anything, with a refusal as in section 4. How far ahead it estimates, whether one call or ten minutes of a machine, is the service's choice, and it SHOULD document it.
3. MUST stop running work from accruing before `spent` passes the cap, in the way `at_cap` declares. Reaching a cap MUST NOT delete the person's data or resources.
4. MUST NOT bill the person for usage past the cap caused by meter lag, outages or rounding. The service absorbs it. Storage that paused work keeps using SHOULD count against the cap like any other usage. If the service bills it past the cap, it MUST say so at `pricing_uri` (open question 6).
5. SHOULD check a resume of paused work against the cap of the registration that started the work, whoever asks for the resume. A capped registration MUST NOT be able to start or resume work that counts against no cap, such as a machine the person made. Refusing that request, as a permission error without `billing`, is enough.
6. MUST NOT raise or lift a cap because money arrived. A payment, plan or top-up raises the balance, not the cap. Only the owner raises a cap (section 5).
7. SHOULD say, for `day` and `month` caps, whether paused work resumes on its own at `resets_at`.

## 4. Refusals for money

When a request is refused because of the cap, an empty balance or a missing plan, the response body carries a `billing` object, and also `limits` when the cap is the reason:

```http
HTTP/1.1 402 Payment Required
Content-Type: application/json
Cache-Control: no-store

{
  "error": "spend_cap_reached",
  "error_description": "This agent has spent $29.80 of its $30 cap, and starting this needs $0.52 more.",
  "billing": {
    "reason": "spend_cap_reached",
    "url": "https://service.example.com/settings/agents?registration=reg_123",
    "needed": {"currency": "USD", "value": "0.52"},
    "pricing_uri": "https://service.example.com/pricing.md"
  },
  "limits": {
    "spend": {
      "cap": {"currency": "USD", "value": "30.00"},
      "spent": {"currency": "USD", "value": "29.80"},
      "period": "total",
      "resets_at": null
    }
  }
}
```

| `billing.reason` | Meaning | Where `billing.url` goes |
|---|---|---|
| `spend_cap_reached` | The registration's cap can't cover the request. | Where the owner raises the cap. |
| `credit_exhausted` | The account's balance, free or paid, is spent. | Where the owner pays. |
| `plan_required` | The request needs a plan or paid tier the account doesn't have. | Where the owner picks a plan. |
| `claim_required` | An `anonymous` registration has used its pre-claim allowance. | No URL needed: the agent runs auth.md's claim ceremony (Step 4). |

- `url` is required except for `claim_required`. `needed`, the amount that would let this exact request through, is optional. `pricing_uri` is optional.
- `error` and its human-readable description follow the service's existing error format. The example uses auth.md's (`error`, `error_description`).
- New APIs SHOULD answer 402, for a cap as well as for a balance: in both cases only a person can unblock the request, and common HTTP clients don't retry a 402 on their own. A service with existing clients MAY keep its current status code and `error` value and add `billing` next to them. Agents recognize a refusal for money by the `billing` member, not by the status code.
- A 402 MAY also carry a machine-payment challenge, such as x402 or the HTTP Payment authentication scheme. An agent without a payment instrument uses `billing.url`.

## 5. What the person sees and controls

1. Before the claim, the person sees the cap. A service with a claim page (auth.md's `verification_uri`) MUST show the cap there, and SHOULD let the person change it on that page before they confirm. A service that emails the code instead MUST state the cap in that email, next to the scopes and expiry.
2. Only the account owner, signed in at the service, raises or removes a cap, at `manage_uri` or `billing.url`. No credential from the registration can change its own cap, and this extension defines no API to do it. A pay-only link, such as a hosted checkout, MAY take payment without sign-in, but MUST NOT change a cap.
3. `manage_uri` lists agent registrations apart from keys the person made. Each entry shows the agent's self-reported name (marked unverified), when it was created and last used, its scopes, cap, spend in the current period, a way to change the cap, and Revoke.
4. The service SHOULD email the owner once when a registration reaches its cap, with the link.
5. Revoking stops new spend within 60 seconds. It doesn't delete what the registration made, and the cap keeps applying to that work.

## 6. What the agent does

1. Before registering, read `agent_auth.billing`. At auth.md's Step 3 consent, tell the person the free allowance, the default cap and where the prices are. For example: "Service gives $30 of free credit with no card. I can spend up to $30 there unless you raise it." If the person names a lower limit, send it as `requested_spend_cap`.
2. Read prices from `pricing_uri`. Never quote a price, credit or cap from memory.
3. Before costly work, call `GET limits_endpoint`.
4. When a response carries `billing`, stop that action. Tell the person the reason and the amount needed, and give them `billing.url` exactly as received. Do not retry in a loop. Do not enter payment details, and do not open `billing.url` in a browser you control. Do not register again, or use another account, to get around a cap or a spent free allowance.
5. When the person says they're done, call `GET limits_endpoint`, then retry once.

## 7. Security considerations

- A cap bounds what a phished, leaked or misbehaving credential can cost the person. That bound is what lets a service offer agents a free allowance at all.
- `requested_spend_cap` is untrusted input, and it can only lower a cap. The service sets every other member.
- `billing.url` is shown to a person, so it is a phishing surface. Services SHOULD keep it on their own origin. Agents MUST show it whole, never shortened or rewritten.
- `limits_endpoint` and the refusals reveal spend only to a holder of the registration's credential.
- Free-allowance abuse is mostly out of scope (workos/auth.md#13 covers metering anonymous registrations). A cap plus one free allowance per verified inbox is the baseline the reference implementation uses.

## 8. Conformance checklist

How to check: P means a public GET is enough. C needs a credential from a test registration. T needs a test account driven to its cap.

| ID | Service requirement | Check |
|---|---|---|
| S1 | `agent_auth.billing` is present, with `version`, `pricing_uri`, `default_spend_cap`, `limits_endpoint` and `manage_uri`. | P |
| S2 | `pricing_uri` answers 200 with Markdown or JSON, without sign-in. | P |
| S3 | `limits_endpoint` answers 401 without a credential. | P |
| S4 | The registration and claim completion responses carry `limits.spend`. | C |
| S5 | `GET limits_endpoint` returns `limits` for the registration's credential, and `spent` grows with use. | C |
| S6 | A new registration's cap is no higher than `default_spend_cap`. A `requested_spend_cap` with a supported period is added as a further cap, and a higher one changes nothing. | C |
| S7 | Past the cap, a start is refused with `billing.reason` `spend_cap_reached`, a `url` and `needed`, and nothing is created. | T |
| S8 | Running work stops accruing at the cap, as `at_cap` says, and nothing is deleted. | T |
| S9 | What the person is billed for the registration never passes the cap in a period, apart from paused storage the service documents at `pricing_uri`. | T |
| S10 | Refusals for an empty balance or a missing plan carry `billing` (`credit_exhausted`, `plan_required`). | T |
| S11 | A payment doesn't raise the cap. Only the signed-in owner can, and no credential from the registration can. | T |
| S12 | The person sees the cap before the claim completes, on the claim page or in the code email. | T |
| S13 | `manage_uri` lists the registration with its cap, spend, a way to change the cap, and Revoke. | T |
| S14 | A capped registration can't start or resume work that counts against no cap. | T |

| ID | Agent requirement |
|---|---|
| A1 | Shows the free allowance and default cap at consent, and sends a lower limit when the person names one. |
| A2 | Reads prices from `pricing_uri`. |
| A3 | When a response carries `billing`: stops, shows the URL as given, doesn't loop, doesn't enter payment details, doesn't register again. |
| A4 | Reads `limits_endpoint` again before retrying. |

S1 to S3 need only GET requests, so a public scanner can check them alongside auth.md's own discovery checks.

## Implementations

- Prized ([prized.dev](https://prized.dev/?via=auth-md-spend-limits)), the reference implementation, in progress. Live today: public `pricing_uri` (https://prized.dev/pricing.md), auth.md discovery and `service_auth` registration. Built and in review: per-key caps, the 402 `spend_cap_reached` refusal above, and pausing a key's work at its cap. Next: `agent_auth.billing`, `limits` at claim completion, `limits_endpoint`, and the owner's cap controls. Open: the disk of a paused machine keeps billing at its sleep rate past a cap today (open question 6).

## Related work

What services ship today, checked 2026-10-28:

| Service | What it ships | Section here | Source |
|---|---|---|---|
| Porkbun (domains) | One monthly spend limit for every API key and agent on an account, $100 by default. Only the account holder raises it, on the website. Orders over it are refused before any charge with `MONTHLY_SPEND_LIMIT_EXCEEDED`. The enforced limit, spend and remaining amount are readable over the API. | 2, 3, 5 | https://porkbun.com/llms/guides/spend-limits |
| Stripe Projects, with Cloudflare as a provider | When an agent provisions a paid service, "Stripe then sets a default limit of $100.00 USD/month as the maximum the agent can spend on any one provider." | 1, 2 | https://blog.cloudflare.com/agents-stripe-projects/ |
| OpenRouter (LLM API) | Per-key credit limits, readable at `GET /api/v1/key` (`limit`, `limit_reset`, `limit_remaining`), and budgets with a `scope` and `resets_at`. A 402 names its `limit_source` (the key's limit or the balance). Spent budgets answer 403 with `remedy_hint`. | 2, 4 | https://openrouter.ai/docs/api_reference/limits.md, https://openrouter.ai/docs/api_reference/errors-and-debugging.md |
| NanoGPT (LLM API) | The app that asks for a key through its key handoff may set a USD `limit` on it (`daily`, `weekly` or `monthly`) and `expires_at`. | 2 | https://nano-gpt.com/auth.md |
| Financial Datasets (data API) | Agent sign-up returns a key. Data calls answer 402 with `checkout_link_endpoint` until the account is funded, and again when the balance runs out. | 4 | https://docs.financialdatasets.ai/agents.md |
| Orthogonal (API marketplace) | `starterCredit` of $1 on agent sign-up, a pricing endpoint, and an emailed payment link the owner pays. | 1, 4 | https://orthogonal.com/agent-signup.md |
| Context.dev (web data API) | auth.md `service_auth` with a sign-up offer of 1,000 extra credits for new customers. | 1 | https://www.context.dev/auth.md |
| Boat (sandboxes) | Its auth.md sends agents to `GET /api/v1/limits` for the plans, what the trial includes and what it caps. Pre-claim tokens may call only `/me` and `/limits`, and everything else answers `claim_required`. | 1, 4 | https://boat.dev/auth.md |

Specifications:

- AAuth Budgets (draft-hardt-aauth-budgets-00, 2026-09-28) carries a budget in AAuth tokens, in a unit the resource declares, with `budget_units` and a `usage_endpoint` in resource metadata. Its rule is the one in section 3: "A resource MUST NOT let metered consumption exceed the granted amount." An exhausted budget is a 401 step-up to the person's server, and 402 is left to payment. It defines no prices. auth.md registrations don't carry AAuth tokens, so this extension puts the cap on the registration instead. https://datatracker.ietf.org/doc/draft-hardt-aauth-budgets/
- The Delegation HTTP authentication scheme (draft-mcgraw-httpapi-agent-budget-04) has the requester present signed, bounded authority to spend, and answers with 401 and 403, not 402. https://datatracker.ietf.org/doc/draft-mcgraw-httpapi-agent-budget/
- Payment instruments bound one instrument: the x402 `upto` scheme (a maximum per payment), Stripe Shared Payment Tokens (`usage_limits.max_amount`) and the Agentic Commerce Protocol's delegated payment `allowance`. This extension bounds the registration, whatever pays for it.

Threads in workos/auth.md:

- workos/auth.md#13 asks how services meter the free value they give anonymous registrations. The maintainer's concern there was that agent-supplied fields can be spoofed. This extension relies on no agent-supplied value except a cap that can only go down.
- workos/auth.md#24, point C: "A scope has no ceiling. ... An agent registered for `api.write` on a payments API is authorized for a $10 transfer and a $10,000,000 one identically." A spend cap is that ceiling for money.
- workos/auth.md#31 proposes showing terms at the same consent step. The two compose.

## Open questions

1. Should this be an optional section of AUTH.md, or an extension that the README links to?
2. Should `billing` live in `agent_auth` (where agents already look before registering) or in the protected resource metadata (prices belong to the resource)?
3. Should every service that offers paid usage to agent registrations be required to declare `default_spend_cap`?
4. Are decimal strings enough for sub-cent metered prices, or does `pricing_uri` need a JSON form with unit prices?
5. Should `spend_cap_reached` be a 402 or a 403? Both IETF drafts above treat a spent budget as an authorization limit (401 or 403) and keep 402 for payment, and OpenRouter answers spent budgets with 403. This draft prefers 402 for every refusal only a person can clear, and agents key on `billing` either way.
6. Should storage that paused work keeps using count against the cap, so the person is never billed past it, or may a service keep billing it past the cap if `pricing_uri` says so? Deleting the data at the cap is not an option (section 3, rule 3).
