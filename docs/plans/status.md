# Project Status

Last updated: 2026-09-30

## Active Milestone

Phase 1 primary contracts and local adapter boundaries implemented; real adapters
and Phase 0 gates remain open. Phase 2 local as-of selection and artifact byte
verification are implemented, including historical universe artifact resolution.

## Completed

- Confirmed market, cadence, paper-only broker boundary, and pilot budget.
- Selected immutable champion/challenger improvement with human promotion.
- Selected Qlib for research and LEAN for independent certification.
- Defined initial risk and promotion thresholds.
- Persisted the approved master plan and core governance decision.
- Established the Python 3.11 `uv` package with Ruff, Pyright, pytest,
    Hypothesis, pre-commit, and a locked development environment.
- Implemented immutable `StrategySpec` lineage, bounded signal-parameter
    mutation, deterministic content digests, and append-only lifecycle events.
- Generated `schemas/strategy/v1.schema.json` with a drift-check command.
- Added typed, operator-owned source, risk, and promotion policies. All remain
    `DRAFT`; source collection is default-deny pending legal approval.
- Implemented immutable `PointInTimeRecord`, `DataSnapshot`, and historical
    universe-manifest contracts. A snapshot rejects future-observed records and
    duplicate source revisions, and has a deterministic content digest.
- Generated `schemas/data/snapshot.v1.schema.json` with a drift-check command.
- Implemented frozen `MarketEvent` observations with versioned producer evidence,
    finite confidence, unique source revisions, and availability cutoff checks.
    `validate_against_snapshot` checks the snapshot ID, cutoff, digest, and exact
    evidence records; consuming code must call it before using an event.
- Generated `schemas/data/market-event.v1.schema.json` through the existing data
    schema generator, including missing/stale schema regression tests.
- Implemented frozen `ExperimentRun` terminal receipts and `EvaluationResult`
    evidence. Runs pin strategy/snapshot references, code, engine, container,
    dependency lock, random seed, times, and output artifacts. Evaluations bind
    to the exact successful run digest and a versioned evaluation protocol.
- Added research schema generation and missing/stale checks for both contracts.
- Implemented frozen `PromotionDecision` receipts with review-bound human
    approval evidence, complete gate attestations, and exact policy/evaluation
    digest resolution. Approved receipts require an approved policy, comparable
    Qlib/LEAN evidence, and passing available numerical metrics.
- Extended the existing research generator with the promotion decision schema.
- Implemented frozen paper-only `OrderIntent` and default-deny `RiskDecision`
    contracts. Risk receipts bind the exact order and operator policy digests,
    require complete passing check evidence for `ALLOW`, and validate expiry.
- Added execution schema generation and missing/stale tests for both contracts.
- Added typed Qlib research, LEAN certification, and signal-evaluator ports with
    request/output validation wrappers. Tests use deterministic in-memory doubles,
    including invalid input rejection before adapter invocation.
- Recorded unresolved replay, revision, universe, runtime, and engine-comparison
    semantics in proposed ADR 0002; no tolerances or new approvals were selected.
- Implemented local as-of selection with inclusive observation cutoffs,
    explicit time-bound revision ordering, deterministic output, and fail-closed
    ambiguous/incomplete history handling. Original snapshots remain unchanged.
- Recorded the tested local selection semantics and remaining source-specific
    decisions in proposed ADR 0002.
- Implemented a read-only local SHA-256 artifact resolver with bounded reads,
    exact-byte verification, static link/reparse rejection, and no stale fallback.
    Verified as-of selection checks all visible raw revisions and ordering
    evidence while leaving future artifact files unread.
- Implemented immutable `UniverseMember` and `UniverseMembershipArtifact`
    contracts, with observation/effective-time selection and rejection of
    overlapping intervals. The resolver verifies artifact bytes, manifest ID,
    and horizon before returning effective, known members for a snapshot cutoff.
- Added the universe membership artifact to the existing data schema generator.
- Latest local checks on 2026-09-30: 330 tests passed; 2 actual symlink tests
    skipped because Windows link creation requires privileges unavailable here.
    The mocked reparse-attribute test, lint, format, types, schemas, and lock
    checks passed.
- Passed focused validation for the point-in-time contract and schema: Ruff,
    Pyright, 6 unit tests, and 2 schema contract tests on 2026-09-30.

