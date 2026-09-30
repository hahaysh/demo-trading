# Demo Trading

Governed research and paper-trading software for Korean equities and ETFs.

The pilot is intentionally limited to backtesting and the Korea Investment Open
API paper environment. Strategies are immutable, risk checks are independent,
and promotion always requires human approval. See
`docs/plans/ats-master-plan.md` for scope and delivery gates.

## Local Setup

Prerequisites: `uv` and Python 3.11. `uv` can install the pinned Python version.

```powershell
uv python install 3.11
uv sync --all-groups
uv run ruff check .
uv run pyright
uv run pytest
```

## Data Contracts

`MarketEvent` represents a derived observation, not an order. It carries a
versioned producer, confidence, availability timestamp, snapshot reference, and
the exact source revisions used as evidence. Before consuming an event, call
`event.validate_against_snapshot(snapshot)` to verify the reference digest and
evidence against the actual frozen snapshot. Event construction alone does not
resolve the snapshot. This does not replace runtime source-policy approval.

An event must be available by the snapshot cutoff and cannot predate its evidence.
Later retrospective extraction is not admitted into that historical signal path.
Publication and effective timestamps remain distinct from observed availability.

Regenerate both data schemas after changing these contracts:

```powershell
uv run python scripts/generate_data_snapshot_schema.py
uv run python scripts/generate_data_snapshot_schema.py --check
```

The generator retains its original name and now covers `DataSnapshot` and
`MarketEvent`. See `docs/plans/status.md` for implementation boundaries and the
next contract slice.

## Research Contracts

`ExperimentRun` is an immutable terminal receipt (`SUCCEEDED` or `FAILED`), not
a job scheduler. It records exact strategy and snapshot references, engine/code
artifacts, container and dependency-lock digests, seed, timestamps, and outputs.
Call `run.validate_inputs(strategy, snapshot)` before accepting it for evaluation.

`EvaluationResult` records one engine's finite measurements and a versioned
protocol reference. Call `result.validate_against_run(run)` to check the run ID
and digest, successful completion, and evaluation period cutoff. The protocol
must document metric definitions, transaction costs, and data splits. Negative
performance and data-integrity violations remain evidence, not discarded runs.
An omitted deflated-Sharpe confidence is unknown, not a passing result.

These checks do not verify artifact bytes, reproduce metrics, certify multiple
engines, authorize promotion, or replace independent risk checks. Execution,
statistical evaluation, and authenticated human approval remain separate work.

```powershell
uv run python scripts/generate_research_schemas.py
uv run python scripts/generate_research_schemas.py --check
```

## Promotion Receipts

`PromotionDecision` records an approved, rejected, or deferred review; it does
not change the champion or authorize orders. An approved receipt requires human
approval evidence bound to the digest of the exact `PromotionReview` and a
passing report reference for every declared promotion gate. Changing the review
invalidates the approval binding. Rejected/deferred receipts may retain missing
or failing gate evidence, but cannot carry an approval.

Before consuming a receipt, resolve each run's inputs and call
`decision.validate_evidence(policy, evaluations, runs)`. This checks exact
references, timing, candidate consistency, and, for approved receipts, policy
approval, Qlib/LEAN coverage, comparable snapshot/protocol/period, available
metric thresholds, and zero reported data-integrity violations.

Report statuses and `HUMAN` metadata are assertions, not verified identity or
statistical proof. An independent service must verify report contents, signatures,
reviewer authority, paper duration, risk history, fold results, slippage, and
engine tolerances before activation. The current contract implements none of
those runtime authorizations. Operator policies remain unchanged and `DRAFT`.

The existing research schema generator also covers `PromotionDecision`.

## Paper Execution Contracts

`OrderIntent` records a cash-funded, long-only KRX order for `KIS_PAPER`, with
strategy, champion-selection, snapshot, signal, account, and risk-policy
references. Quantities are positive integers. Limit prices use finite positive
`Decimal` values; market orders omit the limit price. Use decimal strings in
JSON to preserve price precision. No live environment or intraday phase is accepted.

`RiskDecision` defaults to `DENY`. An `ALLOW` receipt requires all declared
checks to have passing evidence. Before use, call
`decision.validate_for_intent(intent, policy, at=trusted_now)` using the current
operator policy and a trusted, timezone-aware clock. The decision must match the
entire intent digest and approved policy, fit inside the intent lifetime, and
satisfy `checked_at <= trusted_now < expires_at`. A modified quantity, price,
account, symbol, or other intent field invalidates the binding.

These records do not submit orders or prove real risk checks occurred. An
independent service must verify assessor authority, current positions and cash,
price freshness, exposure, losses, champion/universe eligibility, market session,
and kill switch. `SELL` must only reduce an existing long position. The contract
does not verify holdings, the trading calendar, or tick sizes. Atomic idempotency,
reservations, and revalidation at submission are still required; a digest cannot
prevent replay. All repository operator policies remain `DRAFT`.

```powershell
uv run python scripts/generate_execution_schemas.py
uv run python scripts/generate_execution_schemas.py --check
```

## Adapter Boundaries

`ats.ports` provides `QlibResearchPort`, `LeanCertificationPort`, and
`SignalEvaluatorPort`. Use `run_evaluation` or `run_signal_evaluation` instead
of calling an adapter directly to apply pre-dispatch input validation and
post-dispatch result binding. Evaluation requests pin experiment, strategy,
snapshot, engine, seed, dependency lock, protocol, and period. Signal outputs
carry artifact references only, not orders. Adapter errors propagate; no fallback
result is silently substituted.

These in-process interfaces currently have test doubles only. No real engine,
broker, signal implementation, artifact resolver, or sandbox is supplied.
Per-bar point-in-time replay and engine tolerances remain unresolved; see
`docs/adr/0002-adapter-boundaries-and-replay-semantics.md` (Proposed).

## As-Of Selection

`ats.data.select_records_as_of(snapshot, at=cutoff, revision_orders=orders)`
returns one known revision per source/item in deterministic source/item order.
The aware cutoff is inclusive and cannot exceed the snapshot freeze timestamp.
Future observations are excluded, while known announcements with future
effective dates are preserved.

Multiple visible revisions require an explicit `RevisionOrder` (oldest to
newest), including an evidence artifact and the time that ordering was observed.
Ordering learned after the cutoff is ignored. Labels and arrival order are never
used as revision precedence. Ambiguous, incomplete, or conflicting histories
raise `AsOfSelectionError` without partial results or fallback to older records.
Original snapshots and records are not modified.

This local selector is not yet integrated with an engine or a source connector.
It does not verify evidence bytes, source permissions, freshness, universe
membership, or economic applicability. Keep the snapshot, cutoff, and ordering
evidence together for reproducible selection. See ADR 0002 for the remaining
source-specific decisions.
