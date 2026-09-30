import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from ats.data.artifacts import ArtifactResolutionError, LocalArtifactResolver
from ats.data.kis import KisDailyRequest, KisQuoteReceipt
from ats.data.prices import (
    KisDailyNormalization,
    normalize_daily_price,
    normalize_kis_daily_prices,
)
from ats.domain.data import PointInTimeRecord
from ats.domain.prices import SEOUL, DailyPrice

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


def _kis_row(**changes: object) -> dict[str, object]:
    return {
        "stck_bsop_date": "20260930",
        "stck_oprc": "100.000",
        "stck_hgpr": "110",
        "stck_lwpr": "90",
        "stck_clpr": "105.125",
        "acml_vol": "1000",
        "flng_cls_code": "",
        "prtt_rate": "0.00",
        "revl_issu_reas": "00",
        "mod_yn": "N",
        **changes,
    }


def _kis_receipt(*rows: dict[str, object]) -> KisQuoteReceipt:
    return KisQuoteReceipt(
        request=KisDailyRequest(
            symbol="005930", start=date(2026, 9, 1), end=date(2026, 9, 30)
        ),
        observed_at=datetime(2026, 10, 1, tzinfo=UTC),
        policy_digest="sha256:" + "a" * 64,
        raw_payload=json.dumps(
            {
                "rt_cd": "0",
                "output1": {"stck_shrn_iscd": "005930"},
                "output2": list(rows),
            }
        ).encode(),
        row_count=len(rows),
    )


def test_kis_normalization_preserves_exact_prices_flags_and_provenance() -> None:
    receipt = _kis_receipt(_kis_row())
    result = normalize_kis_daily_prices(receipt, session_closes={CLOSE.date(): CLOSE})
    price = result.bars[0].price
    assert price.instrument_id == "krx-005930"
    assert price.open == Decimal("100")
    assert price.close == Decimal("105.125")
    assert price.volume == 1000
    assert price.session_close == CLOSE
    assert price.adjustment == "UNADJUSTED"
    assert result.raw_payload_digest == receipt.raw_payload_digest
    assert result.policy_digest == receipt.policy_digest
    assert result.observed_at == receipt.observed_at
    assert result.bars[0].corporate_actions.split_ratio_text == "0.00"
    assert not result.bars[0].requires_review
    assert not result.coverage_verified
    assert result == normalize_kis_daily_prices(
        receipt, session_closes={CLOSE.date(): CLOSE}
    )
    assert result.content_digest().startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stck_oprc", "0"),
        ("stck_oprc", "-1"),
        ("stck_oprc", "NaN"),
        ("stck_oprc", "1e2"),
        ("stck_oprc", " 100"),
        ("stck_oprc", 100.0),
        ("stck_oprc", True),
        ("stck_hgpr", "95"),
        ("stck_lwpr", "106"),
        ("stck_clpr", "100.1234567"),
        ("acml_vol", "-1"),
        ("acml_vol", "1.5"),
        ("acml_vol", "1e3"),
        ("acml_vol", True),
        ("acml_vol", 1000),
        ("stck_bsop_date", "20260931"),
        ("stck_bsop_date", "2026-09-30"),
        ("stck_bsop_date", "20261001"),
        ("mod_yn", "Y"),
        ("mod_yn", "unknown"),
        ("flng_cls_code", "99"),
        ("prtt_rate", "-1"),
        ("prtt_rate", "NaN"),
        ("revl_issu_reas", "42"),
    ],
)
def test_kis_rejects_invalid_rows(field: str, value: object) -> None:
    receipt = _kis_receipt(_kis_row(**{field: value}))
    with pytest.raises(ValueError, match="normalization failed"):
        normalize_kis_daily_prices(receipt, session_closes={CLOSE.date(): CLOSE})


@pytest.mark.parametrize("field", list(_kis_row()))
def test_kis_missing_fields_are_not_filled(field: str) -> None:
    row = _kis_row()
    del row[field]
    with pytest.raises(ValueError, match="normalization failed"):
        normalize_kis_daily_prices(
            _kis_receipt(row), session_closes={CLOSE.date(): CLOSE}
        )


def test_kis_preserves_corporate_actions_without_adjusting_prices() -> None:
    receipt = _kis_receipt(
        _kis_row(flng_cls_code="03", prtt_rate="0.5", revl_issu_reas="01")
    )
    result = normalize_kis_daily_prices(receipt, session_closes={CLOSE.date(): CLOSE})
    bar = result.bars[0]
    assert bar.requires_review
    assert bar.corporate_actions.ex_rights_code == "03"
    assert bar.corporate_actions.split_ratio_text == "0.5"
    assert bar.corporate_actions.revaluation_reason == "01"
    assert bar.price.open == Decimal("100")
    assert bar.price.volume == 1000


@pytest.mark.parametrize(
    "changes",
    [
        {"prtt_rate": ""},
        {"revl_issu_reas": ""},
        {"mod_yn": ""},
        {"acml_vol": "0"},
    ],
)
def test_kis_incomplete_flags_or_zero_volume_require_review(
    changes: dict[str, object],
) -> None:
    result = normalize_kis_daily_prices(
        _kis_receipt(_kis_row(**changes)), session_closes={CLOSE.date(): CLOSE}
    )
    assert result.bars[0].requires_review


@pytest.mark.parametrize(
    "closes",
    [
        {},
        {CLOSE.date(): CLOSE.replace(tzinfo=None)},
        {CLOSE.date(): CLOSE + timedelta(days=1)},
    ],
)
def test_kis_requires_explicit_valid_session_closes(
    closes: dict[date, datetime],
) -> None:
    with pytest.raises(ValueError, match="normalization failed"):
        normalize_kis_daily_prices(_kis_receipt(_kis_row()), session_closes=closes)


