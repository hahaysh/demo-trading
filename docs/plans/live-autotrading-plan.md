# Bounded Automatic Live Trading

- Status: APPROVED FOR STAGED IMPLEMENTATION - LIVE ACTIVATION NOT APPROVED
- Requested: 2026-10-01
- Implementation approval: 2026-10-01, user instruction to approve the plan/ADR,
  check data terms, and start the production quotes-only connection.
- Decision record: `docs/adr/0003-bounded-automatic-live-trading.md`
- Existing approved baseline: `docs/plans/ats-master-plan.md`

## Request and Authorization Boundary

The user identified the intended use as personal research, reported having a KIS
account and production Open API credentials, and selected strategy-submitted live
orders rather than per-order manual confirmation. Account configuration and key
permissions have not been verified. No credentials were collected or used.

The user approved this plan and ADR 0003 for staged implementation, starting with
production quotations only. This does not approve a live strategy, source-policy
activation, broker order submission, or deployment. The current paper-only
contracts, DRAFT policies, source rights gates, and deployment restrictions remain
unchanged. A low account balance is not a risk control or evidence of readiness.

### Confirmed Requirements: 2026-10-01

The user clarified the earlier account-funded, unrestricted request:

- Capital: cash only, up to verified available account cash, with no additional
  fixed KRW pilot budget. No credit, margin, or unsettled borrowing is authorized.
- Daily activity: no additional cumulative buy-amount or order-count cap.
  Existing stop criteria remain: 1% daily portfolio loss and 15% drawdown.
  The existing 10% per-symbol concentration limit is also retained.
- Instruments: all eligible domestic ordinary stocks and ordinary ETFs, not
  only KOSPI 200 constituents or a hand-picked ETF list. Overseas instruments,
  derivatives, leveraged/inverse products, and short selling remain excluded.
- Execution: strategy-submitted orders without per-order confirmation, within
  an explicitly activated, human-approved immutable champion and risk policy.

These answers resolve the three requirement questions; do not request a fixed
pilot amount or per-symbol list again. They do not prove readiness or grant
permission to send orders. Available cash limits a purchase, not cumulative
turnover or eventual losses. Loss halts stop new submissions; they do not
guarantee that losses cannot exceed a threshold.

## Approved Target Operating Model

After explicit human approval of a named immutable champion, account alias,
eligibility policy, cash-based sizing rule, schedule, and activation period, that champion
may submit qualifying orders without asking for confirmation for each order.
Every intent must still pass an independent risk service immediately before
submission. Research agents cannot submit orders, access broker secrets, change
risk policy, activate a strategy, or promote their own descendants.

Preserve the existing cadence: end-of-day decisions and the next trading
session's approved opening window. This proposal does not add intraday signals,
leverage, margin, short sales, derivatives, or leveraged/inverse products. Initial
support is cash-funded LIMIT buys and sells restricted to actual unreserved long
holdings. Tick sizes, eligible instruments, price bands, and exchange calendars
must be verified independently of strategy output.

Buying power must come from verified cash-only broker state, not total account
equity or a credit-enabled buying-power field. Reconcile pending reservations
and costs exactly once before allocating funds. Deposits, withdrawals and usable
sale proceeds change available cash under the same policy, but must not reset
cash-flow-adjusted loss/drawdown measurements or clear a persisted halt. No order
is affordable merely because one share fits the raw cash balance: concentration,
fees, pending exposure and all other independent checks still apply.

An eligibility-based universe is not permission to trade an unknown instrument.
Require point-in-time instrument classification, trading status and data coverage;
reject unclassified, suspended or unsupported instruments. Historical research
must preserve delistings and membership rather than use today's symbol list.
The existing KOSPI 200 contracts and historical snapshots remain unchanged until
an explicitly versioned broader-universe implementation is approved and tested.

## Confirmed Settings and Activation Inputs

Confirmed requirements and remaining activation inputs are distinguished below.
None is an active live configuration. An explicit no-cap policy must be versioned;
missing configuration is not unlimited, and a zero permitted budget blocks orders.

