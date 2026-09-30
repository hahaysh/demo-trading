from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

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
