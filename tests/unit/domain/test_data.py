from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given, strategies
from pydantic import ValidationError

from ats.data import AsOfSelectionError, RevisionOrder, select_records_as_of
from ats.domain.data import (
    CredibilityTier,
    DataSnapshot,
    MarketEvent,
    MarketEventType,
    PointInTimeRecord,
    UniverseMembershipManifest,
)
from ats.domain.policy import RightsClass
from ats.domain.strategy import ArtifactRef, DatasetSnapshotRef

FREEZE_TIME = datetime(2026, 9, 30, 7, tzinfo=UTC)


def _digest(character: str) -> str:
    return f"sha256:{character * 64}"


def _record(
    *,
    source_item_id: str = "dart-001",
    revision: str = "rev-001",
    observed_at: datetime = FREEZE_TIME,
) -> PointInTimeRecord:
    return PointInTimeRecord(
        source_id="dart-disclosures",
        source_item_id=source_item_id,
        revision=revision,
        published_at=FREEZE_TIME - timedelta(minutes=5),
        observed_at=observed_at,
        effective_at=FREEZE_TIME,
        instrument_id="krx-005930",
        rights_class=RightsClass.APPROVED_PUBLIC,
        credibility_tier=CredibilityTier.PRIMARY,
        content_hash=_digest("a"),
        raw_payload_digest=_digest("b"),
    )


def _snapshot(*records: PointInTimeRecord) -> DataSnapshot:
    return DataSnapshot(
        snapshot_id="eod-20260930-v1",
        observed_through=FREEZE_TIME,
        created_at=FREEZE_TIME + timedelta(minutes=1),
        universe_membership=UniverseMembershipManifest(
            manifest_id="kospi200-20260930",
            as_of=FREEZE_TIME,
            digest=_digest("c"),
        ),
        records=records,
    )


def test_snapshot_is_frozen_and_digest_is_deterministic() -> None:
    snapshot = _snapshot(_record())
    rebuilt = DataSnapshot.model_validate_json(snapshot.model_dump_json())

    assert rebuilt == snapshot
    assert rebuilt.content_digest() == snapshot.content_digest()
    assert snapshot.content_digest().startswith("sha256:")

    with pytest.raises(ValidationError):
        snapshot.__setattr__("snapshot_id", "mutated-snapshot")


def test_snapshot_rejects_record_observed_after_freeze() -> None:
    future_record = _record(observed_at=FREEZE_TIME + timedelta(seconds=1))

    with pytest.raises(ValidationError, match="after snapshot freeze"):
        _snapshot(future_record)


def test_snapshot_rejects_duplicate_source_revision() -> None:
    with pytest.raises(ValidationError, match="unique source revisions"):
        _snapshot(_record(), _record())


def test_snapshot_accepts_multiple_revisions_of_one_source_item() -> None:
    snapshot = _snapshot(
        _record(revision="rev-001"),
        _record(revision="rev-002"),
    )

    assert len(snapshot.records) == 2


def test_snapshot_rejects_future_universe_manifest() -> None:
    with pytest.raises(ValidationError, match="universe manifest cannot be newer"):
        DataSnapshot(
            snapshot_id="eod-20260930-v1",
            observed_through=FREEZE_TIME,
            created_at=FREEZE_TIME + timedelta(minutes=1),
            universe_membership=UniverseMembershipManifest(
                manifest_id="kospi200-20261001",
                as_of=FREEZE_TIME + timedelta(seconds=1),
                digest=_digest("c"),
            ),
        )


def test_record_rejects_publication_after_observation() -> None:
    with pytest.raises(ValidationError, match="published_at cannot be later"):
        _record(observed_at=FREEZE_TIME - timedelta(minutes=10))


def _event_payload(snapshot: DataSnapshot) -> dict[str, object]:
    return {
        "event_id": "event-001",
        "event_type": MarketEventType.DISCLOSURE,
        "summary": "An issuer published an earnings disclosure.",
        "confidence": 0.8,
        "available_at": FREEZE_TIME,
        "producer": ArtifactRef(
            artifact_id="disclosure-normalizer", version="1.0.0", digest=_digest("d")
        ),
        "dataset_snapshot": DatasetSnapshotRef(
            snapshot_id=snapshot.snapshot_id,
            observed_through=snapshot.observed_through,
            digest=snapshot.content_digest(),
        ),
        "evidence": snapshot.records,
    }


def test_event_round_trip_and_exact_snapshot_provenance() -> None:
    snapshot = _snapshot(_record())
    event = MarketEvent.model_validate(_event_payload(snapshot))
    rebuilt = MarketEvent.model_validate_json(event.model_dump_json())

    assert rebuilt == event
    rebuilt.validate_against_snapshot(snapshot)
    with pytest.raises(ValidationError):
        event.__setattr__("summary", "Changed event")
    with pytest.raises(ValidationError):
        event.evidence[0].__setattr__("content_hash", _digest("f"))


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf")])
def test_event_rejects_invalid_confidence(confidence: float) -> None:
    payload = _event_payload(_snapshot(_record()))
    payload["confidence"] = confidence
    with pytest.raises(ValidationError):
        MarketEvent.model_validate(payload)


