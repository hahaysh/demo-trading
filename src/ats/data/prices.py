"""Normalize the local daily-price JSON fixture format, not a broker API format."""

from ats.data.artifacts import LocalArtifactResolver
from ats.domain.data import PointInTimeRecord
from ats.domain.prices import DailyPrice


def normalize_daily_price(
    resolver: LocalArtifactResolver, record: PointInTimeRecord
) -> DailyPrice:
    record = PointInTimeRecord.model_validate(record.model_dump())
    price = DailyPrice.model_validate_json(resolver.read_raw_payload(record))
    if price.instrument_id != record.instrument_id:
        raise ValueError("daily price instrument does not match provenance")
    if record.effective_at is None or record.effective_at != price.session_close:
        raise ValueError("daily price session close does not match effective timestamp")
    if price.session_close > record.observed_at:
        raise ValueError("daily price cannot be observed before session close")
    if price.content_digest() != record.content_hash:
        raise ValueError("normalized daily price content hash mismatch")
    return price
