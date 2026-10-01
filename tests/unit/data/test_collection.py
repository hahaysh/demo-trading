import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from ats.data.collection import (
    CalendarSession,
    CalendarWindow,
    CollectionError,
    StoredKisBatch,
    archive_kis_batch,
    ingest_kis_daily,
    pending_daily_run,
    plan_daily_requests,
    restore_kis_batch,
    validate_kis_calendar,
)
from ats.data.kis import KisDailyRequest, KisQuoteReceipt
from ats.data.storage import LocalPayloadStore, StorageError, StoragePermit
from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.domain.strategy import ArtifactRef

NOW = datetime(2026, 10, 1, tzinfo=UTC)
SESSION = date(2026, 9, 30)
CLOSE = datetime(2026, 9, 30, 6, 30, tzinfo=UTC)


def _calendar() -> CalendarWindow:
    return CalendarWindow(
        start=SESSION,
        end=SESSION,
        known_at=CLOSE,
        evidence=ArtifactRef(
            artifact_id="fabricated-calendar", version="1", digest="sha256:" + "c" * 64
        ),
        sessions=(CalendarSession(session=SESSION, close=CLOSE),),
    )


def _receipt(*, empty: bool = False, review: bool = False) -> KisQuoteReceipt:
    rows = (
        []
        if empty
        else [
            {
                "stck_bsop_date": "20260930",
                "stck_oprc": "100",
                "stck_hgpr": "110",
                "stck_lwpr": "90",
                "stck_clpr": "105",
                "acml_vol": "1000",
                "flng_cls_code": "03" if review else "00",
                "prtt_rate": "0",
                "revl_issu_reas": "00",
                "mod_yn": "N",
            }
        ]
    )
    return KisQuoteReceipt(
        request=KisDailyRequest(symbol="005930", start=SESSION, end=SESSION),
        observed_at=NOW,
        policy_digest="sha256:" + "a" * 64,
        raw_payload=json.dumps(
            {"rt_cd": "0", "output1": {"stck_shrn_iscd": "005930"}, "output2": rows}
        ).encode(),
        row_count=len(rows),
    )


def test_plan_is_bounded_deterministic_and_gapless() -> None:
    start = date(2026, 1, 1)
    requests = plan_daily_requests(
        ("005930", "000660"), start=start, end=SESSION, at=NOW, max_requests=6
    )
    assert len(requests) == 6
    assert requests[0].symbol == "000660"
    assert requests[0].start == start
    assert requests[2].end == SESSION
    assert requests[1].start == requests[0].end + timedelta(days=1)
    assert all((request.end - request.start).days < 100 for request in requests)
    with pytest.raises(CollectionError, match="budget"):
        plan_daily_requests(
            ("005930", "000660"), start=start, end=SESSION, at=NOW, max_requests=5
        )


@pytest.mark.parametrize("symbols", [(), ("005930", "005930")])
def test_plan_rejects_missing_or_duplicate_symbols(symbols: tuple[str, ...]) -> None:
    with pytest.raises(CollectionError):
        plan_daily_requests(symbols, start=SESSION, end=SESSION, at=NOW, max_requests=1)


def test_schedule_coalesces_missed_runs_and_never_schedules_in_future() -> None:
    due = pending_daily_run(now=NOW, local_time=time(6), last_completed_at=None)
    assert due == datetime(2026, 9, 30, 21, tzinfo=UTC)
    assert pending_daily_run(now=NOW, local_time=time(6), last_completed_at=due) is None
    with pytest.raises(CollectionError):
        pending_daily_run(
            now=NOW, local_time=time(6), last_completed_at=NOW + timedelta(seconds=1)
        )


def test_calendar_match_preserves_observation_and_does_not_claim_certification() -> (
    None
):
    result = validate_kis_calendar(_receipt(), _calendar())
    assert result.observed_at == NOW
    assert result.bars[0].price.session_close == CLOSE
    assert result.coverage_verified is False


@pytest.mark.parametrize("empty,review", [(True, False), (False, True)])
def test_missing_sessions_and_corporate_actions_are_not_accepted(
    empty: bool, review: bool
) -> None:
    with pytest.raises(CollectionError):
        validate_kis_calendar(_receipt(empty=empty, review=review), _calendar())


