# Project Status

Last updated: 2026-09-30

## Active Milestone

Phase 1 data and research contracts; Phase 0 approval gates remain open.

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
- Passed the complete local quality gate with 145 tests on 2026-09-30.
- Passed focused validation for the point-in-time contract and schema: Ruff,
    Pyright, 6 unit tests, and 2 schema contract tests on 2026-09-30.

## In Progress

- Remaining Phase 1 contracts: `OrderIntent` and `RiskDecision`.
- Qlib, LEAN, and signal-evaluator adapter boundaries.
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

Implement paper-only `OrderIntent` and `RiskDecision` contracts. Bind every
decision to the exact intent and independent risk-policy version; missing or
inconsistent evidence must not authorize submission. Broker submission and the
runtime risk service remain separate later work.

## Known Boundaries

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
- Universe membership currently contains an external manifest reference and
    supplied digest, not its members or a computed manifest digest. Raw payload
    bytes and supplied digests are not verified by these contracts.
- Snapshot digests identify serialized snapshots, including record ordering and
    creation metadata. Order-independent content-set hashing is not implemented.
- Event confidence is not a trade decision. Rumor-influence caps and independent
    pre-trade risk enforcement remain future signal/execution work.

## Evidence

- Master plan: `docs/plans/ats-master-plan.md`
- Core governance ADR: `docs/adr/0001-governed-recursive-improvement.md`
- Strategy contract: `src/ats/domain/strategy.py`
- Strategy schema: `schemas/strategy/v1.schema.json`
- Point-in-time contract: `src/ats/domain/data.py`
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
- Operator policies: `config/source-allowlist.yaml`, `config/risk-policy.yaml`,
    and `config/promotion-policy.yaml`
- Validation: `git diff --check`, `uv lock --check`, Ruff lint/format, Pyright,
    all three schema generator checks (six schemas), and `uv run pytest -q`
    (145 passed)
- Point-in-time validation: focused Ruff/Pyright checks, 6 unit tests, and 2
    schema contract tests (2026-09-30)
