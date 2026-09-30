"""Operator-owned production quotation connection; no broker order capability."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from ats.domain.governance import evidence_digest
from ats.domain.policy import (
    PolicyStatus,
    RightsClass,
    SourceAllowlist,
    SourceCategory,
    SourceReviewState,
    load_policy,
)
from ats.domain.strategy import FrozenModel

_ORIGIN = "https://openapi.koreainvestment.com:9443"
_TOKEN_PATH = "/oauth2/tokenP"
_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
_SEOUL = timezone(timedelta(hours=9))
_MAX_BYTES = 2 * 1024 * 1024


class KisQuoteError(ValueError):
    """A sanitized, fail-closed quotation error."""


def _header_secret(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 4096
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _valid_credential(value: object) -> bool:
    return isinstance(value, SecretStr) and _header_secret(value.get_secret_value())


@dataclass(frozen=True)
class KisCredentials:
    app_key: SecretStr = field(repr=False)
    app_secret: SecretStr = field(repr=False)

    def __post_init__(self) -> None:
        if not all(
            _valid_credential(value) for value in (self.app_key, self.app_secret)
        ):
            raise KisQuoteError("Invalid KIS credentials")

    @classmethod
    def from_environment(cls) -> Self:
        return cls(
            SecretStr(os.environ.get("KIS_QUOTE_APP_KEY", "")),
            SecretStr(os.environ.get("KIS_QUOTE_APP_SECRET", "")),
        )


class KisDailyRequest(FrozenModel):
    symbol: str
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if re.fullmatch(r"[0-9A-Z]{6}", self.symbol) is None:
            raise ValueError("symbol must be a six-character domestic code")
        if not 0 <= (self.end - self.start).days < 100:
            raise ValueError("request must cover 1 to 100 calendar days")
        return self


@dataclass(frozen=True)
class KisQuoteReceipt:
    request: KisDailyRequest
    observed_at: datetime
    policy_digest: str
    raw_payload: bytes = field(repr=False)
    row_count: int
    source_id: Literal["kis-market"] = "kis-market"
    origin: Literal["https://openapi.koreainvestment.com:9443"] = _ORIGIN
    adjustment: Literal["UNADJUSTED"] = "UNADJUSTED"
    coverage_verified: Literal[False] = False

    @property
    def raw_payload_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.raw_payload).hexdigest()


class _TokenResponse(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    access_token: SecretStr
    token_type: str
    expires_in: Annotated[int, Field(gt=60, le=86400)]
    access_token_token_expired: str


class _DailyResponse(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    rt_cd: Literal["0"]
    output1: dict[str, str]
    output2: Annotated[list[dict[str, str]], Field(max_length=100)]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _approved_rate(policy: SourceAllowlist, now: datetime) -> int:
    try:
        checked = SourceAllowlist.model_validate(policy.model_dump())
    except ValueError:
        raise KisQuoteError("Invalid source policy") from None
    source = next(
        (entry for entry in checked.sources if entry.source_id == "kis-market"), None
    )
    if (
        checked.metadata.status is not PolicyStatus.APPROVED
        or checked.metadata.approved_at is None
        or checked.metadata.approved_at > now
        or source is None
        or not source.enabled
        or source.category is not SourceCategory.MARKET
        or source.legal_review is not SourceReviewState.APPROVED
        or source.rights.classification is not RightsClass.LICENSED
        or source.rights.redistribution_allowed
        or source.rights.retention_days is None
        or source.rate_limit_per_minute is None
    ):
        raise KisQuoteError(
            "KIS collection requires approved private-use source rights"
        )
    return source.rate_limit_per_minute


class KisQuoteClient:
    """Synchronous collector; keep one instance in an operator-owned process."""

    def __init__(
        self,
        credentials: KisCredentials,
        *,
        _transport: httpx.BaseTransport | None = None,
        _clock: Callable[[], datetime] = _utc_now,
        _monotonic: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._credentials = credentials
        self._clock = _clock
        self._monotonic = _monotonic
        self._sleep = _sleep
        self._lock = Lock()
        self._http = httpx.Client(
            transport=_transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        self._token: SecretStr | None = None
        self._token_expiry = datetime.min.replace(tzinfo=UTC)
        self._token_deadline = 0.0
        self._next_auth = 0.0
        self._last_send: float | None = None
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._token = None
            self._closed = True
            self._http.close()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise KisQuoteError("Collector clock must be timezone-aware")
        return now.astimezone(UTC)

    def _send(
        self,
        method: str,
        path: str,
        *,
        rate: int,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        body: Mapping[str, str] | None = None,
    ) -> bytes:
        if (method, path) not in (("POST", _TOKEN_PATH), ("GET", _DAILY_PATH)):
            raise KisQuoteError("KIS endpoint is not permitted")
        interval = max(1.0, 60.0 / rate)
        if self._last_send is not None:
            wait = self._last_send + interval - self._monotonic()
            if wait > 0:
                self._sleep(wait)
        self._last_send = self._monotonic()
        try:
            with self._http.stream(
                method,
                _ORIGIN + path,
                headers={"accept-encoding": "identity", **headers},
                params=params,
                json=body,
            ) as response:
                if response.status_code != 200:
                    raise KisQuoteError("KIS HTTP request rejected; no automatic retry")
                if (
                    response.headers.get("content-type", "").split(";")[0].lower()
                    != "application/json"
                    or response.headers.get("content-encoding", "identity")
                    != "identity"
                ):
                    raise KisQuoteError("Unexpected KIS response encoding")
                maximum = 65536 if path == _TOKEN_PATH else _MAX_BYTES
                payload = bytearray()
                for chunk in response.iter_bytes():
                    if (
                        len(payload) + len(chunk) > maximum
                        or self._monotonic() - self._last_send > 30
                    ):
                        raise KisQuoteError("KIS response exceeds collection bounds")
                    payload.extend(chunk)
                return bytes(payload)
        except httpx.HTTPError:
            raise KisQuoteError("KIS transport failed; no automatic retry") from None

    def _access_token(self, rate: int) -> str:
        now = self._now()
        if (
            self._token is not None
            and now < self._token_expiry
            and self._monotonic() < self._token_deadline
        ):
            return self._token.get_secret_value()
        self._token = None
        if self._monotonic() < self._next_auth:
            raise KisQuoteError("KIS token issuance cooldown is active")
        self._next_auth = self._monotonic() + 60
        payload = self._send(
            "POST",
            _TOKEN_PATH,
            rate=rate,
            headers={"content-type": "application/json; charset=utf-8"},
            body={
                "grant_type": "client_credentials",
                "appkey": self._credentials.app_key.get_secret_value(),
                "appsecret": self._credentials.app_secret.get_secret_value(),
            },
        )
        try:
            token = _TokenResponse.model_validate_json(payload)
            absolute = datetime.strptime(
                token.access_token_token_expired, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=_SEOUL)
            expiry = min(absolute, now + timedelta(seconds=token.expires_in))
            expiry -= timedelta(seconds=60)
            if (
                token.token_type.lower() != "bearer"
                or not _header_secret(token.access_token.get_secret_value())
                or expiry <= self._now()
            ):
                raise ValueError("Unusable token")
        except ValueError:
            raise KisQuoteError("Invalid KIS token response") from None
        self._token = token.access_token
        self._token_expiry = expiry.astimezone(UTC)
        self._token_deadline = (
            self._monotonic() + (self._token_expiry - self._now()).total_seconds()
        )
        return token.access_token.get_secret_value()

    def daily_prices(
        self, request: KisDailyRequest, *, source_policy: SourceAllowlist
    ) -> KisQuoteReceipt:
        with self._lock:
            if self._closed:
                raise KisQuoteError("KIS quotation client is closed")
            rate = _approved_rate(source_policy, self._now())
            request = KisDailyRequest.model_validate(request.model_dump())
            if request.end >= self._now().astimezone(_SEOUL).date():
                raise KisQuoteError("Only past sessions may be requested")
            token = self._access_token(rate)
            payload = self._send(
                "GET",
                _DAILY_PATH,
                rate=rate,
                headers={
                    "content-type": "application/json; charset=utf-8",
                    "authorization": "Bearer " + token,
                    "appkey": self._credentials.app_key.get_secret_value(),
                    "appsecret": self._credentials.app_secret.get_secret_value(),
                    "tr_id": "FHKST03010100",
                    "custtype": "P",
                },
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": request.symbol,
                    "FID_INPUT_DATE_1": request.start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": request.end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "1",
                },
            )
            observed_at = self._now()
            try:
                response = _DailyResponse.model_validate_json(payload)
                if response.output1.get("stck_shrn_iscd") != request.symbol:
                    raise ValueError("Wrong instrument")
                sessions: set[date] = set()
                for row in response.output2:
                    session = datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date()
                    if (
                        not request.start <= session <= request.end
                        or session in sessions
                    ):
                        raise ValueError("Invalid returned sessions")
                    sessions.add(session)
            except (ValueError, KeyError):
                raise KisQuoteError("Invalid KIS daily-price response") from None
            return KisQuoteReceipt(
                request=request,
                observed_at=observed_at,
                policy_digest=evidence_digest(source_policy),
                raw_payload=payload,
                row_count=len(response.output2),
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KIS quotes-only connection check")
    parser.add_argument(
        "--policy", type=Path, default=Path("config/source-allowlist.yaml")
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    if not args.allow_network:
        print(
            "Network disabled; explicit --allow-network and approved policy required."
        )
        return 0
    try:
        policy = load_policy(args.policy, SourceAllowlist)
        _approved_rate(policy, _utc_now())
        request = KisDailyRequest(symbol=args.symbol, start=args.start, end=args.end)
        with KisQuoteClient(KisCredentials.from_environment()) as client:
            receipt = client.daily_prices(request, source_policy=policy)
        print(
            json.dumps(
                {
                    "source_id": receipt.source_id,
                    "observed_at": receipt.observed_at.isoformat(),
                    "raw_payload_digest": receipt.raw_payload_digest,
                    "policy_digest": receipt.policy_digest,
                    "row_count": receipt.row_count,
                    "coverage_verified": False,
                    "persisted": False,
                }
            )
        )
        return 0
    except (ValueError, OSError):
        print(
            "KIS quote check failed; verify policy, local credentials and connectivity."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