def test_kis_rejects_duplicate_rows_and_partial_results() -> None:
    for receipt in (
        _kis_receipt(_kis_row(), _kis_row()),
        _kis_receipt(_kis_row(stck_bsop_date="20260929"), _kis_row(mod_yn="Y")),
    ):
        with pytest.raises(ValueError, match="normalization failed"):
            normalize_kis_daily_prices(
                receipt,
                session_closes={
                    CLOSE.date(): CLOSE,
                    date(2026, 9, 29): CLOSE - timedelta(days=1),
                },
            )


def test_kis_revalidates_receipt_count_and_observation() -> None:
    receipt = _kis_receipt(_kis_row())
    for changed in (
        replace(receipt, row_count=2),
        replace(receipt, observed_at=CLOSE - timedelta(seconds=1)),
        replace(receipt, observed_at=CLOSE.replace(tzinfo=None)),
        replace(receipt, policy_digest="bad-digest"),
    ):
        with pytest.raises(ValueError, match="normalization failed"):
            normalize_kis_daily_prices(changed, session_closes={CLOSE.date(): CLOSE})


def test_kis_rejects_duplicate_json_fields() -> None:
    receipt = _kis_receipt(_kis_row())
    changed = replace(
        receipt,
        raw_payload=receipt.raw_payload.replace(
            b'"stck_oprc": "100.000"', b'"stck_oprc": "1", "stck_oprc": "100.000"'
        ),
    )
    with pytest.raises(ValueError, match="normalization failed"):
        normalize_kis_daily_prices(changed, session_closes={CLOSE.date(): CLOSE})


def test_kis_empty_and_missing_sessions_do_not_claim_coverage() -> None:
    empty = normalize_kis_daily_prices(_kis_receipt(), session_closes={})
    assert empty.bars == ()
    assert not empty.coverage_verified
    result = normalize_kis_daily_prices(
        _kis_receipt(_kis_row()),
        session_closes={
            CLOSE.date(): CLOSE,
            date(2026, 9, 29): CLOSE - timedelta(days=1),
        },
    )
    assert len(result.bars) == 1
    assert not result.coverage_verified


def test_kis_sorts_sessions_and_normalizes_equivalent_timezones() -> None:
    receipt = _kis_receipt(_kis_row(), _kis_row(stck_bsop_date="20260929"))
    closes = {CLOSE.date(): CLOSE, date(2026, 9, 29): CLOSE - timedelta(days=1)}
    result = normalize_kis_daily_prices(receipt, session_closes=closes)
    assert [bar.price.session for bar in result.bars] == [
        date(2026, 9, 29),
        CLOSE.date(),
    ]
    same_instants = normalize_kis_daily_prices(
        replace(receipt, observed_at=receipt.observed_at.astimezone(SEOUL)),
        session_closes={
            session: close.astimezone(SEOUL) for session, close in closes.items()
        },
    )
    assert result.content_digest() == same_instants.content_digest()
    assert result == KisDailyNormalization.model_validate_json(result.model_dump_json())


def test_kis_flags_change_normalization_digest_not_price_digest() -> None:
    ordinary = normalize_kis_daily_prices(
        _kis_receipt(_kis_row()), session_closes={CLOSE.date(): CLOSE}
    )
    action = normalize_kis_daily_prices(
        _kis_receipt(_kis_row(flng_cls_code="02")), session_closes={CLOSE.date(): CLOSE}
    )
    assert (
        ordinary.bars[0].price.content_digest() == action.bars[0].price.content_digest()
    )
    assert ordinary.content_digest() != action.content_digest()
    assert action.bars[0].requires_review


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "other-provider"},
        {"origin": "https://untrusted.invalid"},
        {"adjustment": "ADJUSTED"},
        {"row_count": True},
        {"coverage_verified": True},
        {"raw_payload": b""},
        {"raw_payload": b"not-json"},
        {"raw_payload": b"[]"},
        {"raw_payload": b"{}" * (1024 * 1024 + 1)},
    ],
)
def test_kis_rejects_invalid_receipt_metadata(changes: dict[str, object]) -> None:
    receipt = replace(_kis_receipt(_kis_row()), **changes)
    with pytest.raises(ValueError, match="normalization failed"):
        normalize_kis_daily_prices(receipt, session_closes={CLOSE.date(): CLOSE})


@pytest.mark.parametrize(
    "payload",
    [
        {"rt_cd": "1", "output1": {"stck_shrn_iscd": "005930"}, "output2": []},
        {"rt_cd": "0", "output1": {"stck_shrn_iscd": "000660"}, "output2": []},
        {
            "rt_cd": "0",
            "output1": {"stck_shrn_iscd": "005930"},
            "output2": [_kis_row()] * 101,
        },
    ],
)
def test_kis_rejects_error_instrument_and_row_limit(payload: dict[str, object]) -> None:
    receipt = replace(_kis_receipt(), raw_payload=json.dumps(payload).encode())
    with pytest.raises(ValueError, match="normalization failed"):
        normalize_kis_daily_prices(receipt, session_closes={CLOSE.date(): CLOSE})


def test_kis_explicit_delayed_session_close_is_preserved() -> None:
    delayed = CLOSE + timedelta(hours=1)
    result = normalize_kis_daily_prices(
        _kis_receipt(_kis_row()), session_closes={CLOSE.date(): delayed}
    )
    assert result.bars[0].price.session_close == delayed
