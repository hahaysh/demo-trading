# KIS Production Quotations: Usage Review and Connection Boundary

Reviewed: 2026-10-01. Status: PUBLIC DOCUMENTS REVIEWED; COLLECTION NOT APPROVED.

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
The user must review their applicable agreement or obtain provider clarification,
record private own-asset use, retention and a conservative rate limit, and approve
the operator-owned source policy before collection. Do not send raw prices to
external AI services or publish them as fixtures. Tests use fabricated responses.

`config/source-allowlist.yaml` remains DRAFT with `kis-market` disabled. The
connection must reject missing/unapproved/future approvals, unresolved rights,
redistribution permission, missing retention and missing rate limits before
authentication or any network request. Policy metadata is an operator claim, not
cryptographically authenticated permission. No legal approval is fabricated here.

Implementation approval permits offline development and tests of the gated
connector, not enabling collection while the above review is incomplete.

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
or establish full date-range coverage. A separate adapter must preserve corporate
action flags and handle incomplete/current-session rows before research use.

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
responses, secret-safe errors and offline CLI behavior. Actual account access and
data coverage remain unverified.