| Setting | Status | Required meaning |
| --- | --- | --- |
| Pilot capital | CONFIRMED: CASH ONLY | Verified available account cash after reservations/costs; no separate fixed KRW ceiling |
| Per-order sizing | DERIVED DESIGN | Available cash and remaining 10% symbol capacity; include costs and pending exposure, never round up to afford one share |
| Daily buy budget | CONFIRMED: NO ADDITIONAL CAP | Cash, exposure, reservations, duplicate checks and loss halts still apply |
| Daily loss halt | CONFIRMED: RETAIN 1% | Realized and unrealized portfolio losses with costs and cash-flow-adjusted day-start valuation; no new absolute KRW limit |
| Drawdown halt | CONFIRMED: RETAIN 15% | Cash-flow-adjusted portfolio high-water mark; deposits do not erase losses |
| Per-symbol concentration | RETAIN 10% | Independent pre-trade limit including pending buy exposure |
| Daily order count | CONFIRMED: NO ADDITIONAL CAP | Broker rate limits, duplicate suppression, and unknown-outcome stops remain required |
| Instrument eligibility | CONFIRMED: DOMESTIC ORDINARY STOCKS/ETFS | No hand-picked list; exclude unsupported, unclassified, leveraged/inverse and other out-of-scope products |
| Champion version/digest | UNSET | Human-approved immutable strategy and artifact identities |
| Account binding | UNSET | Local alias bound to verified broker account; no account number in repository docs |
| Existing position ownership | ACTIVATION INPUT | Reconcile all exposure; identify strategy-managed holdings before granting sell authority |
| Activation expiry and operator contact | UNSET | Time-limited authority and an available stop/alert path |
| Price deviation, quote/state freshness, opening window | UNSET | Operator-owned, tested limits, not strategy-controlled parameters |

Keep the existing 10% per-symbol limit, 1% daily loss halt and 15% drawdown halt
in the reconciled proposal. No additional absolute monetary limits are inferred.
If available funds and remaining symbol capacity cannot afford one share, do not trade;
relaxing a baseline limit requires a separately reviewed policy change.

## Implementation Sequence After Plan Approval

1. Implementation approval and ADR 0003 acceptance recorded on 2026-10-01.
  Preserve paper mode as a separate default. Version the broader eligible
  universe and cash-based policy separately from existing paper contracts;
  do not reinterpret immutable strategy/snapshot history.
2. Implement a quotes-only production client separately from order submission.
   Do not expose an arbitrary endpoint/method API to strategies. Confirm data
   storage/use rights independently of account registration.
3. Complete authenticated account/position/quote providers, durable order state,
   independent risk calculations, atomic reservations, and single-writer dispatch.
   Bind decisions to exact intent, policy, champion, and account state versions.
4. Implement a separately armed live adapter with a pinned broker environment,
   explicit account binding, endpoint restrictions, finite timeouts, redacted
   logging, and an audited activation record. No runtime paper-to-live switch
   controlled by a strategy or ordinary retry path.
5. Persist an intent before sending it. On timeout or unknown submission outcome,
   reconcile with the broker rather than blindly retrying. Partial fills,
   cancellations and replacements retain correct exposure/cash reservations.
6. Validate with local fixtures, paper integration, and read-only/shadow runs.
   Meet existing certification and at-least-20-paper-session gates before a
   separate first-live activation review. This proposal grants no waiver.
7. Only after explicit activation approval, run the bounded pilot and collect
   broker reconciliation, limit enforcement, alerts, and recovery evidence.

## Stop and Recovery Rules

- Default to disarmed. Missing policy, approval, account state, source data,
  calendar or quote evidence blocks submission; never substitute stale values.
- Kill switch, loss/drawdown breach, reconciliation mismatch, stale state,
  expired activation, or uncertain broker outcome stops new submissions.
- A stop does not imply that open orders are canceled or positions liquidated.
  Any cancellation must be confirmed by the broker; remaining exposure stays
  reserved. No automatic emergency liquidation is authorized by this draft.
- Persist halt state and the order ledger across restart. No auto-resume or
  automatic policy expansion after deposits. Cash availability may change within
  the approved cash-based rule, but human review and reconciliation are required
  before re-arming a halted account.
- Promotion, rollback, eligibility-rule changes, funding-rule changes, and
  strategy replacement require fresh human approval. Selecting another eligible
  symbol within the approved universe does not require per-symbol approval.

## Exit Evidence Before Live Activation

- Approved real-data rights and point-in-time input integrity.
- Actual Qlib/LEAN certification and reviewed comparison tolerances; synthetic
  native smoke results do not satisfy this condition.
- At least 20 real paper sessions with no unresolved hard-risk/reconciliation
  failures, as required by the existing plan.
- Negative tests for concurrent schedules, duplicate submission, broker timeout,
  partial fills, reservation release, key expiry, restart, and kill-switch recovery.
- Limits enforced using filled plus pending exposure and a cash-flow-aware loss
  basis; no live order reachable from a generated-code environment.
- Verified credential storage, log redaction, operator authentication, immutable
  approval/audit evidence, and explicit first-live activation consent.

No live orders, API calls, deployments, or changes to operational policy are
performed by creating this document.
