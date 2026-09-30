# KIS Production Quotations: Usage Review and Connection Boundary

Reviewed: 2026-10-01. Status: ONE-OFF SMOKE OPERATOR-APPROVED AND USER-REPORTED
SUCCESSFUL; GENERAL COLLECTION NOT APPROVED.

## Official Evidence

- [Service introduction](https://apiportal.koreainvestment.com/about-open-api):
  market information is for account-holding individuals investing their own
  assets; provision to third parties is prohibited. Corporate use requires a
  separate market-data agreement. A personal account is not a redistribution license.
- [Personal token issuance](https://apiportal.koreainvestment.com/apiservice-apiservice?/oauth2/tokenP):
  `POST /oauth2/tokenP`, client credentials. Personal tokens last 24 hours;
  repeat issuance within six hours can return the existing token. Honor the
  returned absolute expiry as well as `expires_in`, rather than extending a
  reused token locally. Token issuance can trigger a notification to the customer.
- [Domestic period prices](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice):
  `GET /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`, production
  origin `https://openapi.koreainvestment.com:9443`, TR `FHKST03010100`.
  Maximum 100 rows per response; `tr_cont` continuation is not supported.
  `J` selects KRX, `D` daily prices, and `FID_ORG_ADJ_PRC=1` original prices.

## Rights Gate

The reviewed public pages do not establish a permitted retention duration,
unrestricted archival/backtesting rights, third-party cloud/AI sharing rights,
or this account's agreed rate quota. These are unresolved, not implicitly allowed.
The operator has attested review of their applicable agreement and approved a
one-off personal-use connection check, reporting a permitted retention end date
of 2027-09-30 and accepting a local one-request-per-minute cap. This is an operator
attestation, not independent verification of the agreement or a provider quota.
`config/kis-quote-smoke-policy.yaml` records the approval with a stricter one-day
local retention maximum. The check does not persist raw data. Broader collection
and storage require their own reviewed scope and retention enforcement. Do not
send raw prices to external AI services or publish them as fixtures. Tests use
fabricated responses.

`config/source-allowlist.yaml` remains DRAFT with `kis-market` disabled. The
connection must reject missing/unapproved/future approvals, unresolved rights,
redistribution permission, missing retention and missing rate limits before
authentication or any network request. Policy metadata is an operator claim, not
cryptographically authenticated permission. No legal approval is fabricated here.

Implementation approval permits offline development and tests of the gated
connector; the subsequent explicit source-policy approval permits the named
one-off smoke only. It does not enable recurring collection or live orders.

## Implemented Connection Boundary

Only token issuance and daily-price retrieval are allowed. No order, amendment,
cancellation, account inquiry, transfer, configurable origin, arbitrary request,
redirect or proxy forwarding is part of this client. Production keys are not
broker-enforced read-only credentials: these restrictions belong to this client,
not to the key itself or a hostile process that can read it.

Secrets stay in an operator-owned collector process, never in strategy/generated
code, fixtures, logs, request URLs or repository files. Responses and exceptions
must not reveal credentials or token bodies. No credential file is inspected by
the coding assistant. Network calls are opt-in, not an import/test side effect.

This first connection returns raw response bytes with observation time and
request/policy provenance, in memory only. It does not approve persistence,
normalize a backtest-ready snapshot, invent historical publication timestamps,
or establish full date-range coverage. The offline adapter in `ats.data.prices`
now preserves corporate-action flags and rejects malformed/no-open rows; snapshot
materialization and corporate-action resolution remain separate work.

## Offline Daily Normalization

`normalize_kis_daily_prices(receipt, session_closes=...)` returns an immutable
`KisDailyNormalization` containing `KisNormalizedDailyPrice` entries. Each entry
contains the existing `DailyPrice` format and `KisCorporateActionFlags`. The
normalizer revalidates receipt metadata, response status/instrument/count, bounded
raw JSON, required field types, OHLC, volume and session dates. Duplicate JSON
keys and duplicate dates are errors. Invalid rows fail the whole batch, not a
partially accepted result. Valid rows are sorted by session.

The calendar mapping must provide an aware closing timestamp for every returned
date, on that Korean session date and no later than observation. Receipt end date
must precede observation's Korean date, matching the quotes-only client. Closing
times are never invented; the mapping's authority and completeness are caller
responsibilities, not established by this adapter.

Wire flag mapping:

| KIS field | Normalized field | Handling |
| --- | --- | --- |
| `flng_cls_code` | `ex_rights_code` | Blank/00 retained; 01-07 require review; other codes rejected |
| `prtt_rate` | `split_ratio_text` | Decimal text preserved exactly; blank or nonzero requires review; no adjustment applied |
| `revl_issu_reas` | `revaluation_reason` | 00 retained; blank, 01-08 or 99 require review; unknown codes rejected |
| `mod_yn` | `no_open_indicator` | Y rejected as no opening trade; blank retained for review; N accepted |

Zero-volume bars also require review. Consumers must inspect `requires_review`
and keep unresolved bars out of certified research. The flag is an in-process
property computed from preserved fields, not a serialized approval or a separate
corporate-action resolution system.

Observation time is preserved, not backdated to the historical price date.
The batch pins the raw SHA-256, policy digest and normalizer version. Computing
a digest binds supplied bytes; it does not independently verify origin, approval,
or a previously trusted hash. No publication timestamps or revision history are
invented. Normalization does not grant collection/storage rights, populate a
snapshot, or change `coverage_verified=false`.

On 2026-10-01, 149 relevant offline tests passed across price normalization,
quotation handling and the native backtest, including a mock quote-to-normalizer
integration. Types and lint passed. No real API request or credential access was
performed for this implementation; the smoke response was not available to replay.

## Local Operation

Implementation: `src/ats/data/kis.py`. Offline tests: `tests/unit/data/test_kis.py`.
The client uses HTTPX with TLS verification, environment proxies disabled,
redirects disabled, 10-second per-operation timeouts, response size caps and an
elapsed-response budget checked at received chunks. The latter is not a hard
end-to-end deadline for DNS, headers or every socket operation.

Calls are serialized and spaced by the stricter of one second and the approved
source rate. The token is cached only in memory until the earlier of its returned
absolute expiry and relative lifetime, with a 60-second safety margin. Failed
token issuance has a local 60-second cooldown; no request is automatically retried.
The personal-token timestamp is interpreted as Korean local time; a provider
format change fails closed and requires review rather than an expiry guess.

Use one long-lived collector per key. Tokens, throttling and locks are per client,
not shared across processes or restarts. Persistent encrypted token storage and
multi-process quota coordination are not implemented. Reload the latest trusted
operator policy for each call; an old policy object cannot discover revocation.
Do not enable HTTP wire logging or expose process/debugger memory to research code.

`KisCredentials.from_environment()` reads only `KIS_QUOTE_APP_KEY` and
`KIS_QUOTE_APP_SECRET`. The CLI does not read credentials without `--allow-network`
and a passing policy. It defaults to an offline message and never persists data.
The public client API is `daily_prices(KisDailyRequest(...), source_policy=...)`;
private test hooks are not a sandbox against code with collector-process access.

The 2026-10-01 validation used 61 fabricated-response tests, not real credentials
or broker responses. Checks cover policy rejection before authentication, routing,
redirect/retry rejection, timeouts, bounded streams, token reuse/expiry, invalid
responses, secret-safe errors and offline CLI behavior. The separate user-run
production result below establishes a reported quotation connection, not account
inquiry, data-quality certification or historical coverage.

## User-Reported Production Smoke

The user ran the documented command for symbol `005930`, start/end `2026-09-30`,
with the dedicated smoke policy, and supplied the following CLI receipt. Terminal
context reports exit code 0. Observation time in Korea is 2026-10-01
02:00:21.626656 (+09:00).

```json
{
  "source_id": "kis-market",
  "observed_at": "2026-09-30T17:00:21.626656+00:00",
  "raw_payload_digest": "sha256:750fd116a7ba9322bd935bd6603444d19375017c93c3a31db014a338f09c2958",
  "policy_digest": "sha256:3883bdcd4d3de0de38033f8399ac858a62e089771020ca639e2930a8eda3d914",
  "row_count": 1,
  "coverage_verified": false,
  "persisted": false
}
```

The supplied policy digest was recomputed from the local smoke policy and matched.
No extra broker request or credential read was performed to record this evidence.
The raw payload was not retained, so its digest and price values cannot be
independently rechecked here. This receipt is user-supplied evidence, not a broker
signature or a reproducible saved dataset. It reports successful authentication
and one validated quotation row; it proves neither complete historical coverage
nor backtest, account access, paper execution or live-trading readiness.
