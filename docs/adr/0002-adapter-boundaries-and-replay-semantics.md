# ADR 0002: Adapter Boundaries and Replay Semantics

- Status: Proposed
- Date: 2026-09-30
- Scope: Local research and signal interfaces; no deployment or trading approval

## Context

ADR 0001 selects Qlib research and independent LEAN certification. Domain
receipts now identify exact strategies, snapshots, runtimes, and results, but
there is no actual engine adapter, replay implementation, or comparison policy.
Accepting structurally valid receipts alone would not establish reproducibility.

## Implemented Boundary

`src/ats/ports.py` defines typed Qlib, LEAN, and signal ports plus validated
invocation functions. Requests bind the existing immutable strategy and snapshot.
Engine requests additionally pin engine artifact, dependency lock, seed,
evaluation protocol, period, and experiment ID. Signal output is an artifact
reference, never an order or a risk decision.

The invocation functions reconstruct inputs and outputs from model data to run
validation even when a caller used an unchecked model copy. Invalid inputs fail
before dispatch. Mismatched output identity, runtime metadata, protocol, period,
or timestamps fails before results reach a consumer. Failed runs carry no
evaluation result, and adapter exceptions propagate without stale-result fallback.

These are in-process interfaces, not persisted or wire-format domain contracts.
They do not install engines, resolve artifact bytes, calculate signals or metrics,
authorize champions, isolate arbitrary code, or prevent side effects in an adapter.
The test doubles demonstrate contract conformance, not actual engine equivalence.

## Decisions Required Before Real Adapters

1. **Point-in-time replay:** A snapshot's final cutoff is not a per-bar
   information cutoff. The local selector below filters each decision timestamp;
   engine-specific replay still must use it without exposing later records.
2. **Revision identity:** Revisions are currently opaque identifiers, not ordered
   numbers. The local selector requires explicit precedence for multiple known
   revisions. Source-specific proof of precedence and persistent quarantine
   remain required before real-source use.
3. **Publication and effective time:** Preserve all original timestamps. A known
   announcement can have a future effective date; observation availability and
   economic applicability must not be conflated. Missing source timestamps must
   not be invented.
4. **Universe provenance:** Local artifact resolution now verifies bytes and
   declared membership observation/effective times. Authenticate source evidence
   and interval correction history before real use; an old `as_of` value alone
   does not prove historical availability.
5. **Derived-event availability:** Define whether retrospective extraction is
   allowed in a separate research protocol. It must not be presented as an event
   available to an earlier operational signal.
6. **Daily inputs and engine environments:** Current requests must use the
   strategy's pinned snapshot and runtime. Specify how an immutable champion
   consumes later daily snapshots and how different engine containers are bound
   to the same candidate without mutating the champion.
7. **Execution equivalence:** Pin signal streams, trading calendars, corporate
   actions, prices, costs, liquidity constraints, and rounding rules. Define
   exact-match discrete order fields and versioned numerical tolerances for
   continuous return/price comparisons. No numerical tolerances are approved here.
8. **Isolation:** Keep generated code behind a separately enforced sandbox. A
   Python protocol and validation wrapper do not limit filesystem, network, or
   credential access by an adapter implementation.

## Implemented Local Selection Semantics

`ats.data.select_records_as_of` implements a local, conservative baseline tested
with synthetic fixtures. This does not approve any connector or engine protocol.

- Require an aware decision timestamp no later than the frozen snapshot cutoff.
   Compare observation instants in UTC with an inclusive boundary.
- Preserve only records observed at or before that instant. Neither publication
   nor effective timestamps substitute for observation time. Known announcements
   with future effective dates remain available; applicability is a separate task.
- Group by `(source_id, source_item_id)`. One visible revision needs no ordering
   claim. Multiple visible revisions require one explicit `RevisionOrder` listing
   oldest to newest, with an evidence artifact and its own observation timestamp.
- Ignore ordering claims first observed after the decision timestamp. Do not
   infer precedence from lexical revision labels or later data arrival.
- An applicable order must list exactly the visible revisions for that item.
   Missing declared revisions, unlisted revisions, or multiple applicable orders
   cause `AsOfSelectionError`, with no partial result or older-record fallback.
- Return an immutable tuple sorted by source/item. Leave snapshot records,
   revision history, original timestamps, and snapshot digest unchanged.
- Revalidate supplied models, retaining the snapshot's strict duplicate-revision
   rejection. Ingestion deduplication and persistent quarantine are not implemented.

`RevisionOrder` is in-process selection metadata, not a new persisted schema.
The selector does not authenticate its evidence, check source rights/freshness,
verify universe membership, infer absent history, or select economic applicability.
The caller must retain snapshot identity, cutoff, and ordering evidence for replay.
Different applicable ordering claims can legitimately produce different outputs;
snapshot identity alone does not identify the complete selection inputs.

## Proposed Next Slice

