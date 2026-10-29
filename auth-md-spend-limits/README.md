# Spend limits and billing for auth.md

Draft extension, version 0.1, 2026-10-29. MIT license, the same as auth.md.

Editor: Hudson Griffith (Prized). Comments and co-editors welcome: open an issue in this repository with `auth-md-spend-limits` in the title, or comment on the proposal in workos/auth.md.

This extends [auth.md](https://github.com/workos/auth.md) (AUTH.md v0.6.0 on `main`, and the v0.7.0 draft in workos/auth.md#28). It only adds members to auth.md's documents and responses. It changes no existing field, endpoint or step.

## Why

An agent that signs its person up for a paid service through auth.md ends up with a credential and a set of scopes. auth.md does not say what that credential may spend, what happens when the free allowance runs out, or how the person raises the limit or stops it.

Services that let agents in already answer these questions, each with its own fields. Porkbun holds every API key on an account to a monthly spend limit ($100 unless the account holder sets one) that only the holder can raise, on the website. OpenRouter keys carry credit limits that an agent can read back, and its 402 names which limit was hit. NanoGPT lets the app that asks for a key set a USD `limit` on it per day, week or month. Financial Datasets answers 402 with a `checkout_link_endpoint` until the person pays. An agent has to learn each service separately (the table under Related work has the sources).

This extension gives those answers one shape:

1. Before registering, the agent reads the service's free allowance, default spend cap and price list from discovery, and tells the person.
2. Every agent registration carries a hard spend cap, reported in a `limits` object.
3. A request refused for money carries a `billing` object with the reason, the amount needed and a link for the person.
4. Only the account owner, signed in at the service, raises or removes a cap.

The service declares every member. The one value an agent may send, a requested cap, can only lower what the registration may spend.

A service can adopt it in two parts. Part 1, the payment handoff, is points 1 and 3 without a cap: discovery and the `billing` object on refusals for money. Part 2, spend limits, adds the cap. [Adopt it in two parts](#adopt-it-in-two-parts) has what each part publishes and how the public [agent sign-up check](https://prized.dev/agent-signup-check?via=auth-md-spend-limits) reads it.

Out of scope: payment instruments an agent holds (x402, the HTTP Payment authentication scheme, Stripe Shared Payment Tokens, AP2 mandates). They can fund a capped registration later. Version 0.1 covers a person paying in a browser, which every paid service already supports. Payments made with no registration at all, such as a keyless x402 call, are outside any cap here.

The key words MUST, SHOULD and MAY are used as described in RFC 2119 and RFC 8174.

Money is always a W3C Payment Request amount: `{"currency": "USD", "value": "20.00"}`, an ISO 4217 code and a non-negative decimal string. It is never a float. A value MAY carry more decimal places than the currency's minor unit, for metered prices.

## Adopt it in two parts

Both parts add members to the authorization server metadata your `agent_auth` block is already in (auth.md Step 1b, RFC 8414). The public agent sign-up check reads the members each part publishes. It sends GET requests only and never signs in, so it can't see your refusals or your caps. Publishing a member is your statement that the behaviour behind it is live.

### Part 1: payment handoff

About a day of work for a service that already refuses requests for money. Publish:

```json
"agent_auth": {
  "identity_endpoint": "https://auth.service.example.com/agent/identity",
  "billing": {
    "version": "0.1",
    "pricing_uri": "https://service.example.com/pricing.md"
  }
}
```

Change: every refusal for money (an empty balance, a missing plan) gets a top-level `billing` object beside the error body you send today (section 4). Keep your status code and your error fields. A service whose errors are an `errors` array answers:

```json
{
  "errors": [{"code": "insufficient_balance", "title": "Insufficient balance", "detail": "Add funds to continue."}],
  "billing": {
    "reason": "credit_exhausted",
    "url": "https://service.example.com/billing",
    "needed": {"currency": "USD", "value": "0.52"}
  }
}
```

If owners can already set spend limits on the account or its keys, also publish `owner_limits_uri`, the page where they do, and answer a refusal under such a limit with `billing.reason` `spend_cap_reached` and a `url` to that page (section 4). This is not part 2: agent registrations still get no cap unless the owner sets one.

The check passes Payment handoff when `agent_auth.billing.pricing_uri` is an https URL. It also passes on x402 (a 402 with a payment requirement, or a `/.well-known/x402` document) and on a `Payment` scheme challenge, which this extension doesn't define. The first rule that matches gives the reason it prints. With `owner_limits_uri` and no `default_spend_cap`, Spend limits passes partly.

### Part 2: spend limits

About a day of mapping if you already enforce a cap on each agent registration or key that covers everything it can bill. Otherwise the cap itself is the work. Publish three more members, once the cap is enforced:

```json
"agent_auth": {
  "identity_endpoint": "https://auth.service.example.com/agent/identity",
  "billing": {
    "version": "0.1",
    "pricing_uri": "https://service.example.com/pricing.md",
    "default_spend_cap": {"amount": {"currency": "USD", "value": "20.00"}, "period": "month"},
    "limits_endpoint": "https://api.service.example.com/agent/limits",
    "manage_uri": "https://service.example.com/settings/agents"
  }
}
```

Change:

1. Every new agent registration gets `default_spend_cap`, unless the owner sets a lower cap when confirming the claim (sections 2 and 5). Every key the registration gets or mints shares that one cap.
2. Refuse a registration's billable requests once what is left of its cap can't cover them, with `billing.reason` `spend_cap_reached` and a top-level `limits` (section 4).
3. Never bill the person past the cap. If your meter lags, credit back what the registration's usage cost above its cap as soon as the meter settles (section 3, rule 4).
4. The cap covers everything the registration can bill. If your limits count only some products, give agent registrations only the scopes those products need (section 3, rule 5).
5. `GET limits_endpoint` with the registration's credential answers `{"limits": {...}}`, and 401 without one (section 2).
6. `manage_uri` lists agent registrations with their caps, spend this period, a way to change the caps, and Revoke. Only the signed-in owner changes a cap (section 5), so an agent registration's credentials can't call any API that changes limits.

A service that already enforces per-key spend limits maps its fields onto these. One limit shared by every key on an account is `"applies_to": "account"` (section 2). A service with no cap today can publish part 1 now and part 2 when the cap is enforced. Agents tell people the cap exists, so don't publish one you don't enforce.

The check passes Spend limits when `default_spend_cap` has the shape above (a currency code, a decimal string and a known `period`), and `version`, an https `pricing_uri`, `limits_endpoint` and `manage_uri` come with it. A `default_spend_cap` without all of that, or only `owner_limits_uri`, passes partly. It can't see enforcement. Section 8 has the full list and how to test each.

### Test it

```sh
node agent-signup-check.mjs service.example.com --api https://api.service.example.com/v1/me
#   PASS  Spend limits     agent_auth.billing declares a default spend cap
#   PASS  Payment handoff  agent_auth.billing declares a pricing_uri for the 402 billing handoff

curl -s -o /dev/null -w '%{http_code}\n' https://api.service.example.com/agent/limits     # 401
curl -s -H "Authorization: Bearer $TOKEN" https://api.service.example.com/agent/limits    # {"limits": {...}}
```

The checker is [agent-signup-check.mjs](https://prized.dev/agent-signup-check.mjs), one Node file with no dependencies. `$TOKEN` is the credential of a test agent registration.

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
    "trial": {"credit": {"currency": "USD", "value": "25.00"}, "card_required": false},
    "default_spend_cap": {"amount": {"currency": "USD", "value": "20.00"}, "period": "total"},
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
| `default_spend_cap` | part 2 | The cap a new registration gets unless the person sets a lower one. `period` is `total` (the registration's whole life), `day` (UTC day) or `month` (UTC calendar month). |
| `limits_endpoint` | part 2 | Returns a registration's current `limits` (section 2). |
| `manage_uri` | part 2 | The page where the account owner sees and changes agent registrations (section 5). |
| `default_spend_caps` | optional, part 2 | Every default cap, in the same shape, when a new registration gets more than one, such as $5 a day and $20 a month. `default_spend_cap` is then the one that would refuse first. |
| `owner_limits_uri` | optional | For a service without part 2: the page where the owner sets spend limits on the account or its keys. That page, or `pricing_uri`, says which products those limits cover. |
| `trial` | optional | The free allowance, given at most once per account. A registration on an account that already used it gets none. An allowance an `anonymous` registration uses before its claim is part of the trial, not in addition to it. Absent means no free allowance. |
| `spend_cap_periods_supported` | optional | The periods a person may choose. Defaults to the period of `default_spend_cap`. |
| `at_cap` | optional | What happens to work still running when a cap is reached. `pause`: it stops and keeps its state, and can resume. `stop`: it ends and its data is kept. `refuse`: the service only refuses new requests, and requests already running finish. Defaults to `refuse`. |
| `url_origins` | optional | Origins other than the service's own that `billing.url` may point to, such as a hosted checkout. |

A service that publishes `billing` MUST answer every refusal for money as in section 4. The three part 2 members come together: a service publishes all of them or none. Publishing them means every agent registration has a cap, and the service follows sections 2, 3 and 5. A `billing` without `default_spend_cap` tells agents the service sets no spend cap on agent registrations, though with `owner_limits_uri` the owner may set one.

The service's `/auth.md` SHOULD state the same facts in prose, so an agent that reads only the Markdown gets them too.

## 2. Limits on a registration

Sections 2, 3 and 5 apply to a service that publishes the part 2 members. A registration's limits are reported in a `limits` object:

```json
"limits": {
  "spend": {
    "cap": {"currency": "USD", "value": "20.00"},
    "spent": {"currency": "USD", "value": "4.12"},
    "period": "total",
    "resets_at": null
  }
}
```

- `cap` is null only when the account owner has removed the cap (section 5).
- `spent` is the list-price value of all usage counted against the cap in the current period: the registration's own, and that of any registration it shares a counter with (below), however it is paid for (free credit, balance, plan or invoice), including work it started that is still running, as of the response or of `as_of` when present. It is null only when the service can't read it right now. A trial does not add to the cap: with $25 of free credit and a $20 cap, the agent can use $20.
- `resets_at` is when the next `day` or `month` period starts. It is null for `total`.
- `as_of` is optional: when `spent` was last measured, for a meter that lags.
- `applies_to` is optional. `registration`, the default, means the cap counts agent registrations: this one and any it shares a counter with. `account` means the cap counts every credential on the account, the owner's own keys included, as some services do today. A service uses `account` only when its credentials already work that way. Agents treat both the same way.

A registration can have more than one cap, such as $20 in total and $5 a day. `spend` is then the cap with the least left, the one that would refuse first, and `spend_caps` lists every cap in the same shape. An agent that reads only `spend` still stops in time.

Unless `applies_to` is `account`, the cap belongs to the registration. Every credential issued for it shares that cap: access tokens, refresh tokens and any service-specific API key. It is separate from the account's balance, so a person with $500 of balance can hold an agent to $20.

Caps survive re-registration. auth.md sends an agent back to register when its assertion expires or a token exchange fails. A registration that replaces an earlier one on the same account MUST inherit the earlier registration's caps and `spent`. That covers a v0.7 refresh, an `identity_assertion` for the same (`iss`, `sub`, `aud`), and a claim while another agent registration on the account is still live or inside its period. While both are live they share one counter, so together they spend no more than the cap. In effect, agent registrations on one account that overlap in time are held to one set of caps, so a new agent can't start from zero. The claim page may lower inherited caps. Only the owner, at `manage_uri`, raises them.

`limits` appears in four places:

1. The registration response (`POST /agent/identity`). There `spend_caps` is required, and lists every cap that will apply. They apply once the registration is claimed. For `anonymous`, and for an `identity_assertion` that needs no confirmation, they apply at once. For `service_auth`, `login_hint` is not yet verified, so this response MUST NOT depend on whether that address has an account: it lists the default cap and any requested cap, with `spent` at zero. Caps carried over from an earlier registration (above) first appear at claim completion.
2. Claim completion: in v0.7, the `/agent/identity/claim/complete` response, next to `identity`. In v0.6, the token response of the claim grant (Step 4c), as an additional parameter (RFC 6749, section 5.1). A service whose claim works differently, such as an emailed link and then a call that mints an API key, puts it on the response that first gives the agent its API credential.
3. `GET limits_endpoint`, with the bearer credential the agent uses for API calls (auth.md Step 6), answers `{"limits": {...}}`. Claim tokens and identity assertions are not accepted there. Without a valid credential it answers 401.
4. Every refusal in section 4 that involves the cap.

An agent MAY send a lower cap when it registers, and only when `agent_auth.billing.default_spend_cap` is present:

```json
{
  "type": "service_auth",
  "login_hint": "maya@example.com",
  "requested_spend_cap": {"amount": {"currency": "USD", "value": "5.00"}, "period": "total"}
}
```

When the period of `requested_spend_cap` is supported and its currency is that of `default_spend_cap`, the service MUST add it to the registration as one more cap, on top of the default, so the registration is held to both. A service that keeps one cap per period MAY instead lower that period's cap to it. Otherwise it MUST ignore it. Adding a cap can only lower what the registration may spend. An agent that sent one MUST check that `spend_caps` in the registration response, and again in the claim completion's `limits`, holds a cap no higher than it, with the same period, and MUST tell the person if it doesn't.

Later versions MAY add other members to `limits`, such as rate or concurrency limits. Agents MUST ignore members they don't recognize.

## 3. Enforcement

The rule: the person is never billed more than `cap` in a period for what the registration did. The one allowed exception, still open, is storage kept by paused work (rule 4). Rules 2 and 3 are measured against the service's own meter. What a lagging meter lets through is absorbed under rule 4.

A service:

1. MUST attribute everything billable that a registration starts to that registration, for the thing's whole life, including after its credential is revoked or expires.
2. MUST refuse a request that would start billable work the remaining cap can't cover, before creating anything, with a refusal as in section 4. How far ahead it estimates, whether one call or ten minutes of compute, is the service's choice, and it SHOULD document it. It SHOULD hold each admitted request's estimate against the cap until the request settles, so parallel requests can't each pass against the same remainder. A service that doesn't hold estimates absorbs what they spend past the cap (rule 4).
3. MUST stop running work from accruing before `spent` passes the cap, in the way `at_cap` declares, except what `refuse` lets finish (rule 4). Reaching a cap MUST NOT delete the person's data or resources.
4. MUST NOT bill the person for usage past the cap caused by meter lag, outages, rounding or requests admitted before the cap was reached. The service absorbs it. The simplest way is to credit back what the registration's usage cost above its cap as soon as the meter settles. On a prepaid balance, an overrun that empties the balance SHOULD NOT stop the person's other work before that credit lands. Storage that paused work keeps using SHOULD count against the cap like any other usage. If the service bills it past the cap, it MUST say so at `pricing_uri` (open question 6).
5. SHOULD check a resume of paused work against the cap of the registration that started the work, whoever asks for the resume. A capped registration MUST NOT be able to cause billable usage that no cap counts, such as resuming work the person started themselves, or using a product the service's limits don't meter. Refusing that request, as a permission error without `billing`, is enough. A service whose limits count only some products can instead give agent registrations only the scopes those products need.
6. MUST NOT raise or lift a cap because money arrived. A payment, plan or top-up raises the balance, not the cap. Only the owner raises a cap (section 5).
7. SHOULD say, for `day` and `month` caps, whether paused work resumes on its own at `resets_at`.
8. MUST bill a registration's usage only to the account the person confirmed at the claim. Here the account is whatever holds the balance: a user, a team or an organization. A claim MUST NOT give the registration another account's balance, such as an organization the address joins through domain auto-join, SSO or a pending invite. That account's owner adds the registration there, and sets its cap.

## 4. Refusals for money

When the resource server of a service that publishes `agent_auth.billing` refuses a request because of the cap, an empty balance or a missing plan, it answers with a non-2xx status whose JSON body has a top-level `billing` object, and also a top-level `limits` when the cap is the reason:

```http
HTTP/1.1 402 Payment Required
Content-Type: application/json
Cache-Control: no-store

{
  "error": "spend_cap_reached",
  "error_description": "This agent has used $19.80 of its $20 cap, and this request needs $0.52 more.",
  "billing": {
    "reason": "spend_cap_reached",
    "url": "https://service.example.com/settings/agents?registration=reg_123",
    "needed": {"currency": "USD", "value": "0.52"},
    "pricing_uri": "https://service.example.com/pricing.md"
  },
  "limits": {
    "spend": {
      "cap": {"currency": "USD", "value": "20.00"},
      "spent": {"currency": "USD", "value": "19.80"},
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
| `claim_required` | An `anonymous` registration has used its pre-claim allowance. | No URL: the agent runs auth.md's claim ceremony (Step 4). |

- `url` is required except for `claim_required`. It MUST be https, on the same site as the `issuer` or the protected resource, or on an origin in `agent_auth.billing.url_origins`.
- `needed` is the amount that would let this exact request through. A service SHOULD send it when it can compute it. It is not a minimum purchase. `pricing_uri` is optional.
- A service SHOULD use the reasons above where they fit, and MAY use another `reason` when none does. It MAY add members to `billing`, such as the smallest purchase that clears the refusal. Agents ignore members they don't recognize, and treat an unrecognized `reason` as one only a person can clear, at `billing.url`.
- `error` and its human-readable description follow the service's existing error format. The example uses auth.md's (`error`, `error_description`). Part 1 above shows `billing` beside an `errors` array. A service whose errors follow JSON:API, which allows no other top-level members, MAY put `billing` and `limits` in the top-level `meta` object instead. If both places carry `billing`, the top-level one counts.
- A service without part 2 whose owners set spend limits themselves (`owner_limits_uri`) answers a refusal under such a limit with `spend_cap_reached`, a `url` where the owner changes the limit, and `limits` when it can compute them.
- New APIs SHOULD answer 402, for a cap as well as for a balance: in both cases only a person can unblock the request, and common HTTP clients don't retry a 402 on their own. A service with existing clients MAY keep its current status code and `error` value and add `billing` next to them, but a refusal for money MUST NOT use a 2xx, 401 or 429 status, which auth.md tells agents to re-register on or retry.
- A `credit_exhausted` or `plan_required` refusal MAY also carry a machine-payment challenge, such as x402 or the HTTP Payment authentication scheme. A `spend_cap_reached` or `claim_required` refusal MUST NOT, because paying never changes a cap.

## 5. What the person sees and controls

1. The person learns the cap before the registration can spend. A service with a claim confirmation page (auth.md's `verification_uri`) MUST show the cap there, and SHOULD let the person lower it on that page before they confirm. Raising a cap is for the owner at `manage_uri`, because whoever completes the claim may not be the owner: some agents read the inbox themselves. A service that confirms the claim some other way MUST show the cap wherever the person confirms. A registration with no claim step (`anonymous` before its claim, an `identity_assertion` that needs no confirmation) relies on the agent's consent step (section 6, rule 1).
2. Only the account owner, signed in at the service, raises or removes a cap, at `manage_uri` or `billing.url`. No credential from the registration can change its own cap, and this extension defines no API to do it. A pay-only link, such as a hosted checkout, MAY take payment without sign-in, but MUST NOT change a cap.
3. `manage_uri` lists agent registrations apart from credentials the person created themselves. Each entry shows any name the agent gave (marked unverified) and, for `identity_assertion`, the provider; when it was created and last used; its scopes, caps and spend in the current period; a way to change the caps; and Revoke.
4. The service SHOULD email the owner once when a registration reaches its cap, with the link.
5. Revoke MUST end the registration the way auth.md's registration-layer revocation does, and the agent coming back MUST need the owner's confirmation. New spend SHOULD stop within 60 seconds. Revoking doesn't delete what the registration made, and the cap keeps applying to that work.
6. When the owner first signs in to an account that an agent registration created, the service SHOULD list its agent registrations and ask the owner to keep or revoke each one, before the owner adds a payment method. Someone else may have registered with the owner's address.

## 6. What the agent does

1. Before registering, the agent MUST read `agent_auth.billing` when present. At auth.md's Step 3 consent it MUST tell the person the free allowance, the default cap and where the prices are, for example: "Service gives $25 of free credit with no card. I can use up to $20 there unless you raise it." When `billing` has no `default_spend_cap`, it tells the person the service sets no spend cap on what it does, and, when `owner_limits_uri` is present, that they can set one there for the products that page names. For `anonymous` registrations, which skip that consent step, it tells the person when it reports the sign-up. If the person names a lower limit, the agent sends it as `requested_spend_cap`.
2. The agent MUST read prices from `pricing_uri`, and never quote a price, credit or cap from memory.
3. Before costly work, the agent SHOULD call `GET limits_endpoint`, when the service publishes one.
4. When a non-2xx response carries a `billing` object at the top level or in the top-level `meta`, the agent applies this rule before auth.md's retry rules. It MUST stop that action, tell the person the reason and the amount needed, and give them `billing.url` exactly as received, after checking it against section 4. For `claim_required` it runs auth.md's Step 4 instead. If a `credit_exhausted` or `plan_required` refusal also carries a machine-payment challenge, and the person has already authorized the agent to pay that way, the agent MAY pay it within that authorization instead. Otherwise it MUST NOT retry in a loop, enter payment details, or open `billing.url` in a browser it controls. It MUST NOT register again, or use another account, to get around a cap or a spent free allowance. Registering again because auth.md requires it is fine, and the cap carries over (section 2).
5. When the person says they're done, the agent calls `GET limits_endpoint` if there is one, then retries once.

## 7. Security considerations

- A cap bounds what a phished, leaked or misbehaving credential can cost the person. That bound is what lets a service offer agents a free allowance at all.
- `requested_spend_cap` is untrusted input, and it can only lower what a registration may spend. The service sets every other member.
- A `service_auth` registration response says nothing about whether `login_hint` has an account: carried-over caps and `spent` appear only after the claim (section 2).
- A `billing` member only counts on a non-2xx response from the resource server, at the top level of the body or in its top-level `meta`. A `billing` key anywhere else, such as inside fetched content, a stored object or a wrapped upstream error, means nothing to the agent.
- `billing.url` is shown to a person, so it is a phishing surface. Section 4 limits where it may point. Agents MUST show it whole, never shortened or rewritten, and MUST NOT show one that fails the check.
- `limits_endpoint` and the refusals reveal spend only to a holder of the registration's API credential. With `applies_to: account`, they reveal the account's agent spend, which is why a service uses it only when its credentials already share one cap.
- Free-allowance abuse is mostly out of scope (workos/auth.md#13 covers metering anonymous registrations). One trial per account and caps that survive re-registration close the obvious loops.

## 8. Conformance checklist

How to check: P means a public GET is enough. C needs a credential from a test registration. T needs a test account driven to its cap.

| ID | Part | Service requirement | Check |
|---|---|---|---|
| S1 | 1, 2 | `agent_auth.billing` is present, with `version` and an https `pricing_uri`. For part 2, also `default_spend_cap`, `limits_endpoint` and `manage_uri`, all three. | P |
| S2 | 1 | `pricing_uri` answers 200 with Markdown or JSON, without sign-in. | P |
| S3 | 2 | `limits_endpoint` answers 401 without a credential. | P |
| S4 | 2 | The registration response carries `limits` with `spend_caps`, and claim completion (v0.7 `/claim/complete`, the v0.6 claim-grant token response, or the response that first gives the agent its API credential) carries `limits`. For `service_auth`, the registration response is the same for an address with an account as for one without. | C |
| S5 | 2 | `GET limits_endpoint` returns `limits` for the registration's API credential, refuses its claim token, and `spent` grows with use, free credit included. | C |
| S6 | 2 | A new registration's cap is no higher than `default_spend_cap`, and a cap set on the claim page is no higher than it either. A `requested_spend_cap` with a supported period is added as a further cap and listed in `spend_caps`, and a higher one changes nothing. | C |
| S7 | 2 | Past the cap, a start is refused with a non-2xx status (not 401 or 429), `billing.reason` `spend_cap_reached`, a `url` that passes section 4, and `needed` when the service can compute it, and nothing is created. | T |
| S8 | 2 | Running work stops accruing at the cap, as `at_cap` says, and nothing is deleted. | T |
| S9 | 2 | What the person is billed for the registration never passes the cap in a period, apart from paused storage the service documents at `pricing_uri`. | T |
| S10 | 1 | Refusals for an empty balance or a missing plan carry `billing` (`credit_exhausted`, `plan_required`). | T |
| S11 | 2 | A payment doesn't raise the cap. Only the signed-in owner can, and no credential from the registration can. | T |
| S12 | 2 | Where there is a claim confirmation, the person sees the cap there before it completes. | T |
| S13 | 2 | `manage_uri` lists the registration with its caps, spend, a way to change the caps, and Revoke. | T |
| S14 | 2 | A capped registration can't cause billable usage that no cap counts. | T |
| S15 | 2 | After Revoke, new spend stops, and the agent can't come back without the owner's confirmation. | T |
| S16 | 2 | A registration that replaces an expired one on the same account keeps its caps and `spent`. | T |
| S17 | 2 | A claim on an address whose domain has an organization at the service doesn't put the registration's spend on that organization's balance. | T |

| ID | Agent requirement |
|---|---|
| A1 | Shows the free allowance and default cap at consent (or that there is none, and `owner_limits_uri` when present), and sends a lower limit when the person names one. |
| A2 | Reads prices from `pricing_uri`. |
| A3 | On a non-2xx response with a top-level (or `meta`) `billing`: stops, checks the URL and shows it as given, doesn't loop, doesn't enter payment details (unless the refusal carries a machine-payment challenge the person already authorized), doesn't register again to get around the cap. |
| A4 | Reads `limits_endpoint` again before retrying. |
| A5 | After sending `requested_spend_cap`, checks it appears in `spend_caps`, and tells the person if it doesn't. |

S1 to S3 need only GET requests, so a public scanner can check them alongside auth.md's own discovery checks. The agent sign-up check tests part of S1: an https `pricing_uri` passes its Payment handoff check, and a `default_spend_cap` in the shape of section 1 with https `limits_endpoint` and `manage_uri` passes Spend limits, when `version` and an https `pricing_uri` come with it. It doesn't yet fetch S2 or S3. A4 and A5 apply to part 2 only.

## Implementations

- Prized ([prized.dev](https://prized.dev/?via=auth-md-spend-limits)), the reference implementation, in progress. Live today: public `pricing_uri` (https://prized.dev/pricing.md), auth.md discovery and `service_auth` registration. An agent-made account gets a workspace of its own and joins no other (section 3, rule 8). At first sign-in the owner reviews agent-made keys, with Revoke chosen, and can't pay until they answer (section 5, rule 6). Its claim ceremony differs from AUTH.md's: the service emails the person a 6-digit code, and the agent reads it back. On the agent sign-up check of 2026-10-28, Prized fails Spend limits and passes Payment handoff only partly, because it publishes no `agent_auth.billing` yet. Part 1 needs `billing` on its existing refusals for money (`insufficient_balance`, `paid_balance_required`, `tier_unavailable`). Built and in review for part 2: per-key total and daily caps, the 402 `spend_cap_reached` refusal above, pausing a key's work at its cap, and refusing a capped key's start of work it didn't make. Next: `agent_auth.billing`, `limits` at registration and claim, `limits_endpoint`, caps that carry over re-registration, and the owner's cap controls. Known gap: as built, the disk of a paused machine keeps billing at its storage rate past a cap (open question 6). Abuse baseline: a cap plus one free allowance per verified inbox.

## Related work

What services ship today, checked 2026-10-28 (Telnyx 2026-10-29):

| Service | What it ships | Section here | Source |
|---|---|---|---|
| Porkbun (domains) | One monthly spend limit for every API key and agent on an account, $100 by default. Only the account holder raises it, on the website. Orders over it are refused before any charge with `MONTHLY_SPEND_LIMIT_EXCEEDED`, and parallel orders are held against the limit until they finish. The enforced limit, spend and remaining amount are readable over the API. | 2, 3, 5 | https://porkbun.com/llms/guides/spend-limits |
| Telnyx (communications and inference) | Daily and monthly USD limits on inference spend, for the organization and for each API key within it, read and set at `/v2/spend_limits`. A blocked request answers 403 with code `10039`. Requests already running finish, and spend can pass the limit while the meter catches up, which is the overrun section 3, rule 4 has the service absorb. | 2, 3 | https://developers.telnyx.com/docs/inference/spending-limits |
| Stripe Projects, with Cloudflare as a provider | When an agent provisions a paid service, "Stripe then sets a default limit of $100.00 USD/month as the maximum the agent can spend on any one provider." It is set on the payment token, a payment-instrument bound, but it is the model for a default cap. | 1 | https://blog.cloudflare.com/agents-stripe-projects/ |
| OpenRouter (LLM API) | Per-key credit limits, readable at `GET /api/v1/key` (`limit`, `limit_reset`, `limit_remaining`), and budgets with a `scope` and `resets_at`. A 402 names its `limit_source` (the key's limit or the balance). Spent budgets answer 403 with `remedy_hint`. | 2, 4 | https://openrouter.ai/docs/api_reference/limits.md, https://openrouter.ai/docs/api_reference/errors-and-debugging.md |
| NanoGPT (LLM API) | The app that asks for a key through its key handoff may set a USD `limit` on it (`daily`, `weekly` or `monthly`) and `expires_at`. | 2 | https://nano-gpt.com/auth.md |
| Financial Datasets (data API) | Agent sign-up returns a key. Data calls answer 402 with `checkout_link_endpoint` until the account is funded, and again when the balance runs out. | 4 | https://docs.financialdatasets.ai/agents.md |
| Orthogonal (API marketplace) | `starterCredit` of $1 on agent sign-up, a pricing endpoint, and an emailed payment link the owner pays. | 1, 4 | https://orthogonal.com/agent-signup.md |
| Context.dev (web data API) | auth.md `service_auth` with a sign-up offer of 1,000 extra credits for new customers. | 1 | https://www.context.dev/auth.md |
| Boat (sandboxes) | Its auth.md sends agents to `GET /api/v1/limits` for the plans, what the trial includes and what it caps. Pre-claim tokens may call only `/me` and `/limits`, and everything else answers `claim_required`. | 1, 4 | https://boat.dev/auth.md |

Specifications:

- AAuth Budgets (draft-hardt-aauth-budgets-00, 2026-09-28) carries a budget in AAuth tokens, in a unit the resource declares, with `budget_units` and a `usage_endpoint` in resource metadata. Its rule is the one in section 3: "A resource MUST NOT let metered consumption exceed the granted amount." An exhausted budget is signalled as a 401 or 202 step-up to the person's server, and 402 is left to payment. It defines no prices. auth.md registrations don't carry AAuth tokens, so this extension puts the cap on the registration instead. https://datatracker.ietf.org/doc/draft-hardt-aauth-budgets/
- The Delegation HTTP authentication scheme (draft-mcgraw-httpapi-agent-budget-04) has the requester present signed, bounded authority to spend, and answers with 401 and 403, not 402. https://datatracker.ietf.org/doc/draft-mcgraw-httpapi-agent-budget/
- Payment instruments bound one instrument: the x402 `upto` scheme (a maximum per payment), Stripe Shared Payment Tokens (`usage_limits.max_amount`) and the Agentic Commerce Protocol's delegated payment `allowance`. This extension bounds the registration, whatever pays for it.

Threads in workos/auth.md:

- workos/auth.md#13 proposed origin fields for metering anonymous registrations. The maintainer asked how they could be trusted when they "can be easily spoofed", and said such fields "don't belong in the protocol spec / reference implementation" because they're "not globally applicable". This extension relies on no agent-supplied value except a cap that can only go down, and it lives outside AUTH.md for the second reason.
- workos/auth.md#24, point C: "A scope has no ceiling. ... An agent registered for `api.write` on a payments API is authorized for a $10 transfer and a $10,000,000 one identically." A spend cap is that ceiling for money.
- workos/auth.md#31 proposes showing terms at the same consent step. The two compose.

## Open questions

1. Should AUTH.md link extensions like this one, and say once that agents ignore `agent_auth` members they don't recognize?
2. Should `billing` live in `agent_auth` (where agents already look before registering) or in the protected resource metadata (prices belong to the resource)?
3. Should a service that offers paid usage to agent registrations be required to publish `agent_auth.billing`?
4. Are decimal strings enough for sub-cent metered prices? AAuth Budgets uses integers with a `decimals` field instead (its appendix A.1 argues for it).
5. Should `spend_cap_reached` be a 402 or a 403? Both IETF drafts above treat a spent budget as an authorization limit (401 or 403) and keep 402 for payment, and OpenRouter answers spent budgets with 403. This draft prefers 402 for every refusal only a person can clear, and agents key on `billing` either way.
6. Should storage that paused work keeps using count against the cap, so the person is never billed past it, or may a service keep billing it past the cap if `pricing_uri` says so? Deleting the data at the cap is not an option (section 3, rule 3).
