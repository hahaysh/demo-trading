"""Production read-only transport; no credential discovery or order capability."""

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from ats.domain.governance import evidence_digest
from ats.domain.policy import PolicyMetadata, PolicyStatus, SourceAllowlist
from ats.domain.prices import SEOUL
from ats.domain.strategy import FrozenModel, Identifier, Sha256Digest

ORIGIN = "https://openapi.koreainvestment.com:9443"
TOKEN_PATH = "/oauth2/tokenP"
ReadOperation = Literal["BALANCE", "CAPACITY", "HISTORY", "OPEN_ORDERS", "QUOTE", "DAILY"]
ROUTES = {
    "BALANCE": ("/uapi/domestic-stock/v1/trading/inquire-balance", "TTTC8434R"),
    "CAPACITY": ("/uapi/domestic-stock/v1/trading/inquire-psbl-order", "TTTC8908R"),
    "HISTORY": ("/uapi/domestic-stock/v1/trading/inquire-daily-ccld", "TTTC0081R"),
    "OPEN_ORDERS": ("/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl", "TTTC0084R"),
    "QUOTE": ("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100"),
    "DAILY": ("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100"),
}


class _TokenResponse(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    access_token: SecretStr
    token_type: str
    expires_in: Annotated[int, Field(gt=60, le=86400)]
    access_token_token_expired: str


def _header_secret(value: str) -> bool:
    return bool(value) and len(value) <= 4096 and all(33 <= ord(char) <= 126 for char in value)


class ReadOnlyError(ValueError):
    """Only fixed, non-sensitive failure descriptions may cross this boundary."""


class _RetryableReadError(ReadOnlyError):
    pass


class ReadOnlyAccessPermit(FrozenModel):
    metadata: PolicyMetadata
    account_id: Identifier
    credential_ref: Identifier
    binding_id: UUID
    source_policy_digest: Sha256Digest
    source_id: Identifier = "kis-market"
    starts_at: AwareDatetime
    expires_at: AwareDatetime
    enabled: bool = False
    operations: tuple[ReadOperation, ...] = ("BALANCE",)
    symbols: tuple[Annotated[str, Field(pattern=r"^[A-Z0-9]{6}$")], ...] = ()
    history_start: date | None = None
    max_calls: Annotated[int, Field(strict=True, ge=1, le=500)] = 60
    max_pages: Annotated[int, Field(strict=True, ge=1, le=20)] = 20
    max_bytes: Annotated[int, Field(strict=True, ge=1024, le=4194304)] = 1048576
    max_seconds: Annotated[int, Field(strict=True, ge=1, le=120)] = 120
    max_retries: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    rate_per_minute: Annotated[int, Field(strict=True, ge=1, le=60)] = 1
    synthetic_only: bool = False
    agent_editable: Literal[False] = False


class ReadOnlyCredentials(FrozenModel):
    account_id: Identifier
    credential_ref: Identifier
    binding_id: UUID
    app_key: SecretStr
    app_secret: SecretStr
    account_number: SecretStr
    product_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")]


class ReadReceipt(FrozenModel):
    account_id: Identifier
    operation: ReadOperation
    permit_digest: Sha256Digest
    started_at: AwareDatetime
    observed_at: AwareDatetime
    response_digest: Sha256Digest
    mode: Literal["SYNTHETIC", "PRODUCTION_READ_ONLY"]
    binding: Literal["REQUEST_BOUND_NOT_BROKER_SIGNED"] = "REQUEST_BOUND_NOT_BROKER_SIGNED"


Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class AccountHolding(FrozenModel):
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9]{6}$")]
    quantity: Annotated[int, Field(strict=True, ge=0)]
    sellable_quantity: Annotated[int, Field(strict=True, ge=0)]
    market_value: Money


class ProductionAccountObservation(FrozenModel):
    account_id: Identifier
    mode: Literal["SYNTHETIC", "PRODUCTION_READ_ONLY"]
    started_at: AwareDatetime
    observed_at: AwareDatetime
    receipts: tuple[ReadReceipt, ...]
    holdings: tuple[AccountHolding, ...]
    deposit_balance: Money
    total_equity: Money
    securities_value: Money
    cash_orderable: Money | None = None
    no_margin_buy_amount: Money | None = None
    no_margin_buy_quantity: int | None = None
    capacity_symbol: str | None = None
    capacity_price: Money | None = None
    unsettled_receivable: Money | None = None
    credit_amount: Money | None = None
    external_reservations_verified: Literal[False] = False
    cash_flow_baseline_verified: Literal[False] = False
    snapshot_atomic: Literal[False] = False
    uncertainties: tuple[str, ...] = ("CASH_FLOW_BASELINE_UNKNOWN", "RESERVATIONS_UNVERIFIED", "NON_ATOMIC_REST_SNAPSHOT")


