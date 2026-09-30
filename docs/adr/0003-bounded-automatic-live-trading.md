# ADR 0003: Bounded Automatic Live Trading

- Status: Accepted for staged implementation; live activation not approved
- Date: 2026-10-01
- Supersedes: Paper-only development scope for this extension only; existing
  paper artifacts and operational gates remain unchanged.

## Context

The approved scope currently excludes live trading. The user has now requested
automatic strategy-submitted live orders for personal use and reports holding
production KIS keys. The user subsequently confirmed cash-only account funding,
no additional daily buy-amount or order-count limits, retention of the existing
stop criteria, and all eligible domestic ordinary stocks and ordinary ETFs.
The plan retains 1% daily loss, 15% drawdown and 10% symbol concentration limits.
Overseas markets, leverage, shorting, derivatives and leveraged/inverse products
remain excluded. These requirements supersede the initial proposal for a fixed
KRW budget and per-symbol allowlist, not the active paper-only implementation.
No operational policy was changed or live activation granted.

## Decision

The user approved `docs/plans/live-autotrading-plan.md` and this ADR for staged
implementation on 2026-10-01 and instructed work to start with data-terms review
and the production quotes-only connection. The operating requirements are
resolved; champion/account binding and activation evidence remain separate.
Do not weaken or silently reinterpret the current paper-only implementation.

Use an eligibility-based universe and verified cash-based sizing rather than
requesting a fixed pilot amount or hand-picked symbols again. Version these
changes without rewriting existing strategies or historical snapshots. Absence of a turnover/count
cap is distinct from missing configuration and does not waive risk checks,
broker rate limits, cash reservations, or reconciliation. Retain baseline
loss/drawdown/concentration controls. Cash flows change available funds but must
not erase losses or clear a halt. New eligible symbols require verified metadata
and data coverage, not a separate human decision on each symbol.

Order automation and research autonomy are distinct: a human approves the
immutable champion and time-limited operating envelope, then an independent
risk/dispatch service may submit matching intents without per-order approval.
Research cannot mutate that envelope or promote a replacement champion.

Keep production quote access, paper execution, and live execution as distinct
capabilities. Possessing production credentials or a nearly empty account does
not arm live execution. Missing limits and approvals fail closed. Require durable
reservations, duplicate/unknown-outcome reconciliation, restart-safe halts, and
an operator kill switch. Preserve existing certification and paper evidence gates.

## Alternatives

- Keep paper trading only: remains the operational baseline until live activation.
- Production quotes with no orders: useful for research, but not the user's
  requested final execution model; it does not imply live-order permission.
- Manual confirmation for every order: not the selected target model. Human
  approval still governs activation, champion changes, policy changes and recovery.

## Consequences and Approval

This adds financial exposure and significant integration/operational obligations.
Existing `KIS_PAPER` literals must not simply be replaced with production values.
Paper validation remains a prerequisite, not equivalent to testing against an
almost-empty real account. Implementation approval covers the confirmed cash-only
sizing, domestic eligibility and retained safety criteria in the linked plan.
The current task is quotes-only; first-live activation remains a later decision.
This is a recorded user development approval, not a signed runtime approval or
data-usage license. Source policies remain DRAFT pending operator rights review.
