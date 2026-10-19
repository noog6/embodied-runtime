# OpenRouter response cost accounting

Compatible cognition and compatible vision automatically account for `usage.cost`
when the configured endpoint is `https://openrouter.ai/api/v1` (optional trailing
slash or explicit port 443). No model-name matching or catalog entry is needed.
Other compatible origins, HTTP endpoints, alternate ports, proxy URLs, and
unverified API paths do not acquire OpenRouter monetary semantics. No new request
parameters, lookups, retries, pricing discovery, or model discovery are added.
Existing requested/returned model identities and token accounting are preserved.

## Meaning and unit

Official references checked on 2026-10-19:

- [Usage Accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
  identifies `usage.cost` as the total charged to the OpenRouter account, supplied
  automatically with the response. `cost_details.upstream_inference_cost` is a
  different measure and is never substituted, added, or persisted here.
- [FAQ: Credit and billing systems](https://openrouter.ai/docs/faq#credit-and-billing-systems)
  states that the credit system's base currency is US dollars and API pricing is
  denominated in dollars. This is why normalized monetary fields use `_usd`.

Reported amounts describe OpenRouter account charges represented in received
responses. They are not an invoice reconciliation. In particular, BYOK can incur
separate charges at the upstream provider; a reported OpenRouter charge of zero
can be valid while upstream inference still costs money. This accounting does
not measure that additional bill, credit purchase fees, taxes, or subscriptions.

## Validation and precedence

The response extractor supports dictionaries, SDK attributes, and SDK
`model_extra` extensions. It reads only `usage.cost`. Integers, decimal numbers,
SDK-decoded finite floats, and numeric strings are accepted when nonnegative,
including explicit zero. Floats are converted through their decimal string
representation; precision already lost by SDK JSON decoding cannot be recovered.
Missing, null, booleans, malformed, negative, and non-finite values are unavailable.
Parsing is bounded to 128 text characters, 40 coefficient digits, decimal exponents
from -24 through 12, and amounts below 10^13 USD. Out-of-bound values are unavailable
and do not fail a successful model response.

Each outbound attempt selects exactly one monetary basis:

1. Valid OpenRouter-reported charge, regardless of token completeness or output
   acceptance.
2. Otherwise, an applicable valid catalog estimate from complete, consistent token
   usage. Direct-provider context limits and cache-read/write rules still apply.
3. Otherwise, unavailable. Transport failures without usage/cost evidence are
   unknown, never assumed free.

Client setup failures are not outbound attempts. Received charges survive refused,
truncated, malformed, or otherwise rejected output. Failure and cancellation still
propagate normally. There are no new provider requests. Existing successful hosted
TTS synthesis retains its catalog character-rate estimate; a provider-reported
character count is usage evidence, not a reported monetary charge.

Money and token completeness are independent. Known money does not fill in missing
tokens. Conversely, complete tokens do not guarantee a monetary total when no rate
or reported charge applies. Monetary accumulation uses decimal arithmetic separate
from the bounded diagnostic deque and retains sub-microdollar precision. Serialized
amounts are fixed-point strings with at least six fractional digits.

## Summary schema 6

`summary.json` retains the previous usage/counter fields and adds these cost fields
both at `cost` and within its `cognition`, `vision`, and `tts` components:

| Field | Meaning |
| --- | --- |
| `status` | `reported`, `estimated`, `mixed`, or `unavailable` |
| `total_usd` | Complete total; null if any relevant attempt lacks money evidence |
| `reported_usd` | Sum of known provider-reported charges |
| `catalog_estimated_usd` | Sum of known catalog estimates, excluding reported calls |
| `known_usd` | Sum of those two known subtotals, even when the total is incomplete |
| `reported_calls`, `estimated_calls`, `unknown_calls` | Monetary evidence counts; TTS counts syntheses |
| `estimated_usd` | Legacy complete catalog-only estimate; null for reported, mixed, or incomplete totals |

The root also includes `currency: "USD"` and the configured `pricing_identity`.
That catalog identity applies to estimates, not to reported charges. A component
with no calls contributes zero with `status: "estimated"`; a run with no accounted
calls has an unavailable total. One unknown attempt prevents the complete run total,
but does not erase known subtotals or independently complete role components.

Each bounded `provider_calls` record adds only `reported_cost_usd`, `cost_usd`, and
`cost_basis` (`reported`, `estimated`, or `unavailable`). No arbitrary metadata,
response text, prompts, credentials, or cost details are retained. The summary
banner shows the neutral total, basis, known subtotals, and unknown count. Mixed or
estimated values are never described as actual invoice charges.

Historical schemas 3–5 keep their original estimate semantics and remain usable by
the banner. `run.json` stays at schema 1; run-history status readers do not parse
`summary.json` and require no migration. External summary readers should accept
schema 6 and read `total_usd` plus `status` to display all accounting bases.

Benchmark JSON format 4 adds `cost_usd`, `cost_basis`, `reported_cost_usd`,
`catalog_estimated_cost_usd`, and `unknown_cost_calls` to trials. The legacy
`estimated_cost_usd` remains a complete catalog-only estimate. The harness uses
cognition monetary subtotal/count deltas after startup, preserving the scenario
boundary even if prewarm had no cost metadata. Text reports label the basis of
trial, model, and report totals. Unknown scenario calls make the relevant total
unavailable; prewarm remains excluded. Scenario definitions and pass/fail rules
are unchanged. Legacy in-memory trial objects with only estimate fields still
render as estimates.

## Offline verification and manual live validation

Offline checks:

```sh
python -m pytest -q tests/test_openrouter_costs.py tests/test_observability.py \
  tests/test_openai_compatible.py tests/test_portable_visual_perception.py \
  tests/test_cognition_benchmarks.py
python -m pytest -q
python -m unittest discover -q
python -m compileall -q embodied_runtime tests
```

No paid live calls were made to verify this change. To validate on an account:

1. Follow [compatible configuration](cognition-compatible.md#configure-openrouter)
   with virtual hardware and a protected `OPENROUTER_API_KEY`. Set `model` to a newly
   selected tool-capable OpenRouter model absent from the built-in pricing catalog.
   Do not change the catalog. Choose the supported parallel-tool parameter mode.
2. Start `python main.py --config /tmp/mira-openrouter.toml`, submit one short ask,
   and exit normally. Startup/prewarm is a real call and is included in run cost.
3. Inspect the shutdown banner and that run's `data/runs/R<n>/summary.json`.
   Every attempt with valid metadata should have `cost_basis: "reported"` and a
   normalized `reported_cost_usd`, including zero when appropriate. With all
   relevant calls covered, `cost.total_usd` should be present and equal the exact
   sum; with only OpenRouter calls its status should be `reported`. TTS or direct
   provider estimates can make the total `mixed`.
4. Compare the OpenRouter account activity for those calls. For a long run, use
   activity records rather than assuming the bounded diagnostic list contains
   every call. Missing cost metadata must produce an unavailable complete total
   when no valid estimate applies, with known amounts retained separately.

Optionally run the documented production benchmark once using that same model.
Format 4 trial cost must exclude prewarm and preserve the reported/mixed basis.
These manual steps incur normal account usage; they have not been executed here.
