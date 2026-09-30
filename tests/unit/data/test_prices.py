import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ats.data.artifacts import ArtifactResolutionError, LocalArtifactResolver
from ats.data.prices import normalize_daily_price
from ats.domain.data import PointInTimeRecord
from ats.domain.prices import DailyPrice

CLOSE = datetime(2026, 9, 30, 6, 30, tzinfo=UTC)


def _price(**changes: object) -> DailyPrice:
    return DailyPrice.model_validate(
        {
            "instrument_id": "krx-005930",
            "session": "2026-09-30",
            "session_close": CLOSE,
            "open": "100",
            "high": "110",
            "low": "90",
            "close": "105",
            "volume": 1000,
            **changes,
        }
    )


def _stored(root: Path, price: DailyPrice) -> PointInTimeRecord:
    payload = price.model_dump_json().encode()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    path = root / "sha256" / digest[7:]
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(payload)
    return PointInTimeRecord.model_validate(
        {
            "source_id": "synthetic-prices",
            "source_item_id": "price-20260930",
            "revision": "rev-001",
            "instrument_id": price.instrument_id,
            "observed_at": CLOSE,
            "effective_at": CLOSE,
            "rights_class": "APPROVED_PUBLIC",
            "credibility_tier": "PRIMARY",
            "content_hash": price.content_digest(),
            "raw_payload_digest": digest,
        }
    )


def test_normalize_exact_bytes_and_canonical_decimals(tmp_path: Path) -> None:
    price = _price()
    record = _stored(tmp_path, price)
    assert normalize_daily_price(LocalArtifactResolver(tmp_path), record) == price
    assert _price(open="100.000").content_digest() == price.content_digest()
    assert DailyPrice.model_validate_json(price.model_dump_json()) == price


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open", 100.0),
        ("close", "NaN"),
        ("low", "0"),
        ("high", "95"),
        ("low", "106"),
        ("volume", -1),
        ("volume", True),
        ("volume", 1.5),
        ("adjustment", "ADJUSTED"),
        ("session", "2026-10-01"),
    ],
)
def test_invalid_prices_fail(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _price(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instrument_id", "krx-000660"),
        ("content_hash", "sha256:" + "0" * 64),
        ("effective_at", None),
        ("effective_at", CLOSE - timedelta(days=1)),
        ("observed_at", CLOSE - timedelta(seconds=1)),
    ],
)
def test_provenance_mismatch_fails(tmp_path: Path, field: str, value: object) -> None:
    record = _stored(tmp_path, _price())
    altered = PointInTimeRecord.model_validate({**record.model_dump(), field: value})
    with pytest.raises(ValueError):
        normalize_daily_price(LocalArtifactResolver(tmp_path), altered)


def test_raw_corruption_fails_before_normalization(tmp_path: Path) -> None:
    record = _stored(tmp_path, _price())
    (tmp_path / "sha256" / record.raw_payload_digest[7:]).write_bytes(b"{}")
    with pytest.raises(ArtifactResolutionError):
        normalize_daily_price(LocalArtifactResolver(tmp_path), record)