def _amount(value: object) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ReadOnlyError("invalid nonnegative money field")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ReadOnlyError("invalid money field") from None


def _quantity(value: object) -> int:
    amount = _amount(value)
    if amount != amount.to_integral_value():
        raise ReadOnlyError("invalid integral quantity field")
    return int(amount)


class ReadOnlyKisClient:
    def __init__(
        self, permit: Callable[[], ReadOnlyAccessPermit], source_policy: Callable[[], SourceAllowlist],
        credentials: Callable[[str], ReadOnlyCredentials], *, account_id: str,
        transport: httpx.BaseTransport | None = None, allow_network: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(transport, httpx.MockTransport) and not allow_network:
            raise ReadOnlyError("real read-only networking is disarmed")
        self._permit, self._policy, self._loader = permit, source_policy, credentials
        self.account_id, self._clock, self._monotonic = account_id, clock, monotonic
        self.synthetic = isinstance(transport, httpx.MockTransport)
        self._http = httpx.Client(transport=transport, timeout=10, follow_redirects=False, trust_env=False)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._token: SecretStr | None = None
        self._credentials: ReadOnlyCredentials | None = None
        self._expiry = datetime.min.replace(tzinfo=UTC)
        self._token_deadline = 0.0
        self._next_auth = 0.0
        self._last_send: float | None = None
        self._calls = 0
        self._started = monotonic()
        self._permit_digest: str | None = None
        self.audit: list[dict[str, str | int]] = []

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._token = None
            self._credentials = None
            self._http.close()

    def _authorize(self, operation: str) -> ReadOnlyAccessPermit:
        if operation not in ROUTES:
            raise ReadOnlyError("read operation is not allowlisted")
        try:
            permit = ReadOnlyAccessPermit.model_validate(self._permit().model_dump())
            policy = SourceAllowlist.model_validate(self._policy().model_dump())
            now = self._clock()
            source = next((entry for entry in policy.sources if entry.source_id == permit.source_id), None)
            if (
                self._stop.is_set() or now.tzinfo is None or now.utcoffset() is None
                or not permit.enabled or permit.account_id != self.account_id
                or permit.synthetic_only != self.synthetic
                or permit.metadata.status is not PolicyStatus.APPROVED
                or permit.metadata.approved_at is None or permit.metadata.approved_at > now
                or not permit.starts_at <= now < permit.expires_at
                or operation not in permit.operations
                or evidence_digest(policy) != permit.source_policy_digest
                or policy.metadata.status is not PolicyStatus.APPROVED
                or policy.metadata.approved_at is None or policy.metadata.approved_at > now
                or source is None or not source.enabled or source.category.value != "MARKET"
                or source.legal_review.value != "APPROVED" or source.rights.classification.value != "LICENSED"
                or source.rights.retention_days is None
                or source.rights.redistribution_allowed or source.rate_limit_per_minute is None
                or permit.rate_per_minute > source.rate_limit_per_minute
                or self._monotonic() - self._started >= permit.max_seconds
            ):
                raise ReadOnlyError("read-only permit, source rights or execution window rejected")
            digest = evidence_digest(permit)
            if self._permit_digest is not None and self._permit_digest != digest:
                raise ReadOnlyError("read-only permit changed; create a new session")
            self._permit_digest = digest
            return permit
        except (ValueError, TypeError):
            raise ReadOnlyError("read-only authorization failed") from None

    def _load(self, permit: ReadOnlyAccessPermit) -> ReadOnlyCredentials:
        if self._credentials is None:
            try:
                supplied = ReadOnlyCredentials.model_validate(self._loader(permit.credential_ref).model_dump())
                if supplied.account_id != permit.account_id or supplied.credential_ref != permit.credential_ref or supplied.binding_id != permit.binding_id or not re.fullmatch(r"[0-9]{8}", supplied.account_number.get_secret_value()) or not _header_secret(supplied.app_key.get_secret_value()) or not _header_secret(supplied.app_secret.get_secret_value()):
                    raise ValueError("binding")
                self._credentials = supplied
            except Exception:
                raise ReadOnlyError("protected credential binding unavailable") from None
        return self._credentials

    def _send(self, operation: str, *, token: bool, parameters: dict[str, str], continuation: str = "") -> tuple[dict[str, Any], httpx.Headers, str]:
        permit = self._authorize(operation)
        if self._calls >= permit.max_calls:
            raise ReadOnlyError("read-only HTTP budget exhausted")
        if self._last_send is not None and not self.synthetic:
            delay = max(0, self._last_send + 60 / permit.rate_per_minute - self._monotonic())
            if self._stop.wait(delay):
                raise ReadOnlyError("read-only session stopped")
            permit = self._authorize(operation)
        credentials = self._load(permit)
        path, transaction = ROUTES[operation]
        headers = {"accept-encoding": "identity", "content-type": "application/json"}
        body: dict[str, str] | None = None
        if token:
            path = TOKEN_PATH
            body = {"grant_type": "client_credentials", "appkey": credentials.app_key.get_secret_value(), "appsecret": credentials.app_secret.get_secret_value()}
        else:
            if self._token is None:
                raise ReadOnlyError("read-only token is unavailable")
            headers.update({"authorization": "Bearer " + self._token.get_secret_value(), "appkey": credentials.app_key.get_secret_value(), "appsecret": credentials.app_secret.get_secret_value(), "tr_id": transaction, "custtype": "P", "tr_cont": continuation})
            if operation not in ("QUOTE", "DAILY"):
                parameters = {**parameters, "CANO": credentials.account_number.get_secret_value(), "ACNT_PRDT_CD": credentials.product_code}
        self._calls += 1
        self._last_send = self._monotonic()
        self.audit.append({"method": "POST" if token else "GET", "path": path, "call": self._calls})
        try:
            with self._http.stream("POST" if token else "GET", ORIGIN + path, headers=headers, params=None if token else parameters, json=body) as response:
                if response.status_code in (401,403):
                    self.stop()
                if not token and response.status_code in (429,500,502,503,504):
                    raise _RetryableReadError("temporary read-only HTTP failure")
                if response.status_code != 200:
                    raise ReadOnlyError("read-only HTTP response rejected")
                if response.headers.get("content-type", "").split(";")[0] != "application/json" or response.headers.get("content-encoding", "identity") != "identity":
                    raise ReadOnlyError("read-only response encoding rejected")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    self._authorize(operation)
                    if len(raw) + len(chunk) > (65536 if token else permit.max_bytes):
                        raise ReadOnlyError("read-only response exceeds byte budget")
                    raw.extend(chunk)
                def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                    result: dict[str, Any] = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate field")
                        result[key] = value
                    return result
                data = TypeAdapter(dict[str, Any]).validate_python(json.loads(raw, object_pairs_hook=unique))
                self._authorize(operation)
                if not token and data.get("rt_cd") != "0":
                    raise ReadOnlyError("read-only broker response rejected")
                for key, expected in (("CANO", credentials.account_number.get_secret_value()), ("ACNT_PRDT_CD", credentials.product_code)):
                    if key in data and data[key] != expected or key.lower() in data and data[key.lower()] != expected:
                        raise ReadOnlyError("read-only account echo mismatch")
                return data, response.headers, "sha256:" + hashlib.sha256(raw).hexdigest()
        except _RetryableReadError:
            raise
        except httpx.TransportError:
            if not token:
                raise _RetryableReadError("temporary read-only transport failure") from None
            raise ReadOnlyError("read-only authentication transport failed") from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise ReadOnlyError("read-only response or transport failed") from None

    def _get(self, operation: ReadOperation, parameters: dict[str,str], continuation: str) -> tuple[dict[str,Any],httpx.Headers,str]:
        permit = self._authorize(operation)
        for attempt in range(permit.max_retries + 1):
            self._authorize(operation)
            self._authenticate(operation)
            try:
                return self._send(operation,token=False,parameters=parameters,continuation=continuation)
            except _RetryableReadError:
                if attempt == permit.max_retries:
                    raise ReadOnlyError("read-only retry budget exhausted") from None
                if not self.synthetic and self._stop.wait(2**attempt):
                    raise ReadOnlyError("read-only session stopped") from None
        raise ReadOnlyError("read-only request incomplete")

    def _authenticate(self, operation: str) -> None:
        if self._token is not None and self._clock() < self._expiry and self._monotonic() < self._token_deadline:
            return
        self._token = None
        if self._monotonic() < self._next_auth:
            raise ReadOnlyError("read-only token issuance cooldown active")
        self._next_auth = self._monotonic() + 60
        issued = self._clock()
        data, _, _ = self._send(operation, token=True, parameters={})
        try:
            result = _TokenResponse.model_validate(data)
            absolute = datetime.strptime(result.access_token_token_expired, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SEOUL)
            expiry = min(absolute, issued + timedelta(seconds=result.expires_in)) - timedelta(seconds=60)
            if result.token_type.lower() != "bearer" or not _header_secret(result.access_token.get_secret_value()) or expiry <= self._clock():
                raise ValueError("invalid token")
            self._token, self._expiry = result.access_token, expiry
            self._token_deadline = self._monotonic() + (expiry - self._clock()).total_seconds()
        except ValueError:
            raise ReadOnlyError("read-only token response rejected") from None

    def _pages(self, operation: ReadOperation, parameters: dict[str, str], *, paginated: bool = True) -> tuple[tuple[dict[str, Any], ...], tuple[ReadReceipt, ...]]:
        with self._lock:
            permit = self._authorize(operation)
            self._authenticate(operation)
            rows: list[dict[str, Any]] = []
            receipts: list[ReadReceipt] = []
            cursor = ("", "")
            seen: set[tuple[str, str]] = set()
            for index in range(permit.max_pages):
                started = self._clock()
                data, headers, digest = self._get(operation, continuation="N" if index else "", parameters={**parameters, **({"CTX_AREA_FK100":cursor[0], "CTX_AREA_NK100":cursor[1]} if paginated else {})})
                rows.append(data)
                receipts.append(ReadReceipt(account_id=permit.account_id, operation=operation, permit_digest=evidence_digest(permit), started_at=started, observed_at=self._clock(), response_digest=digest, mode="SYNTHETIC" if self.synthetic else "PRODUCTION_READ_ONLY"))
                if headers.get("tr_cont", "") not in ("F", "M"):
                    return tuple(rows), tuple(receipts)
                if not paginated:
                    raise ReadOnlyError("unexpected partial single response")
                values = (data.get("ctx_area_fk100"), data.get("ctx_area_nk100"))
                if not all(isinstance(value, str) for value in values):
                    raise ReadOnlyError("read-only page cursor is invalid")
                cursor = (str(values[0]).strip(), str(values[1]).strip())
                if not any(cursor) or cursor in seen:
                    raise ReadOnlyError("read-only pagination loop or incomplete cursor")
                seen.add(cursor)
            raise ReadOnlyError("read-only page budget exhausted")

    def balance_pages(self) -> tuple[tuple[dict[str, Any], ...], tuple[ReadReceipt, ...]]:
        return self._pages("BALANCE", {"AFHR_FLPR_YN":"N", "OFL_YN":"", "INQR_DVSN":"02", "UNPR_DVSN":"01", "FUND_STTL_ICLD_YN":"N", "FNCG_AMT_AUTO_RDPT_YN":"N", "PRCS_DVSN":"00"})

    def _symbol(self, operation: ReadOperation, symbol: str) -> None:
        permit = self._authorize(operation)
        if symbol not in permit.symbols:
            raise ReadOnlyError("symbol is outside approved read scope")

    def quote(self, symbol: str) -> tuple[dict[str, Any], ReadReceipt]:
        with self._lock:
            self._symbol("QUOTE", symbol)
            pages, receipts = self._pages("QUOTE", {"FID_COND_MRKT_DIV_CODE":"J", "FID_INPUT_ISCD":symbol}, paginated=False)
            return pages[0], receipts[0]

    def capacity(self, symbol: str, price: Decimal) -> tuple[dict[str, Any], ReadReceipt]:
        with self._lock:
            self._symbol("CAPACITY", symbol)
            if not price.is_finite() or price <= 0:
                raise ReadOnlyError("positive finite capacity reference price required")
            pages, receipts = self._pages("CAPACITY", {"PDNO":symbol,"ORD_UNPR":str(price),"ORD_DVSN":"01","CMA_EVLU_AMT_ICLD_YN":"N","OVRS_ICLD_YN":"N"}, paginated=False)
            return pages[0], receipts[0]

    def history(self, start: date, end: date) -> tuple[tuple[dict[str, Any], ...], tuple[ReadReceipt, ...]]:
        permit = self._authorize("HISTORY")
        today = self._clock().astimezone(SEOUL).date()
        if permit.history_start is None or not max(permit.history_start, today-timedelta(days=60)) <= start <= end <= today:
            raise ReadOnlyError("history range is outside approved recent window")
        return self._pages("HISTORY", {"INQR_STRT_DT":start.strftime("%Y%m%d"),"INQR_END_DT":end.strftime("%Y%m%d"),"SLL_BUY_DVSN_CD":"00","PDNO":"","CCLD_DVSN":"00","INQR_DVSN":"01","INQR_DVSN_3":"00","ORD_GNO_BRNO":"","ODNO":"","INQR_DVSN_1":"","EXCG_ID_DVSN_CD":"ALL"})

    def open_orders(self) -> tuple[tuple[dict[str, Any], ...], tuple[ReadReceipt, ...]]:
        return self._pages("OPEN_ORDERS", {"INQR_DVSN_1":"0","INQR_DVSN_2":"0"})

    def daily_prices(self, symbol: str, start: date, end: date) -> tuple[dict[str, Any], ReadReceipt]:
        permit = self._authorize("DAILY")
        self._symbol("DAILY", symbol)
        if permit.history_start is None or not permit.history_start <= start <= end < self._clock().astimezone(SEOUL).date() or (end-start).days > 89:
            raise ReadOnlyError("daily price range is outside approved completed window")
        pages, receipts = self._pages("DAILY", {"FID_COND_MRKT_DIV_CODE":"J","FID_INPUT_ISCD":symbol,"FID_INPUT_DATE_1":start.strftime("%Y%m%d"),"FID_INPUT_DATE_2":end.strftime("%Y%m%d"),"FID_PERIOD_DIV_CODE":"D","FID_ORG_ADJ_PRC":"1"}, paginated=False)
        return pages[0], receipts[0]

    def account_observation(self, *, symbol: str, price: Decimal) -> ProductionAccountObservation:
        with self._lock:
            self._symbol("CAPACITY", symbol)
            pages, receipts = self.balance_pages()
            capacity, capacity_receipt = self.capacity(symbol, price)
            if (capacity_receipt.observed_at - receipts[0].started_at).total_seconds() > 30:
                raise ReadOnlyError("account observation time spread exceeded")
            try:
                holdings: list[AccountHolding] = []
                summary: dict[str, str] | None = None
                for page in pages:
                    totals = TypeAdapter(list[dict[str,str]]).validate_python(page["output2"])
                    if len(totals) != 1 or summary is not None and summary != totals[0]:
                        raise ReadOnlyError("account changed during pagination")
                    summary = totals[0]
                    for row in TypeAdapter(list[dict[str,str]]).validate_python(page["output1"]):
                        holding = AccountHolding(symbol=row["pdno"],quantity=_quantity(row["hldg_qty"]),sellable_quantity=_quantity(row["ord_psbl_qty"]),market_value=_amount(row["evlu_amt"]))
                        if holding.sellable_quantity > holding.quantity:
                            raise ReadOnlyError("sellable quantity exceeds holding")
                        holdings.append(holding)
                if summary is None or len({item.symbol for item in holdings}) != len(holdings) or sum(item.market_value for item in holdings) != _amount(summary["scts_evlu_amt"]):
                    raise ReadOnlyError("account holdings do not reconcile")
                output = TypeAdapter(dict[str,str]).validate_python(capacity["output"])
                return ProductionAccountObservation(account_id=self.account_id,mode=receipts[0].mode,started_at=receipts[0].started_at,observed_at=capacity_receipt.observed_at,receipts=(*receipts,capacity_receipt),holdings=tuple(holdings),deposit_balance=_amount(summary["dnca_tot_amt"]),total_equity=_amount(summary["tot_evlu_amt"]),securities_value=_amount(summary["scts_evlu_amt"]),cash_orderable=_amount(output["ord_psbl_cash"]),no_margin_buy_amount=_amount(output["nrcvb_buy_amt"]),no_margin_buy_quantity=_quantity(output["nrcvb_buy_qty"]),capacity_symbol=symbol,capacity_price=price,credit_amount=_amount(summary["fncg_amt"]) if "fncg_amt" in summary else None)
            except (ValueError, KeyError, TypeError):
                raise ReadOnlyError("account observation is incomplete or inconsistent") from None