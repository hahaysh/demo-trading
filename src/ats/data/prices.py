"""Normalize local price fixtures and KIS daily quotations without I/O side effects."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from ats.data.artifacts import LocalArtifactResolver
from ats.data.kis import KisDailyRequest, KisQuoteReceipt
from ats.domain.data import PointInTimeRecord
from ats.domain.governance import evidence_digest
from ats.domain.prices import SEOUL, DailyPrice
from ats.domain.strategy import FrozenModel, Sha256Digest

_DecimalText = Annotated[
    str, StringConstraints(pattern=r"^[0-9]+(?:\.[0-9]{1,6})?$", max_length=32)
]
_RatioText = Annotated[
    str, StringConstraints(pattern=r"^(?:[0-9]+(?:\.[0-9]{1,6})?)?$", max_length=32)
]
_ExRightsCode = Literal["", "00", "01", "02", "03", "04", "05", "06", "07"]
_RevaluationCode = Literal[
    "", "00", "01", "02", "03", "04", "05", "06", "07", "08", "99"
]


class KisCorporateActionFlags(FrozenModel):
    ex_rights_code: _ExRightsCode
    split_ratio_text: _RatioText
    revaluation_reason: _RevaluationCode
    no_open_indicator: Literal["", "N"]

    @property
    def requires_review(self) -> bool:
        return (
            self.ex_rights_code not in ("", "00")
            or self.split_ratio_text == ""
            or Decimal(self.split_ratio_text) != 0
            or self.revaluation_reason != "00"
            or self.no_open_indicator != "N"
        )


class KisNormalizedDailyPrice(FrozenModel):
    price: DailyPrice
    corporate_actions: KisCorporateActionFlags

    @property
    def requires_review(self) -> bool:
        return self.corporate_actions.requires_review or self.price.volume == 0


class KisDailyNormalization(FrozenModel):
    normalizer_version: Literal["kis-daily-v1"] = "kis-daily-v1"
    source_id: Literal["kis-market"] = "kis-market"
    request: KisDailyRequest
    observed_at: AwareDatetime
    policy_digest: Sha256Digest
    raw_payload_digest: Sha256Digest
    bars: tuple[KisNormalizedDailyPrice, ...]
    coverage_verified: Literal[False] = False

    def content_digest(self) -> str:
        return evidence_digest(self)


class _KisDailyRow(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    stck_bsop_date: Annotated[str, StringConstraints(pattern=r"^[0-9]{8}$")]
    stck_oprc: _DecimalText
    stck_hgpr: _DecimalText
    stck_lwpr: _DecimalText
    stck_clpr: _DecimalText
    acml_vol: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,18}$")]
    flng_cls_code: _ExRightsCode
    prtt_rate: _RatioText
    revl_issu_reas: _RevaluationCode
    mod_yn: Literal["", "N", "Y"]


class _KisPricePayload(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    rt_cd: Literal["0"]
    output1: dict[str, str]
    output2: Annotated[list[_KisDailyRow], Field(max_length=100)]


class _KisReceiptMetadata(FrozenModel):
    request: KisDailyRequest
    observed_at: AwareDatetime
    policy_digest: Sha256Digest
    raw_payload: Annotated[
        bytes, Field(strict=True, min_length=1, max_length=2 * 1024 * 1024)
    ]
    row_count: Annotated[int, Field(strict=True, ge=0, le=100)]
    source_id: Literal["kis-market"]
    origin: Literal["https://openapi.koreainvestment.com:9443"]
    adjustment: Literal["UNADJUSTED"]
    coverage_verified: Literal[False]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON fields are not permitted")
        result[key] = value
    return result


def normalize_kis_daily_prices(
    receipt: KisQuoteReceipt,
    *,
    session_closes: Mapping[date, datetime],
) -> KisDailyNormalization:
    """Convert a receipt using caller-supplied session closes; never guess a calendar."""
    try:
        metadata = _KisReceiptMetadata.model_validate(
            {
                "request": receipt.request.model_dump(),
                "observed_at": receipt.observed_at,
                "policy_digest": receipt.policy_digest,
                "raw_payload": receipt.raw_payload,
                "row_count": receipt.row_count,
                "source_id": receipt.source_id,
                "origin": receipt.origin,
                "adjustment": receipt.adjustment,
                "coverage_verified": receipt.coverage_verified,
            }
        )
        if metadata.request.end >= metadata.observed_at.astimezone(SEOUL).date():
            raise ValueError("only completed past sessions are supported")
        payload = _KisPricePayload.model_validate(
            cast(
                object,
                json.loads(metadata.raw_payload, object_pairs_hook=_unique_json_object),
            )
        )
        if payload.output1.get("stck_shrn_iscd") != metadata.request.symbol:
            raise ValueError("instrument mismatch")
        if len(payload.output2) != metadata.row_count:
            raise ValueError("receipt row count mismatch")
        closes = dict(session_closes)
        seen: set[date] = set()
        bars: list[KisNormalizedDailyPrice] = []
        for row in payload.output2:
            if row.mod_yn == "Y":
                raise ValueError("daily row has no opening trade")
            session = datetime.strptime(row.stck_bsop_date, "%Y%m%d").date()
            if (
                session in seen
                or not metadata.request.start <= session <= metadata.request.end
                or session not in closes
            ):
                raise ValueError("duplicate, out-of-range or unknown session")
            seen.add(session)
            price = DailyPrice(
                instrument_id="krx-" + metadata.request.symbol.lower(),
                session=session,
                session_close=closes[session],
                open=Decimal(row.stck_oprc),
                high=Decimal(row.stck_hgpr),
                low=Decimal(row.stck_lwpr),
                close=Decimal(row.stck_clpr),
                volume=int(row.acml_vol),
            )
            if price.session_close > metadata.observed_at:
                raise ValueError("price observed before session close")
            price = price.model_copy(
                update={"session_close": price.session_close.astimezone(UTC)}
            )
            flags = KisCorporateActionFlags(
                ex_rights_code=row.flng_cls_code,
                split_ratio_text=row.prtt_rate,
                revaluation_reason=row.revl_issu_reas,
                no_open_indicator=row.mod_yn,
            )
            bars.append(KisNormalizedDailyPrice(price=price, corporate_actions=flags))
        return KisDailyNormalization(
            request=metadata.request,
            observed_at=metadata.observed_at.astimezone(UTC),
            policy_digest=metadata.policy_digest,
            raw_payload_digest="sha256:"
            + hashlib.sha256(metadata.raw_payload).hexdigest(),
            bars=tuple(sorted(bars, key=lambda bar: bar.price.session)),
        )
    except (ValueError, TypeError, RecursionError):
        raise ValueError("KIS daily-price normalization failed validation") from None


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