def test_future_calendar_is_rejected() -> None:
    with pytest.raises(CollectionError):
        validate_kis_calendar(
            _receipt(),
            _calendar().model_copy(update={"known_at": NOW + timedelta(seconds=1)}),
        )


def test_ingestion_requires_separate_storage_permission(tmp_path: Path) -> None:
    policy = SourceAllowlist.model_validate(
        {
            "metadata": {"policy_id": "fixture-source", "version": "1"},
            "sources": [],
        }
    )
    permit = StoragePermit.model_validate(
        {
            "metadata": {"policy_id": "fixture-storage", "version": "1"},
            "source_id": "kis-market",
            "source_policy_digest": evidence_digest(policy),
            "retention_days": 1,
            "expires_at": NOW + timedelta(days=1),
        }
    )
    database = tmp_path / "absent.sqlite3"
    with pytest.raises(StorageError):
        ingest_kis_daily(
            replace(_receipt(), policy_digest=evidence_digest(policy)),
            calendar=_calendar(),
            store=LocalPayloadStore(database),
            source_policy=policy,
            permit=permit,
            now=NOW,
        )
    assert not database.exists()


def test_validated_synthetic_batch_is_stored_with_observation_and_expiry(
    tmp_path: Path,
) -> None:
    metadata = {
        "policy_id": "fixture-only",
        "version": "1",
        "status": "APPROVED",
        "approved_by": "synthetic-reviewer",
        "approved_at": NOW - timedelta(days=1),
    }
    policy = SourceAllowlist.model_validate(
        {
            "metadata": metadata,
            "sources": [
                {
                    "source_id": "kis-market",
                    "category": "MARKET",
                    "enabled": True,
                    "legal_review": "APPROVED",
                    "rights": {"classification": "LICENSED", "retention_days": 1},
                    "rate_limit_per_minute": 1,
                    "notes": "Synthetic transport fixture only.",
                }
            ],
        }
    )
    permit = StoragePermit.model_validate(
        {
            "metadata": metadata,
            "source_id": "kis-market",
            "allow_persistence": True,
            "source_policy_digest": evidence_digest(policy),
            "retention_days": 1,
            "expires_at": NOW + timedelta(days=1),
        }
    )
    receipt = replace(_receipt(), policy_digest=evidence_digest(policy))
    store = LocalPayloadStore(tmp_path / "synthetic.sqlite3")
    batch = ingest_kis_daily(
        receipt,
        calendar=_calendar(),
        store=store,
        source_policy=policy,
        permit=permit,
        now=NOW,
    )
    assert batch.raw.observed_at == batch.normalization.observed_at == NOW
    assert batch.raw.expires_at == NOW + timedelta(days=1)
    assert batch.raw.digest == batch.normalization.raw_payload_digest
    archive = archive_kis_batch(
        batch, store=store, source_policy=policy, permit=permit, now=NOW
    )
    altered = batch.model_dump(mode="json")
    altered["normalization"]["bars"][0]["price"]["close"] = "106"
    with pytest.raises(ValueError):
        archive_kis_batch(
            StoredKisBatch.model_validate(altered), store=store,
            source_policy=policy, permit=permit, now=NOW,
        )
    assert archive.expires_at == batch.raw.expires_at
    assert restore_kis_batch(
        archive, store=LocalPayloadStore(store.database),
        source_policy=policy, permit=permit, now=NOW,
    ) == batch
    assert batch.matches_supplied_calendar
    assert not batch.source_and_calendar_authenticated
    assert not batch.certified
    assert (
        store.read(batch.raw, now=NOW, source_policy=policy, permit=permit)
        == receipt.raw_payload
    )
    assert (
        ingest_kis_daily(
            receipt,
            calendar=_calendar(),
            store=LocalPayloadStore(store.database),
            source_policy=policy,
            permit=permit,
            now=NOW,
        )
        == batch
    )
    with pytest.raises(CollectionError, match="new observation"):
        ingest_kis_daily(
            replace(receipt, observed_at=NOW + timedelta(seconds=1)),
            calendar=_calendar(),
            store=store,
            source_policy=policy,
            permit=permit,
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(StorageError):
        restore_kis_batch(
            archive, store=store, source_policy=policy, permit=permit,
            now=archive.expires_at,
        )
    assert store.purge_expired(now=archive.expires_at) == 2
