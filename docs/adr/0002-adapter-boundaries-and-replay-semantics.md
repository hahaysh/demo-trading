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
4. **Universe provenance:** The current manifest is a reference with a supplied
   digest. Verify membership bytes and when that membership was known; an old
   `as_of` value alone does not prove historical availability.
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

Verify content-addressed raw payloads and revision-order evidence against actual
local bytes, using synthetic/recorded fixtures. Review source-specific ordering
and completeness rules before real connectors or engine replay. Source rights,
independent risk, human promotion, and cloud approval gates remain intact.

## Acceptance Evidence

- `tests/unit/domain/test_research.py`: deterministic typed doubles, pre-dispatch
  input rejection, exact output binding, failed runs, exception propagation,
  unchecked-copy revalidation, and signal/order separation.
- `tests/unit/domain/test_data.py`: cutoff boundaries, late-arriving older
   revisions, unavailable ordering evidence, incomplete/conflicting histories,
   UTC-equivalent cutoffs, and property tests for input-order independence and
   exclusion of future observations.
- Real Qlib/LEAN performance, data leakage resistance during replay, and numerical
  agreement require separate integration evidence and are not yet validated.