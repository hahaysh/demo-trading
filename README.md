# Demo Trading

Governed research and paper-trading software for Korean equities and ETFs.

The pilot is intentionally limited to backtesting and the Korea Investment Open
API paper environment. Strategies are immutable, risk checks are independent,
and promotion always requires human approval. See
`docs/plans/ats-master-plan.md` for scope and delivery gates.

The staged live-trading development extension is approved in
`docs/plans/live-autotrading-plan.md` and ADR 0003. This is not approval to send
live orders. A separately gated production quotation client is now available;
the existing order contracts remain paper-only.

## Local Setup

Deployment readiness: **BLOCKED**. See `docs/plans/predeployment.md` for milestone
status, real integration gaps, and approval requirements. The synthetic workflow
below is executable; it is not a real-data or KIS certification result.

Prerequisites: `uv` and Python 3.11. `uv` can install the pinned Python version.

```powershell
uv python install 3.11
uv sync --all-groups
uv run ruff check .
uv run pyright
uv run pytest
```

## Runnable Synthetic Workflow

```powershell
uv run python -m ats.demo --output .local/my-new-run
```

Use a new directory each run. The workflow writes verified synthetic raw artifacts,
replay/strategy inputs, risk state, and `report.json`. It normalizes daily prices,
runs a next-session-open trend baseline, compares three immutable parameter
descendants, and exercises numerical risk acceptance/kill-switch denial. It never
connects to a broker or cloud service, promotes a candidate, or modifies policies.
The report explicitly records `deployment_ready=false` and `promoted=false`.

Prices use positive finite decimals with OHLC consistency and integer volume.
Raw bytes, normalized content hash, instrument, and session-close provenance are
verified before use. The fixture JSON format is not a KIS/DART adapter. Native
simulation uses supplied session opens and synthetic cost assumptions; calendar,
corporate-action, multi-asset and independent-engine validation remain outstanding.

## KIS Production Quotations Only

`ats.data.kis.KisQuoteClient` supports token issuance and historical daily prices
only. It cannot submit, amend or cancel orders, query accounts, or transfer funds.
No account number is needed. Production keys are not intrinsically read-only;
keep them outside research/strategy processes.

Read `docs/sources/kis-market-data.md` before use. Personal own-asset use and
no third-party provision are documented by KIS; retention and account-specific
terms still require operator review. `config/source-allowlist.yaml` remains
DRAFT and disabled. Implementation approval did not approve collection.

This command is offline and does not read credentials:

```powershell
uv run python -m ats.data.kis --symbol 005930 --start 2026-09-01 --end 2026-09-30
```

After rights review, the operator supplies an approved source policy and sets
`KIS_QUOTE_APP_KEY` and `KIS_QUOTE_APP_SECRET` in the local collector environment
using their secret manager. Never paste values into chat or commit them. This
module does not automatically load `.env` files. An explicit `--allow-network`
enables the connection only after the policy gate passes; use `--policy` to select
the operator-approved file. Do not enable the repository policy merely to test.

The connection-check CLI prints only observation time, digests, row count and
unverified-coverage status; it does not print prices, tokens, or write files.
The Python API returns an in-memory `KisQuoteReceipt` with raw bytes, request,
observation time and source-policy digest. The request covers at most 100 calendar
days ending before today in Seoul. Prices are KRX daily, original/unadjusted.
Missing rows are not synthesized and coverage is not claimed. This is not yet a
KIS-to-snapshot normalization or backtest adapter.

Run deterministic tests with fabricated responses and no broker credentials:

```powershell
uv run pytest tests/unit/data/test_kis.py -q
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
broker, signal implementation, or sandbox is supplied. A separate local artifact
resolver is available below; engine-specific materialization is not implemented.
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

## Local Artifact Resolution

`ats.data.LocalArtifactResolver(root, max_bytes=16 * 1024 * 1024)` reads a
prepopulated, trusted directory using `root/sha256/<64 lowercase hex digits>`.
It returns exact bytes only after SHA-256 verification and rejects missing,
corrupt, oversized, non-regular, or statically linked artifacts. It has no writer,
network fetch, repair, installation, or read-cache behavior.

Use `read_digest`, `read_artifact`, `read_raw_payload`, or
`read_revision_evidence` for explicit reads. Use
`resolver.select_verified_records_as_of(snapshot, at=cutoff, revision_orders=orders)`
to verify all visible raw revisions and applicable ordering evidence before
receiving selected records. Future-observed artifact files are not read. The
original `select_records_as_of` remains a pure, filesystem-free selector.

The store must be protected from concurrent untrusted writes; static path checks
are not a race-proof sandbox. A successful read proves byte integrity, not source
rights, timestamp truth, normalized-content correctness, or the meaning of a
revision report. Consumers must use verified bytes or reverify when reading again.
Actual symlink integration tests may be skipped when OS privileges are unavailable.

## Historical Universe Membership

`UniverseMembershipArtifact` holds the actual declarations behind a snapshot's
`UniverseMembershipManifest` reference. `UniverseMember` includes an instrument,
asset class, membership basis, observation time, effective interval, and evidence.
Equities use `KOSPI_200`; ETFs use `ETF_ALLOWLIST`. These labels are not proof of
exchange membership or operator approval.

Use `resolver.read_universe_membership(reference)` to verify and parse the exact
JSON bytes and match the manifest ID/as-of horizon. Use
`resolver.select_universe_members_as_of(snapshot, at=cutoff)` to return sorted
members both known and effective at that instant. Observation and interval start
are inclusive; interval end is exclusive (or open when omitted). Overlapping
declarations and queries beyond the snapshot/manifest horizons fail. For explicit
lookup, `artifact.require_member(instrument_id, at=cutoff)` rejects absent members.

Source evidence, completeness, interval correction provenance, and ETF approval
authority are not authenticated. Later-known removals must not be backdated into
an earlier declaration. The input builder below combines membership and record
selection but does not authorize orders. The existing data schema generator includes
`schemas/data/universe-membership.v1.schema.json`.

## Per-Decision Inputs

Use `ats.data.build_decision_inputs(resolver, snapshot, at=cutoff,
source_policy=approved_policy, revision_orders=orders)` to produce a frozen
`DecisionInputBundle`. The source policy is required; there is no implicit
allow-all default. It combines
verified record selection with effective, known universe membership. Instrument
records outside that universe are excluded with a source/item/revision reason.
Records with no instrument ID default to exclusion because they might represent
unresolved symbols rather than market-wide information. Explicit
`unscoped_policy=UnscopedRecordPolicy.INCLUDE` permits them and records that choice.

The builder verifies all visible raw history before scope filtering, not only
admitted records. Missing/corrupt artifacts or ambiguous revisions fail without a
partial bundle. Future raw observations and ordering evidence are not read.
`bundle.content_digest()` binds snapshot identity, UTC cutoff, universe, effective
members, applicable revision orders, source-policy reference, data requirements, inclusion policy,
records, and exclusions.
It does not remove the underlying snapshot's order-sensitive identity.

This is an in-process provenance receipt, not a persisted schema or trade
authorization. Engine integration remains unimplemented; explicit local freshness
requirements can be supplied as described below.
Direct model creation is not proof of verified bytes;
consumers that later read artifact files must use the resolver again.

## Source Eligibility

Bundle construction rejects policies that are not approved by the cutoff. Before
artifact I/O, every visible raw revision must come from a listed, enabled,
legally approved source, with matching resolved rights. This includes superseded
revisions and records later excluded by universe/unscoped filtering. Applicable
revision-order evidence must also have an eligible source. Future observations
do not affect earlier source eligibility checks. Failures produce no bundle.

`bundle.source_policy` pins the complete policy by ID, version, and digest.
Use `bundle.validate_source_policy(policy)` to resolve the reference and recheck
receipt eligibility. Rebuild from the original snapshot to recheck discarded raw
revisions. The caller must supply the trusted operator policy; hashes and approval
metadata do not authenticate the approver or prove current policy activation.

Collection-time rights, revocation, retention, rate limits, and universe
evidence authorization remain separate controls. Low-level resolver/selection
methods do not apply this gate. Repository policies remain `DRAFT`; only synthetic
test policies are approved in memory, and external collection remains disabled.

## Required Data

Pass `data_requirements=(SourceDataRequirement(...), ...)` to the builder to
require source data and freshness. Each rule specifies `requirement_id`,
`source_id`, `min_records`, `freshness_basis`, and `max_age_seconds`. Optional
`source_item_id` and `instrument_id` restrict matching; omitted filters match all
admitted records for that source. Requirements are included in the bundle digest.

Choose `OBSERVED`, `PUBLISHED`, or `EFFECTIVE` explicitly. The builder does not
invent a fallback timestamp. Missing/future timestamps, stale data, or too few
admitted records cause failure. All matching records must pass, so one recent
record cannot hide an older matching record. Age is elapsed UTC seconds, not
trading sessions; the maximum is inclusive. Source-specific economic timestamp
meaning must be established before using `EFFECTIVE` as a freshness basis.

An empty requirement tuple means no availability/freshness checks. This preserves
selection-only use; it is not a completeness guarantee. Requirements currently
come from the caller, not an authenticated operator policy store, and production
consumers must prevent strategy-controlled omission or relaxation. No operational
thresholds or repository policy approvals were introduced.

## Local Input Replay

Create `InputReplayRequest` with `snapshot`, `source_policy`, ordered `cutoffs`,
and explicit `data_requirements`, plus optional `revision_orders` and
`unscoped_policy`. Call `ats.data.replay_inputs(resolver, request)` to build all
per-decision bundles using the same pinned inputs. Cutoffs must be aware,
strictly increasing, and within snapshot/manifest horizons. Equivalent timezone
representations are normalized to UTC; invalid schedules are not silently sorted.

On success, `InputReplayResult` contains the complete ordered bundles and the
request digest. `result.validate_against_request(request)` checks declared
provenance and exact cutoff coverage, and `result.content_digest()` supports
repeat-run comparisons. No input archive, policy, or source file is modified.

The first failed cutoff raises `InputReplayError` with `index`, `cutoff`, and the
original chained cause; earlier bundles are not returned. There is no fallback
to prior data or partially successful replay. The result is kept in memory, not
published transactionally to storage. The archive must remain trusted/read-only.

This workflow replays input validation, not trading strategies or returns. It
does not run Qlib/LEAN, authenticate policies, calculate metrics, or submit orders.
An explicit empty requirement tuple still makes no freshness guarantee. Actual
engine integration and policy/snapshot changes across cutoffs remain separate work.