@pytest.mark.parametrize("offset", [-1, 1])
def test_event_rejects_invalid_availability(offset: int) -> None:
    payload = _event_payload(_snapshot(_record()))
    payload["available_at"] = FREEZE_TIME + timedelta(seconds=offset)
    with pytest.raises(ValidationError, match="available"):
        MarketEvent.model_validate(payload)


@pytest.mark.parametrize("evidence", [(), (_record(), _record())])
def test_event_rejects_missing_or_duplicate_evidence(
    evidence: tuple[PointInTimeRecord, ...],
) -> None:
    payload = _event_payload(_snapshot(_record()))
    payload["evidence"] = evidence
    with pytest.raises(ValidationError):
        MarketEvent.model_validate(payload)


def test_event_rejects_unreviewed_rights() -> None:
    record = PointInTimeRecord.model_validate(
        {**_record().model_dump(), "rights_class": RightsClass.PENDING_REVIEW}
    )
    with pytest.raises(ValidationError, match="resolved usage rights"):
        MarketEvent.model_validate(_event_payload(_snapshot(record)))


@pytest.mark.parametrize("field", ["content_hash", "raw_payload_digest", "revision"])
def test_event_rejects_tampered_evidence(field: str) -> None:
    snapshot = _snapshot(_record())
    value = "rev-999" if field == "revision" else _digest("f")
    record = PointInTimeRecord.model_validate({**_record().model_dump(), field: value})
    payload = _event_payload(snapshot)
    payload["evidence"] = (record,)
    event = MarketEvent.model_validate(payload)
    with pytest.raises(ValueError, match="does not match snapshot record"):
        event.validate_against_snapshot(snapshot)


def test_event_rejects_wrong_snapshot_reference() -> None:
    event = MarketEvent.model_validate(_event_payload(_snapshot(_record())))
    with pytest.raises(ValueError, match="snapshot reference"):
        event.validate_against_snapshot(_snapshot(_record(revision="rev-002")))


def test_event_is_not_an_order_contract() -> None:
    payload = _event_payload(_snapshot(_record()))
    payload["order_quantity"] = 100
    with pytest.raises(ValidationError, match="Extra inputs"):
        MarketEvent.model_validate(payload)


def _revision_order(
    revisions: tuple[str, ...],
    *,
    observed_at: datetime = FREEZE_TIME,
) -> RevisionOrder:
    return RevisionOrder(
        source_id="dart-disclosures",
        source_item_id="dart-001",
        revisions=revisions,
        observed_at=observed_at,
        evidence=ArtifactRef(
            artifact_id="revision-order", version="1.0.0", digest=_digest("d")
        ),
    )


def test_asof_includes_boundary_but_excludes_late_observations() -> None:
    early = _record(observed_at=FREEZE_TIME - timedelta(minutes=1))
    late = _record(revision="rev-002")
    snapshot = _snapshot(early, late)
    cutoff = early.observed_at
    assert select_records_as_of(snapshot, at=cutoff) == (early,)
    assert select_records_as_of(snapshot, at=cutoff - timedelta(microseconds=1)) == ()
    with pytest.raises(AsOfSelectionError, match="ambiguous"):
        select_records_as_of(snapshot, at=FREEZE_TIME)
    assert snapshot.records == (early, late)


def test_late_arriving_old_revision_does_not_replace_newer_source_revision() -> None:
    newer = _record(revision="rev-002", observed_at=FREEZE_TIME - timedelta(minutes=1))
    older = _record(revision="rev-010")
    order = _revision_order((older.revision, newer.revision))
    snapshot = _snapshot(newer, older)
    assert select_records_as_of(snapshot, at=FREEZE_TIME, revision_orders=(order,)) == (
        newer,
    )
    with pytest.raises(AsOfSelectionError, match="ambiguous"):
        select_records_as_of(snapshot, at=FREEZE_TIME)


def test_future_revision_order_cannot_resolve_historical_ambiguity() -> None:
    snapshot = _snapshot(_record(revision="rev-001"), _record(revision="rev-002"))
    future = _revision_order(
        ("rev-001", "rev-002"), observed_at=FREEZE_TIME + timedelta(seconds=1)
    )
    with pytest.raises(AsOfSelectionError, match="ambiguous"):
        select_records_as_of(snapshot, at=FREEZE_TIME, revision_orders=(future,))


@pytest.mark.parametrize(
    "revisions", [("rev-001", "rev-003"), ("rev-001", "rev-002", "rev-003")]
)
def test_revision_order_rejects_missing_or_unlisted_history(
    revisions: tuple[str, ...],
) -> None:
    snapshot = _snapshot(_record(), _record(revision="rev-002"))
    with pytest.raises(AsOfSelectionError, match="incomplete or unlisted"):
        select_records_as_of(
            snapshot, at=FREEZE_TIME, revision_orders=(_revision_order(revisions),)
        )