## In Progress

- Actual engine and signal adapters, engine-specific input materialization,
    and point-in-time replay remain unimplemented. Local ports and receipts do
    not constitute an executable trading system or independent certification.
- Phase 0 ADRs for point-in-time and revision semantics, source/data rights,
    Azure service selection, and Qlib/LEAN comparison metrics and tolerances.

## Approval Gates

- Complete source-specific legal review before enabling collection.
- Restore Azure subscription access and validate region, SKU, quota, policy, and
  cost before infrastructure scaffolding; scaffold and deployment approvals are
  separate.
- Promotion remains subject to explicit human approval.

## Blockers

- Azure subscription discovery returns HTTP 403. Region, SKU, quota, policy,
  and cost validation are blocked until access is restored.
- Source-specific connectors remain gated on documented legal and license review.

## Next Executable Step

Combine verified record selection and universe membership selection into one
local per-decision input bundle with explicit snapshot, cutoff, and revision-order
provenance. Test that out-of-universe instrument records cannot enter that bundle
and define handling of market-wide records. Keep actual engines and sources gated.

## Known Boundaries

- As-of selection is local and not wired into an actual engine. `RevisionOrder`
    evidence and observation timestamps are caller-supplied claims. The resolver
    verifies evidence bytes, not their interpretation or source-specific
    completeness. Errors are raised
    without partial results; persistent quarantine is not implemented.
- Use `LocalArtifactResolver.select_verified_records_as_of` when byte integrity
    is required; the original pure selector remains filesystem-free. The resolver
    verifies raw payload digests, not normalized `content_hash` values or semantic
    agreement of an order with its report. Universe resolution is a separate call.
- The artifact root must be trusted and protected from concurrent untrusted
    writes. Static link checks are not a race-proof filesystem sandbox. The
    resolver has no write/install/fetch interface, and every read rechecks bytes;
    consumers must not later read the path without verification.
- The selector returns known records, not a new universe-validated snapshot.
    It does not filter by economic effective time or check stale data/source
    permissions. Callers must retain cutoff and revision-order evidence as well
    as the snapshot identity to reproduce selection.
- Adapter wrappers validate contracts and identity before/after invocation, not
    actual engine execution, metric accuracy, or adapter side effects. Their
    request/output models are in-process interfaces, not new persisted schemas.
- Tests use synthetic doubles only; Qlib and LEAN are not installed or invoked.
    A whole-snapshot cutoff does not prevent per-bar look-ahead inside an engine.
    Engine integration of as-of selection and a numerical comparison policy
    remain open.
- Order/risk objects are receipts, not broker capabilities. Call
    `decision.validate_for_intent(intent, policy, at=trusted_now)` with the current
    operator policy; this checks binding and a half-open validity interval, not
    authenticated assessor identity or the truth of the check reports.
- Runtime checks of positions, reserved cash, live quotes, portfolio limits,
    champion selection, historical universe, holidays, tick sizes, and kill switch
    are not implemented. `SELL` means reducing an existing long position; only
    an independent service using actual holdings can rule out a short sale.
- Session dates only enforce ordering, not the next KRX trading session or its
    opening time. Client order IDs are metadata; atomic replay prevention,
    resource reservations, and revalidation before submission remain required.
- Promotion decisions are records, not activation capabilities. Consumers must
    resolve run inputs and call `decision.validate_evidence(policy, evaluations,
    runs)`. No lifecycle, champion pointer, policy file, or broker state is changed.
- Gate reports and human approval artifacts remain supplied attestations. No
    signature, reviewer authority, report bytes, or append-only audit persistence
    is verified here. Paper session counts, walk-forward folds, realized slippage,
    hard-risk history, and dual-engine tolerances require independent report
    verification before any activation. Hash binding is not authentication.
- Research models describe completed runs and supplied measurements; they do
    not schedule jobs, execute Qlib/LEAN, calculate metrics, or enforce a sandbox.
    Call `ExperimentRun.validate_inputs` with the actual strategy and snapshot,
    then `EvaluationResult.validate_against_run` before consuming results.
- Evaluation protocol references must pin metric definitions, costs, and split
    methodology. Artifact bytes, statistical correctness, walk-forward evidence,
    multiple-testing correction, and dual-engine tolerances are not verified yet.
    Missing deflated-Sharpe confidence is represented as unknown, not as a pass.