The synthetic daily-price-to-report workflow is implemented; see
`docs/plans/predeployment.md` for its exact limitations and remaining milestones.
Resolve source usage rights and real fixture provenance before source-specific
integration. Actual engine certification, KIS paper execution, and authenticated
operations remain unimplemented. No numerical certification tolerances, source
approvals, or deployment permissions are implied by the synthetic smoke run.

## Implemented Local Artifact Verification

`LocalArtifactResolver` reads `root/sha256/<64 lowercase hex characters>` under
a trusted configured root. It accepts digest references, not caller-supplied
artifact paths or URLs. Reads are bounded (16 MiB by default), must be regular
files, and reject static symlinks/reparse points beneath the root. The resolver
returns immutable bytes only after exact SHA-256 verification; it never fetches,
repairs, overwrites, or installs artifact content and has no read cache. Explicit
typed readers may decode bytes only after digest verification.

`select_verified_records_as_of` first resolves selection, then verifies raw bytes
for every visible revision (including superseded ones) and all applicable
revision-order evidence. Missing or mismatched visible bytes fail without partial
results. Later observations and later ordering evidence cause no filesystem reads
at earlier cutoffs. The original pure selector remains available without I/O.

Integrity is not authenticity: the code does not prove that a report supports a
declared order, that timestamps are truthful, or that collection was licensed.
Normalized content hashes and universe semantics are not checked by raw-byte
verification. A trusted read-only directory is required; these portable checks
do not defend against hostile concurrent path replacement or provide a sandbox.
Use returned verified bytes, or reverify on every subsequent read.

## Implemented Historical Universe Artifact

The existing `UniverseMembershipManifest` remains a digest reference. Its payload
is now defined by `UniverseMembershipArtifact` with a matching manifest ID,
KRX market, as-of horizon, and immutable `UniverseMember` declarations. Each member
pins instrument, equity/ETF class, KOSPI 200/ETF-allowlist basis, observation time,
effective interval, and evidence reference.

`read_universe_membership` verifies the exact JSON bytes and reference ID/horizon.
`select_universe_members_as_of` also enforces the snapshot cutoff and returns
members observed by the requested instant and effective in `[from, until)`;
an absent end is open. Cutoffs beyond the manifest horizon fail. Known future
additions and late-observed historical memberships do not enter earlier selections.
Results are sorted by instrument; adjacent intervals are allowed, but duplicate
or overlapping intervals for an instrument fail closed, regardless of input order.

These are supplied declarations, not authenticated exchange or operator evidence.
The artifact is archival: it can contain later observations which selection omits.
Every interval endpoint must be known at its declared observation time. Encoding
a later correction into an earlier declaration is invalid provenance; correction
reconciliation and source completeness still require a separate protocol. Evidence
references are retained but not recursively resolved or interpreted here. Empty
or absent membership does not prove universal exclusion or permission to trade.
No changes are made to engines, record selection, risk, or broker execution.

## Implemented Per-Decision Bundle

`ats.data.build_decision_inputs` composes verified record selection and known,
effective universe membership into a frozen `DecisionInputBundle`. It retains
snapshot ID/cutoff/digest, a UTC decision cutoff, universe reference, selected
membership declarations, all applicable revision-order claims, and scope policy.

Only instrument records whose IDs belong to the selected membership are admitted.
Other selected instrument records are recorded as `OUTSIDE_UNIVERSE` exclusions.
`instrument_id=None` does not distinguish market-wide information from unresolved
symbol mapping: it defaults to `UNSCOPED` exclusion. Callers may explicitly use
`UnscopedRecordPolicy.INCLUDE`; this choice is included in the bundle digest.
Exclusions retain source/item/revision identifiers but no raw payload content.

All visible raw revisions and applicable ordering evidence are verified before
scope filtering, including excluded items and superseded revisions. Thus corrupt
data cannot be hidden by filtering. Future raw observations/order evidence are
not read. Errors return no partial bundle; the original snapshot is unchanged.

The builder sorts through the existing selectors; repeat builds of the same
inputs and timezone-equivalent cutoffs produce the same bundle digest. Snapshot
identity still includes its original ordering and creation metadata. Future
orders are not part of the applied provenance, whereas the snapshot digest still
binds its entire archive. The bundle is an in-process receipt, not a new persisted
schema, proof of authentic source claims, or an engine sandbox. Direct model
construction is not evidence that the resolver ran. Semantic content validation
and actual engine integration remain separate work. Explicit local freshness
requirements are described below.

## Implemented Source Eligibility Gate

`build_decision_inputs` now requires `source_policy: SourceAllowlist`. It
revalidates that policy and requires `APPROVED` with approval time no later than
the decision cutoff, including when the input is empty. Before any artifact read,
every visible raw revision must reference a listed, enabled, legally approved
source with resolved rights, and its rights classification must match exactly.
Applicable revision-order claims also require eligible source IDs. Superseded
and scope-excluded records receive the same checks. Future observations and
future ordering claims are not evaluated for an earlier cutoff.