def test_known_revision_order_does_not_fall_back_to_an_older_visible_record() -> None:
    early = _record(observed_at=FREEZE_TIME - timedelta(minutes=1))
    latest = _record(revision="rev-002")
    order = _revision_order(("rev-001", "rev-002"), observed_at=early.observed_at)
    with pytest.raises(AsOfSelectionError, match="incomplete"):
        select_records_as_of(
            _snapshot(early, latest), at=early.observed_at, revision_orders=(order,)
        )
    with pytest.raises(AsOfSelectionError, match="incomplete"):
        select_records_as_of(_snapshot(), at=FREEZE_TIME, revision_orders=(order,))


@pytest.mark.parametrize("reversed_order", [False, True])
def test_multiple_visible_orders_are_rejected_even_if_identical(
    reversed_order: bool,
) -> None:
    first = _revision_order(("rev-001", "rev-002"))
    second = _revision_order(("rev-002", "rev-001")) if reversed_order else first
    with pytest.raises(AsOfSelectionError, match="multiple revision orders"):
        select_records_as_of(
            _snapshot(_record(), _record(revision="rev-002")),
            at=FREEZE_TIME,
            revision_orders=(first, second),
        )


def test_order_rejects_duplicate_labels_and_is_immutable() -> None:
    with pytest.raises(ValidationError, match="unique revisions"):
        _revision_order(("rev-001", "rev-001"))
    order = _revision_order(("rev-001", "rev-002"))
    with pytest.raises(ValidationError):
        order.__setattr__("revisions", ("rev-002", "rev-001"))


def test_asof_preserves_future_effective_announcements_and_missing_timestamps() -> None:
    announced = PointInTimeRecord.model_validate(
        {
            **_record().model_dump(),
            "effective_at": FREEZE_TIME + timedelta(days=30),
            "published_at": None,
        }
    )
    selected = select_records_as_of(_snapshot(announced), at=FREEZE_TIME)
    assert selected == (announced,)
    assert selected[0].published_at is None
    unknown = PointInTimeRecord.model_validate(
        {**announced.model_dump(), "effective_at": None}
    )
    assert select_records_as_of(_snapshot(unknown), at=FREEZE_TIME) == (unknown,)


@pytest.mark.parametrize(
    "at", [datetime(2026, 9, 30), FREEZE_TIME + timedelta(microseconds=1)]
)
def test_asof_rejects_naive_time_and_extrapolation(at: datetime) -> None:
    with pytest.raises(AsOfSelectionError):
        select_records_as_of(_snapshot(_record()), at=at)


def test_selection_is_timezone_equivalent_and_source_scoped() -> None:
    first = _record()
    other_source = PointInTimeRecord.model_validate(
        {**first.model_dump(), "source_id": "krx-primary"}
    )
    snapshot = _snapshot(other_source, first)
    local_time = FREEZE_TIME.astimezone(timezone(timedelta(hours=9)))
    assert select_records_as_of(snapshot, at=local_time) == (first, other_source)
    assert select_records_as_of(snapshot, at=FREEZE_TIME) == (first, other_source)


@given(strategies.permutations(("dart-003", "dart-001", "dart-002")))
def test_asof_output_is_independent_of_input_order(item_ids: list[str]) -> None:
    snapshot = _snapshot(*(_record(source_item_id=item_id) for item_id in item_ids))
    selected = select_records_as_of(snapshot, at=FREEZE_TIME)
    assert tuple(record.source_item_id for record in selected) == (
        "dart-001",
        "dart-002",
        "dart-003",
    )


@given(strategies.integers(min_value=1, max_value=59))
def test_future_observation_does_not_change_earlier_selection(seconds: int) -> None:
    early = _record(observed_at=FREEZE_TIME - timedelta(minutes=1))
    late = _record(
        revision="rev-002", observed_at=early.observed_at + timedelta(seconds=seconds)
    )
    baseline = select_records_as_of(_snapshot(early), at=early.observed_at)
    assert (
        select_records_as_of(_snapshot(early, late), at=early.observed_at) == baseline
    )


def test_asof_revalidates_unchecked_copies() -> None:
    snapshot = _snapshot(_record())
    duplicate = snapshot.model_copy(update={"records": (_record(), _record())})
    with pytest.raises(ValidationError, match="unique source revisions"):
        select_records_as_of(duplicate, at=FREEZE_TIME)
    invalid_order = _revision_order(("rev-001", "rev-002")).model_copy(
        update={"revisions": ("rev-001", "rev-001")}
    )
    with pytest.raises(ValidationError, match="unique revisions"):
        select_records_as_of(snapshot, at=FREEZE_TIME, revision_orders=(invalid_order,))