- Research inputs currently use the exact snapshot and runtime pinned by the
    strategy. A protocol for alternative evaluation datasets/runtimes is deferred.
- Event construction checks structure and declared timing. Snapshot resolution
    is an explicit second check, not a registry lookup or authorization boundary.
- Event availability must not exceed the selected snapshot cutoff. A later
    extraction cannot be backdated into a historical signal. Offline retrospective
    analysis needs a separately documented protocol before use in evaluations.
- Rights labels are metadata, not proof of legal approval; runtime source-policy
    enforcement, authenticated approvals, and append-only persistence remain open.
- Snapshot membership references remain unchanged. `read_universe_membership`
    resolves their exact JSON bytes and checks ID/horizon; membership selection
    requires observed-at <= cutoff and effective-from <= cutoff < effective-until
    (an absent end is open). Empty/unseen membership never authorizes an instrument.
- Membership evidence artifacts, observation timestamps, source completeness,
    and ETF approval authority remain unverified claims. Interval corrections
    cannot overwrite history; overlapping declarations are rejected, not resolved.
    Callers must supply as-known interval declarations and separately verified
    evidence; a later-known removal cannot be inserted into an earlier declaration.
- Membership selection is not yet combined with record selection, engine replay,
    or order risk checks. Manifest JSON is an archival container, not proof that
    the whole file existed at each earlier replay timestamp.
- Snapshot digests identify serialized snapshots, including record ordering and
    creation metadata. Order-independent content-set hashing is not implemented.
- Event confidence is not a trade decision. Rumor-influence caps and independent
    pre-trade risk enforcement remain future signal/execution work.

## Evidence

- Master plan: `docs/plans/ats-master-plan.md`
- Core governance ADR: `docs/adr/0001-governed-recursive-improvement.md`
- Proposed adapter/replay ADR: `docs/adr/0002-adapter-boundaries-and-replay-semantics.md`
- Adapter boundaries: `src/ats/ports.py`
- Adapter regression tests: `tests/unit/domain/test_research.py`
- Strategy contract: `src/ats/domain/strategy.py`
- Strategy schema: `schemas/strategy/v1.schema.json`
- Point-in-time contract: `src/ats/domain/data.py`
- Local as-of selector: `src/ats/data/asof.py`
- Local artifact resolver: `src/ats/data/artifacts.py`
- Historical universe contract: `src/ats/domain/universe.py`
- Universe artifact schema: `schemas/data/universe-membership.v1.schema.json`
- Artifact integrity and selection tests: `tests/unit/data/test_artifacts.py`
- Selection and revision tests: `tests/unit/domain/test_data.py`
- Data snapshot schema: `schemas/data/snapshot.v1.schema.json`
- Market event schema: `schemas/data/market-event.v1.schema.json`
- Event and provenance tests: `tests/unit/domain/test_data.py`
- Data schema and generator tests: `tests/contract/test_data_snapshot_schema.py`
- Research contracts: `src/ats/domain/research.py`
- Research tests: `tests/unit/domain/test_research.py` and
    `tests/contract/test_research_schema.py`
- Research schemas: `schemas/research/experiment-run.v1.schema.json` and
    `schemas/research/evaluation-result.v1.schema.json`
- Promotion contract: `src/ats/domain/governance.py`
- Promotion schema: `schemas/research/promotion-decision.v1.schema.json`
- Promotion tests reuse `tests/unit/domain/test_research.py` and
    `tests/contract/test_research_schema.py`.
- Execution contracts: `src/ats/domain/execution.py`
- Execution tests: `tests/unit/domain/test_execution.py` and
    `tests/contract/test_execution_schema.py`
- Execution schemas: `schemas/execution/order-intent.v1.schema.json` and
    `schemas/execution/risk-decision.v1.schema.json`
- Operator policies: `config/source-allowlist.yaml`, `config/risk-policy.yaml`,
    and `config/promotion-policy.yaml`
- Validation: `git diff --check`, `uv lock --check`, Ruff lint/format, Pyright,
    all four schema generator checks (nine schemas), and `uv run pytest -q`
    (330 passed, 2 skipped for Windows symlink creation privileges)
- Point-in-time validation: focused Ruff/Pyright checks, 6 unit tests, and 2
    schema contract tests (2026-09-30)