Failure raises `SourceEligibilityError` (or model validation errors for malformed
policies) with no partial bundle and no stale fallback. The bundle stores a
`PolicyRef` with ID, version, and digest of the complete validated policy.
`bundle.validate_source_policy(policy)` detects policy drift and rechecks the
sources represented by the receipt. Its exclusions retain identifiers, not raw
rights metadata; full-history validation still requires the original builder.

This is validation of a supplied policy, not identity authentication or a trusted
policy registry. Callers must select the correct operator-approved policy.
Approval signatures, revocation, collection-time permission, retention, freshness,
and rate enforcement are not implemented. Universe evidence has no source ID and
is not authorized by this gate. Low-level resolver/selection APIs remain policy-free.
No source files were approved and no connector was enabled; tests use synthetic
policy objects. The repository source allowlist remains `DRAFT` and is rejected.

## Implemented Availability and Freshness

`SourceDataRequirement` declares a unique requirement ID, source ID, optional
source item and instrument filters, positive minimum record count, explicit
`OBSERVED`, `PUBLISHED`, or `EFFECTIVE` age basis, and nonnegative maximum age
in seconds. There are no inferred production thresholds. Null filters mean no
filter; they are not a request for unscoped-only records.

The builder validates requirement sources before artifact I/O. After revision
and universe selection, it requires enough admitted matching records and checks
every matching record, not just the newest one. Missing records, missing chosen
timestamps, future timestamps, or excessive age fail without a partial bundle or
fallback. Age is calculated in UTC with an inclusive limit; observation age and
economic age can be independently required using separate requirements.

Rules are stored in the bundle and its digest and rerun on receipt validation.
`data_requirements=()` means no availability/freshness assertion, not a passing
production gate. Callers must pin trusted requirements; they are not yet signed
operator policies. Excluded/superseded records cannot satisfy availability, but
their existing source-eligibility and byte-integrity checks remain unchanged.
These checks do not prove dataset completeness, source timestamp truth, source
heartbeat health, or per-trading-session freshness. Effective time is not an
automatic substitute for an economic observation timestamp.

## Implemented Local Multi-Cutoff Replay

`replay_inputs` takes a frozen `InputReplayRequest` containing a snapshot, source
policy, explicit requirement tuple, revision orders, unscoped policy, and ordered
cutoffs. It revalidates even unchecked model copies, normalizes cutoffs to UTC,
rejects duplicate/reversed/out-of-horizon schedules, and requires the source policy
to be approved by the first cutoff before reading any artifacts.

Each cutoff uses `build_decision_inputs` unchanged. Earlier cutoffs do not see
later raw observations or revision claims. The result records a digest of the
entire request and all ordered bundles; all share the pinned input provenance.
`validate_against_request` verifies the full cutoff sequence, request digest,
snapshot, source policy, universe reference, requirements, and applicable orders.

The first data/policy/I/O failure raises `InputReplayError` with a zero-based
index, UTC cutoff, and original exception as its cause. No prior bundles are
returned. Unexpected exceptions also propagate; there is no fallback, retry,
incremental publication, or automatic quarantine. This all-or-error API is not a
filesystem transaction. The trusted archive must still resist concurrent writes.

These request/result types are in-process models, not new persisted schemas.
The result does not include the full raw snapshot; its request digest references
that archive and includes future claims present in the request. A result checksum
is a reproducibility aid, not a signature or proof that a real engine ran.
One policy and requirement set are pinned for the whole replay. Policy/snapshot
changes, incremental updates, metric calculation, and actual trading remain out
of scope. Synthetic fixture thresholds do not approve operational settings.

## Acceptance Evidence

- Local replay tests in `tests/unit/data/test_artifacts.py` exercise late revisions,
  new memberships, stale/missing/corrupt data, schedule validation, deterministic
  rebuilds, and incomplete/mismatched result receipts.

- `tests/unit/domain/test_research.py`: deterministic typed doubles, pre-dispatch
  input rejection, exact output binding, failed runs, exception propagation,
  unchecked-copy revalidation, and signal/order separation.
- `tests/unit/domain/test_data.py`: cutoff boundaries, late-arriving older
   revisions, unavailable ordering evidence, incomplete/conflicting histories,
   UTC-equivalent cutoffs, and property tests for input-order independence and
   exclusion of future observations.
- `tests/unit/data/test_artifacts.py`: exact binary reads, digest/path rejection,
  missing/corrupt artifacts, bounded reads, visible-history verification, and
  future-artifact exclusion. Actual symlink tests require OS privileges and were
  skipped on this Windows host; simulated reparse rejection was tested.
- Real Qlib/LEAN performance, data leakage resistance during replay, and numerical
  agreement require separate integration evidence and are not yet validated.
- Universe tests in `tests/unit/data/test_artifacts.py` cover observed/effective
   boundaries, duplicate/overlapping intervals, JSON round trips, UTC-equivalent
   cutoffs, byte corruption, reference mismatch, and horizon extrapolation.
- Source eligibility tests in the same file cover pre-I/O denial, inactive and
   unlisted sources, rights mismatch, superseded revisions, future observations,
   exact policy digest binding, and rejection of the unchanged repository draft.