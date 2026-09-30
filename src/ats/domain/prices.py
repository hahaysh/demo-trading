"""Daily price observations with exact decimal values and explicit close times."""

from datetime import date, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from ats.domain.governance import evidence_digest
from ats.domain.strategy import FrozenModel, Identifier

Price = Annotated[
    Decimal, Field(gt=0, max_digits=20, decimal_places=6, allow_inf_nan=False)
]
SEOUL = timezone(timedelta(hours=9))


class DailyPrice(FrozenModel):
    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["DailyPrice"] = "DailyPrice"
    instrument_id: Identifier
    session: date
    session_close: AwareDatetime
    currency: Literal["KRW"] = "KRW"
    adjustment: Literal["UNADJUSTED"] = "UNADJUSTED"
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def reject_float(cls, value: object) -> object:
        if isinstance(value, (float, bool)):
            raise ValueError("prices require decimal strings, integers, or Decimal")
        return value

    @field_serializer("open", "high", "low", "close")
    def serialize_price(self, value: Decimal) -> str:
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        if self.session_close.astimezone(SEOUL).date() != self.session:
            raise ValueError("session close must belong to the declared KRX date")
        if (
            not self.low
            <= min(self.open, self.close)
            <= max(self.open, self.close)
            <= self.high
        ):
            raise ValueError("inconsistent OHLC range")
        return self

    def content_digest(self) -> str:
        return evidence_digest(self)
