"""Offline collection planning and calendar-bound KIS ingestion; no scheduler daemon."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from ats.data.kis import KisDailyRequest, KisQuoteReceipt
from ats.data.prices import KisDailyNormalization, normalize_kis_daily_prices
from ats.data.storage import LocalPayloadStore, StoragePermit, StoredPayload
from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.domain.prices import SEOUL
from ats.domain.strategy import ArtifactRef, FrozenModel


class CollectionError(ValueError):
    """Collection inputs are incomplete, outside budget, or require review."""


class CalendarSession(FrozenModel):
    session: date
    close: AwareDatetime

    @model_validator(mode="after")
    def same_korean_date(self) -> Self:
        if self.close.astimezone(SEOUL).date() != self.session:
            raise ValueError("session close must match the Korean calendar date")
        return self


class CalendarWindow(FrozenModel):
    start: date
    end: date
    known_at: AwareDatetime
    evidence: ArtifactRef
    sessions: tuple[CalendarSession, ...]

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        dates = [entry.session for entry in self.sessions]
        if (
            self.start > self.end
            or dates != sorted(set(dates))
            or any(not self.start <= session <= self.end for session in dates)
        ):
            raise ValueError("calendar sessions must be unique, ordered and in range")
        return self


class StoredKisBatch(FrozenModel):
    raw: StoredPayload
    normalization: KisDailyNormalization
    calendar: CalendarWindow
    matches_supplied_calendar: Literal[True] = True
    source_and_calendar_authenticated: Literal[False] = False
    certified: Literal[False] = False

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if (
            self.raw.source_id != "kis-market"
            or self.raw.digest != self.normalization.raw_payload_digest
            or self.raw.policy_digest != self.normalization.policy_digest
            or self.raw.observed_at != self.normalization.observed_at
        ):
            raise ValueError("stored raw and normalization provenance must match")
        return self


def plan_daily_requests(
    symbols: tuple[str, ...],
    *,
    start: date,
    end: date,
    at: datetime,
    max_requests: int,
) -> tuple[KisDailyRequest, ...]:
    if at.tzinfo is None or at.utcoffset() is None:
        raise CollectionError("planning cutoff must be timezone-aware")
    if (
        start > end
        or end >= at.astimezone(SEOUL).date()
        or not symbols
        or len(set(symbols)) != len(symbols)
        or type(max_requests) is not int
        or max_requests < 1
    ):
        raise CollectionError("explicit unique symbols, past range and budget required")
    chunks = ((end - start).days // 100) + 1
    if len(symbols) * chunks > max_requests:
        raise CollectionError("collection exceeds request budget; no partial plan")
    requests: list[KisDailyRequest] = []
    for symbol in sorted(symbols):
        cursor = start
        while cursor <= end:
            last = min(end, cursor + timedelta(days=99))
            requests.append(KisDailyRequest(symbol=symbol, start=cursor, end=last))
            cursor = last + timedelta(days=1)
    return tuple(requests)


def pending_daily_run(
    *, now: datetime, local_time: time, last_completed_at: datetime | None
) -> datetime | None:
    if now.tzinfo is None or now.utcoffset() is None or local_time.tzinfo is not None:
        raise CollectionError("aware now and naive Korean wall-clock time required")
    if last_completed_at is not None and (
        last_completed_at.tzinfo is None
        or last_completed_at.utcoffset() is None
        or last_completed_at > now
    ):
        raise CollectionError("invalid completed-run timestamp")
    local_now = now.astimezone(SEOUL)
    due = datetime.combine(local_now.date(), local_time, tzinfo=SEOUL)
    if due > local_now:
        due -= timedelta(days=1)
    due = due.astimezone(UTC)
    return None if last_completed_at is not None and last_completed_at >= due else due


def validate_kis_calendar(
    receipt: KisQuoteReceipt, calendar: CalendarWindow
) -> KisDailyNormalization:
    calendar = CalendarWindow.model_validate(calendar.model_dump())
    if (
        receipt.observed_at.tzinfo is None
        or receipt.observed_at.utcoffset() is None
        or calendar.known_at > receipt.observed_at
        or calendar.start > receipt.request.start
        or calendar.end < receipt.request.end
    ):
        raise CollectionError("calendar is unknown at observation or lacks range")
    expected = {
        entry.session: entry.close
        for entry in calendar.sessions
        if receipt.request.start <= entry.session <= receipt.request.end
    }
    result = normalize_kis_daily_prices(receipt, session_closes=expected)
    actual = {bar.price.session for bar in result.bars}
    if actual != set(expected):
        raise CollectionError("missing or unexpected sessions; coverage not accepted")
    if any(bar.requires_review for bar in result.bars):
        raise CollectionError("corporate action or trading activity requires review")
    return result


def ingest_kis_daily(
    receipt: KisQuoteReceipt,
    *,
    calendar: CalendarWindow,
    store: LocalPayloadStore,
    source_policy: SourceAllowlist,
    permit: StoragePermit,
    now: datetime,
) -> StoredKisBatch:
    if (
        receipt.policy_digest != evidence_digest(source_policy)
        or permit.source_id != receipt.source_id
    ):
        raise CollectionError("collection policy provenance mismatch")
    normalized = validate_kis_calendar(receipt, calendar)
    raw = store.put(
        receipt.raw_payload,
        observed_at=receipt.observed_at,
        now=now,
        source_policy=source_policy,
        permit=permit,
    )
    if raw.observed_at != receipt.observed_at:
        raise CollectionError(
            "duplicate raw payload cannot be relabeled as a new observation"
        )
    return StoredKisBatch(raw=raw, normalization=normalized, calendar=calendar)


def archive_kis_batch(
    batch: StoredKisBatch,
    *,
    store: LocalPayloadStore,
    source_policy: SourceAllowlist,
    permit: StoragePermit,
    now: datetime,
) -> StoredPayload:
    batch = StoredKisBatch.model_validate(batch.model_dump())
    _revalidate_stored_batch(
        batch, store=store, source_policy=source_policy, permit=permit, now=now
    )
    return store.put(
        batch.model_dump_json().encode("utf-8"),
        observed_at=batch.raw.observed_at,
        now=now,
        source_policy=source_policy,
        permit=permit,
    )


def _revalidate_stored_batch(
    batch: StoredKisBatch,
    *,
    store: LocalPayloadStore,
    source_policy: SourceAllowlist,
    permit: StoragePermit,
    now: datetime,
) -> None:
    payload = store.read(batch.raw, now=now, source_policy=source_policy, permit=permit)
    receipt = KisQuoteReceipt(
        request=batch.normalization.request,
        observed_at=batch.raw.observed_at,
        policy_digest=batch.raw.policy_digest,
        raw_payload=payload,
        row_count=len(batch.normalization.bars),
    )
    if validate_kis_calendar(receipt, batch.calendar) != batch.normalization:
        raise CollectionError("stored normalization differs from raw payload")


def restore_kis_batch(
    archive: StoredPayload,
    *,
    store: LocalPayloadStore,
    source_policy: SourceAllowlist,
    permit: StoragePermit,
    now: datetime,
) -> StoredKisBatch:
    encoded = store.read(archive, now=now, source_policy=source_policy, permit=permit)
    batch = StoredKisBatch.model_validate_json(encoded)
    if (
        archive.observed_at != batch.raw.observed_at
        or archive.expires_at != batch.raw.expires_at
    ):
        raise CollectionError("archive cannot extend raw observation or retention")
    _revalidate_stored_batch(
        batch, store=store, source_policy=source_policy, permit=permit, now=now
    )
    return batch
