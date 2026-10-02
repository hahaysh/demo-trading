"""Fixed-host KIS paper transport; real network is disarmed by default."""

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

import httpx
from pydantic import AwareDatetime, Field, SecretStr, TypeAdapter, ValidationError

from ats.domain.execution import OrderIntent
from ats.domain.governance import evidence_digest
from ats.domain.policy import RiskPolicy
from ats.domain.prices import SEOUL
from ats.domain.strategy import ArtifactRef, FrozenModel, Identifier, Sha256Digest
from ats.paper import (
    PaperBrokerUpdate,
    PaperLedgerError,
    PaperOrderLedger,
    PaperOrderRecord,
    PaperOrderStatus,
)
from ats.risk.assessor import RiskState

ORIGIN = "https://openapivts.koreainvestment.com:29443"
_TRADING = "/uapi/domestic-stock/v1/trading/"
_ROUTES = {
    ("GET", "inquire-balance", "VTTC8434R"),
    ("GET", "inquire-psbl-order", "VTTC8908R"),
    ("GET", "inquire-daily-ccld", "VTTC0081R"),
    ("POST", "order-cash", "VTTC0012U"),
    ("POST", "order-cash", "VTTC0011U"),
    ("POST", "order-rvsecncl", "VTTC0013U"),
}
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
_OBJECT = TypeAdapter(dict[str, Any])
_ROWS = TypeAdapter(list[dict[str, Any]])


class PaperTransportError(ValueError):
    pass


class PaperRejectedError(PaperTransportError):
    def __init__(self, evidence: ArtifactRef) -> None:
        super().__init__("paper broker rejected request")
        self.evidence = evidence


class PaperCredentials(FrozenModel):
    app_key: SecretStr
    app_secret: SecretStr
    account_number: SecretStr
    product_code: Annotated[str, Field(pattern="^[0-9]{2}$")]
    account_id: Identifier


class PaperHolding(FrozenModel):
    symbol: Annotated[str, Field(pattern="^[A-Z0-9]{6}$")]
    quantity: Annotated[int, Field(strict=True, ge=0)]
    sellable: Annotated[int, Field(strict=True, ge=0)]
    value: Money


class PaperAccount(FrozenModel):
    account_id: Identifier
    observed_at: AwareDatetime
    cash: Money
    equity: Money
    gross_exposure: Money
    holdings: tuple[PaperHolding, ...]


class PaperAck(FrozenModel):
    order_id: Identifier
    organization: Identifier
    observed_at: AwareDatetime
    evidence: ArtifactRef


def _money(value: object) -> Decimal:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None
    ):
        raise PaperTransportError("invalid paper numeric field")
    return Decimal(value)


def _integer(value: object) -> int:
    result = _money(value)
    if result != result.to_integral_value():
        raise PaperTransportError("paper quantity is not an integer")
    return int(result)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PaperTransportError("duplicate paper JSON field")
        result[key] = value
    return result


def _evidence(payload: bytes, name: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=name,
        version="1",
        digest="sha256:" + hashlib.sha256(payload).hexdigest(),
    )


class KisPaperClient:
    def __init__(
        self,
        credentials: PaperCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        allow_network: bool = False,
        clock: Callable[[], datetime] | None = None,
        max_pages: int = 20,
    ) -> None:
        credentials = PaperCredentials.model_validate(credentials.model_dump())
        if (
            not credentials.app_key.get_secret_value()
            or not credentials.app_secret.get_secret_value()
            or re.fullmatch(r"[0-9]{8}", credentials.account_number.get_secret_value())
            is None
        ):
            raise PaperTransportError("invalid paper credentials or account binding")
        if not allow_network and not isinstance(transport, httpx.MockTransport):
            raise PaperTransportError("real paper network is disarmed")
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise PaperTransportError("bounded page budget required")
        self.credentials = credentials
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_pages = max_pages
        self._client = httpx.Client(
            transport=transport, timeout=10, follow_redirects=False, trust_env=False
        )
        self._token: SecretStr | None = None
        self._expiry: datetime | None = None
        self._lock = threading.RLock()
        self._last_request = 0.0
        self._interval = 0.0 if isinstance(transport, httpx.MockTransport) else 1.0

    def close(self) -> None:
        self._token = None
        self._client.close()

    def now(self) -> datetime:
        return self._now()

    def symbol_for(self, intent: OrderIntent) -> str:
        return self._symbol(intent)

    def send_claimed(
        self,
        ledger: PaperOrderLedger,
        policy: RiskPolicy,
        account_id: str,
        client_order_id: str,
    ) -> PaperAck:
        record = ledger.start_transmission(
            account_id, client_order_id, policy, at=self._now()
        )
        return self._submit(record.intent)

    def send_cancellation(
        self,
        ledger: PaperOrderLedger,
        account_id: str,
        client_order_id: str,
        organization: str,
    ) -> PaperAck:
        record = ledger.claim_cancellation(account_id, client_order_id, at=self._now())
        return self._cancel(record, organization, record.remaining_quantity)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PaperTransportError("aware broker clock required")
        return now.astimezone(UTC)

    def _http(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[dict[str, Any], httpx.Headers, bytes]:
        with self._lock:
            delay = self._last_request + self._interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            try:
                with self._client.stream(method, ORIGIN + path, **kwargs) as response:
                    if response.status_code == 401:
                        self._token = None
                    if not 200 <= response.status_code < 300:
                        raise PaperTransportError(
                            "paper HTTP request failed; no automatic replay"
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        data.extend(chunk)
                        if len(data) > 2 * 1024 * 1024:
                            raise PaperTransportError(
                                "paper response exceeds byte budget"
                            )
                    raw = bytes(data)
                    body = _OBJECT.validate_python(
                        json.loads(raw, object_pairs_hook=_unique)
                    )
                    return body, response.headers, raw
            except (
                httpx.HTTPError,
                UnicodeError,
                json.JSONDecodeError,
                ValidationError,
            ):
                raise PaperTransportError(
                    "paper transport failed; outcome must be reconciled"
                ) from None

    def _authenticate(self) -> str:
        now = self._now()
        if self._token is None or self._expiry is None or now >= self._expiry:
            body, _, _ = self._http(
                "POST",
                "/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.credentials.app_key.get_secret_value(),
                    "appsecret": self.credentials.app_secret.get_secret_value(),
                },
            )
            token = body.get("access_token")
            lifetime = body.get("expires_in")
            if (
                not isinstance(token, str)
                or not token
                or type(lifetime) is not int
                or not 60 < lifetime <= 86400
            ):
                raise PaperTransportError("invalid paper token receipt")
            expiry = now + timedelta(seconds=lifetime - 60)
            absolute = body.get("access_token_token_expired")
            if absolute is not None:
                try:
                    parsed = (
                        datetime.strptime(absolute, "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=SEOUL)
                        .astimezone(UTC)
                    )
                except (TypeError, ValueError):
                    raise PaperTransportError("invalid absolute token expiry") from None
                expiry = min(expiry, parsed - timedelta(seconds=60))
            if expiry <= now:
                raise PaperTransportError("expired paper token receipt")
            self._token, self._expiry = SecretStr(token), expiry
        return self._token.get_secret_value()

    def _request(
        self,
        method: str,
        route: str,
        transaction: str,
        parameters: dict[str, str],
        *,
        continuation: str = "",
    ) -> tuple[dict[str, Any], httpx.Headers, bytes]:
        if (method, route, transaction) not in _ROUTES:
            raise PaperTransportError("paper route is not allowlisted")
        with self._lock:
            headers = {
                "authorization": "Bearer " + self._authenticate(),
                "appkey": self.credentials.app_key.get_secret_value(),
                "appsecret": self.credentials.app_secret.get_secret_value(),
                "tr_id": transaction,
                "custtype": "P",
                "tr_cont": continuation,
            }
            parameters = {
                **parameters,
                "CANO": self.credentials.account_number.get_secret_value(),
                "ACNT_PRDT_CD": self.credentials.product_code,
            }
            body, response_headers, raw = self._http(
                method,
                _TRADING + route,
                headers=headers,
                **({"params": parameters} if method == "GET" else {"json": parameters}),
            )
            if body.get("rt_cd") == "1":
                raise PaperRejectedError(_evidence(raw, "paper-rejection"))
            if body.get("rt_cd") != "0":
                raise PaperTransportError("invalid paper result code")
            return body, response_headers, raw

    def _pages(
        self, route: str, transaction: str, parameters: dict[str, str]
    ) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        cursor = ("", "")
        seen: set[tuple[str, str]] = set()
        for index in range(self.max_pages):
            body, headers, _ = self._request(
                "GET",
                route,
                transaction,
                {
                    **parameters,
                    "CTX_AREA_FK100": cursor[0],
                    "CTX_AREA_NK100": cursor[1],
                },
                continuation="N" if index else "",
            )
            try:
                body["output1"] = _ROWS.validate_python(body.get("output1"))
            except ValidationError:
                raise PaperTransportError("missing paper page rows") from None
            pages.append(body)
            if headers.get("tr_cont", "") not in ("F", "M"):
                return pages
            if not isinstance(body.get("ctx_area_fk100"), str) or not isinstance(
                body.get("ctx_area_nk100"), str
            ):
                raise PaperTransportError("invalid pagination cursor")
            cursor = (body["ctx_area_fk100"].strip(), body["ctx_area_nk100"].strip())
            if not any(cursor) or cursor in seen:
                raise PaperTransportError("paper pagination is incomplete or cyclic")
            seen.add(cursor)
        raise PaperTransportError("paper page budget exceeded; no partial result")

    def account(self) -> PaperAccount:
        pages = self._pages(
            "inquire-balance",
            "VTTC8434R",
            {
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
            },
        )
        holdings: list[PaperHolding] = []
        totals: tuple[Decimal, Decimal, Decimal] | None = None
        try:
            for page in pages:
                summary = _ROWS.validate_python(page["output2"])
                if len(summary) != 1:
                    raise PaperTransportError("ambiguous account summary")
                values = tuple(
                    _money(summary[0][key])
                    for key in ("dnca_tot_amt", "tot_evlu_amt", "scts_evlu_amt")
                )
                if totals is not None and values != totals:
                    raise PaperTransportError("account changed during pagination")
                totals = (values[0], values[1], values[2])
                for row in page["output1"]:
                    holdings.append(
                        PaperHolding(
                            symbol=row["pdno"],
                            quantity=_integer(row["hldg_qty"]),
                            sellable=_integer(row["ord_psbl_qty"]),
                            value=_money(row["evlu_amt"]),
                        )
                    )
        except (KeyError, TypeError, IndexError, ValidationError):
            raise PaperTransportError("incomplete paper account response") from None
        if (
            totals is None
            or totals[0] + totals[2] != totals[1]
            or len({holding.symbol for holding in holdings}) != len(holdings)
            or any(holding.sellable > holding.quantity for holding in holdings)
            or sum(holding.value for holding in holdings) != totals[2]
        ):
            raise PaperTransportError(
                "paper account totals or holdings do not reconcile"
            )
        return PaperAccount(
            account_id=self.credentials.account_id,
            observed_at=self._now(),
            cash=totals[0],
            equity=totals[1],
            gross_exposure=totals[2],
            holdings=tuple(holdings),
        )

    def _symbol(self, intent: OrderIntent) -> str:
        if (
            intent.account_id != self.credentials.account_id
            or intent.limit_price is None
        ):
            raise PaperTransportError("paper account mismatch or non-limit intent")
        match = re.fullmatch(r"krx-([A-Z0-9]{6})", intent.instrument_id)
        if match is None:
            raise PaperTransportError("unsupported instrument identifier")
        return match.group(1)

    def cash_capacity(self, intent: OrderIntent) -> tuple[Decimal, int]:
        body, _, _ = self._request(
            "GET",
            "inquire-psbl-order",
            "VTTC8908R",
            {
                "PDNO": self._symbol(intent),
                "ORD_UNPR": "0",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        try:
            output = body["output"]
            return min(
                _money(output["ord_psbl_cash"]), _money(output["nrcvb_buy_amt"])
            ), _integer(output["nrcvb_buy_qty"])
        except (KeyError, TypeError):
            raise PaperTransportError("cash-only capacity is unavailable") from None

    def _submit(self, intent: OrderIntent) -> PaperAck:
        body, _, raw = self._request(
            "POST",
            "order-cash",
            "VTTC0012U" if intent.side.value == "BUY" else "VTTC0011U",
            {
                "PDNO": self._symbol(intent),
                "ORD_DVSN": "00",
                "ORD_QTY": str(intent.quantity),
                "ORD_UNPR": str(intent.limit_price),
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01" if intent.side.value == "SELL" else "",
                "CNDT_PRIC": "",
            },
        )
        try:
            return PaperAck(
                order_id=body["output"]["ODNO"],
                organization=body["output"]["KRX_FWDG_ORD_ORGNO"],
                observed_at=self._now(),
                evidence=_evidence(raw, "paper-submit"),
            )
        except (KeyError, TypeError, ValidationError):
            raise PaperTransportError("paper acknowledgement is incomplete") from None

    def verify_open_orders(
        self, intent: OrderIntent, records: tuple[PaperOrderRecord, ...]
    ) -> None:
        self._symbol(intent)
        expected = {
            record.broker_order_id: record
            for record in records
            if record.status
            in (PaperOrderStatus.ACCEPTED, PaperOrderStatus.PARTIALLY_FILLED)
        }
        if any(
            record.intent.execution_session != intent.execution_session
            for record in expected.values()
        ):
            raise PaperTransportError("prior-session open order must be reconciled")
        pages = self._pages(
            "inquire-daily-ccld",
            "VTTC0081R",
            {
                "INQR_STRT_DT": intent.execution_session.strftime("%Y%m%d"),
                "INQR_END_DT": intent.execution_session.strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "INQR_DVSN": "01",
                "INQR_DVSN_1": "",
                "INQR_DVSN_3": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "EXCG_ID_DVSN_CD": "KRX",
            },
        )
        seen: set[str] = set()
        try:
            for row in (row for page in pages for row in page["output1"]):
                remaining, filled, quantity = (
                    _integer(row["rmn_qty"]),
                    _integer(row["tot_ccld_qty"]),
                    _integer(row["ord_qty"]),
                )
                if row["cncl_yn"] not in ("Y", "N") or filled > quantity:
                    raise PaperTransportError("invalid broker open-order quantities")
                if row["cncl_yn"] == "Y" or remaining == 0:
                    continue
                record = expected.get(row["odno"])
                if (
                    record is None
                    or row["odno"] in seen
                    or remaining != record.remaining_quantity
                    or filled != record.filled_quantity
                    or quantity != record.intent.quantity
                    or row["pdno"] != self._symbol(record.intent)
                    or row["sll_buy_dvsn_cd"]
                    != ("02" if record.intent.side.value == "BUY" else "01")
                    or _money(row["ord_unpr"]) != record.intent.limit_price
                ):
                    raise PaperTransportError("unmanaged or changed broker open order")
                seen.add(row["odno"])
        except (KeyError, TypeError):
            raise PaperTransportError("incomplete broker open-order response") from None
        if seen != set(expected):
            raise PaperTransportError(
                "ledger open order missing from broker; reconcile first"
            )

    def order_status(self, record: PaperOrderRecord) -> tuple[PaperBrokerUpdate, str]:
        intent = record.intent
        if record.broker_order_id is None:
            raise PaperTransportError(
                "unknown broker order ID requires authenticated operator mapping"
            )
        pages = self._pages(
            "inquire-daily-ccld",
            "VTTC0081R",
            {
                "INQR_STRT_DT": intent.execution_session.strftime("%Y%m%d"),
                "INQR_END_DT": intent.execution_session.strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": self._symbol(intent),
                "CCLD_DVSN": "00",
                "INQR_DVSN": "01",
                "INQR_DVSN_1": "",
                "INQR_DVSN_3": "01",
                "ORD_GNO_BRNO": "",
                "ODNO": record.broker_order_id,
                "EXCG_ID_DVSN_CD": "KRX",
            },
        )
        rows = [
            row
            for page in pages
            for row in page["output1"]
            if row.get("odno") == record.broker_order_id
        ]
        if len(rows) != 1:
            raise PaperTransportError("order status is missing or ambiguous")
        row = rows[0]
        try:
            filled, quantity = _integer(row["tot_ccld_qty"]), _integer(row["ord_qty"])
            if (
                row["pdno"] != self._symbol(intent)
                or quantity != intent.quantity
                or filled > quantity
                or row["ord_dt"] != intent.execution_session.strftime("%Y%m%d")
                or row["sll_buy_dvsn_cd"]
                != ("02" if intent.side.value == "BUY" else "01")
                or _money(row["ord_unpr"]) != intent.limit_price
            ):
                raise PaperTransportError("broker order identity mismatch")
            canceled = row["cncl_yn"]
            if canceled not in ("Y", "N"):
                raise PaperTransportError("unknown cancellation state")
            status = (
                "FILLED"
                if filled == quantity
                else "CANCELED"
                if canceled == "Y"
                else "PARTIALLY_FILLED"
                if filled
                else "ACCEPTED"
            )
            if canceled == "N" and _integer(row["rmn_qty"]) != quantity - filled:
                raise PaperTransportError("broker remaining quantity mismatch")
            update = PaperBrokerUpdate.model_validate(
                {
                    "account_id": intent.account_id,
                    "client_order_id": intent.client_order_id,
                    "status": status,
                    "filled_quantity": filled,
                    "broker_order_id": record.broker_order_id,
                    "observed_at": self._now(),
                    "evidence": _evidence(
                        json.dumps(
                            {"row": row, "observed_at": self._now().isoformat()},
                            sort_keys=True,
                        ).encode(),
                        "paper-reconcile",
                    ),
                }
            )
            return update, str(row["ord_gno_brno"])
        except (KeyError, TypeError, ValidationError):
            raise PaperTransportError("incomplete paper order status") from None

    def _cancel(
        self, record: PaperOrderRecord, organization: str, remaining: int
    ) -> PaperAck:
        if remaining <= 0 or record.broker_order_id is None or not organization:
            raise PaperTransportError(
                "cancel requires verified remaining quantity and original order"
            )
        self._symbol(record.intent)
        body, _, raw = self._request(
            "POST",
            "order-rvsecncl",
            "VTTC0013U",
            {
                "KRX_FWDG_ORD_ORGNO": organization,
                "ORGN_ODNO": record.broker_order_id,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": str(remaining),
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "N",
                "EXCG_ID_DVSN_CD": "KRX",
                "CNDT_PRIC": "",
            },
        )
        try:
            return PaperAck(
                order_id=body["output"]["ODNO"],
                organization=body["output"]["KRX_FWDG_ORD_ORGNO"],
                observed_at=self._now(),
                evidence=_evidence(raw, "paper-cancel-ack"),
            )
        except (KeyError, TypeError, ValidationError):
            raise PaperTransportError("cancel acknowledgement is incomplete") from None


class PaperRecovery(FrozenModel):
    account_id: Identifier
    client_order_id: Identifier
    broker_order_id: Identifier
    record_digest: Sha256Digest
    expires_at: AwareDatetime
    approval: ArtifactRef
    resume: bool = False


class PaperExecutionService:
    def __init__(
        self,
        client: KisPaperClient,
        ledger: PaperOrderLedger,
        assessor: ArtifactRef,
        *,
        recovery_authorizer: Callable[[PaperRecovery], bool] | None = None,
    ) -> None:
        self.client, self.ledger, self.assessor = client, ledger, assessor
        self.recovery_authorizer = recovery_authorizer

    def submit(
        self, intent: OrderIntent, policy: RiskPolicy, state: RiskState
    ) -> PaperOrderRecord:
        now = self.client.now()
        self.client.symbol_for(intent)
        prepared = self.ledger.prepare(
            intent, policy, state, at=now, assessor=self.assessor
        )
        if prepared.status is not PaperOrderStatus.PREPARED:
            raise PaperLedgerError(
                "order already processed; reconcile instead of resending"
            )
        try:
            self.client.verify_open_orders(
                intent, self.ledger.account_orders(intent.account_id)
            )
        except Exception:
            self.ledger.halt(intent.account_id)
            raise
        account = self.client.account()
        symbol = self.client.symbol_for(intent)
        holding = next(
            (holding for holding in account.holdings if holding.symbol == symbol), None
        )
        if (
            account.account_id != intent.account_id
            or account.cash != state.cash
            or account.equity != state.equity
            or account.gross_exposure != state.gross_exposure
            or (holding.quantity if holding else 0) != state.held_quantity
        ):
            raise PaperTransportError(
                "independent risk state differs from authenticated account"
            )
        if intent.limit_price is None:
            raise PaperTransportError("only limit intents can be transmitted")
        if intent.side.value == "BUY":
            cash, quantity = self.client.cash_capacity(intent)
            cost = (
                intent.limit_price
                * intent.quantity
                * (1 + Decimal(state.fee_reserve_bps) / 10000)
            )
            if cost > cash or intent.quantity > quantity:
                raise PaperTransportError("cash-only broker capacity is insufficient")
        elif holding is None or intent.quantity > holding.sellable:
            raise PaperTransportError("broker sellable holdings are insufficient")
        self.ledger.claim_submission(
            intent.account_id,
            intent.client_order_id,
            policy,
            state,
            at=self.client.now(),
            assessor=self.assessor,
        )
        try:
            ack = self.client.send_claimed(
                self.ledger, policy, intent.account_id, intent.client_order_id
            )
            return self.ledger.reconcile(
                PaperBrokerUpdate(
                    account_id=intent.account_id,
                    client_order_id=intent.client_order_id,
                    status="ACCEPTED",
                    filled_quantity=0,
                    broker_order_id=ack.order_id,
                    observed_at=ack.observed_at,
                    evidence=ack.evidence,
                )
            )
        except PaperRejectedError as error:
            return self.ledger.reconcile(
                PaperBrokerUpdate(
                    account_id=intent.account_id,
                    client_order_id=intent.client_order_id,
                    status="REJECTED",
                    filled_quantity=0,
                    broker_order_id="local-rejection-" + intent.intent_id.hex,
                    observed_at=self.client.now(),
                    evidence=error.evidence,
                )
            )
        except Exception:
            self.ledger.mark_unknown(
                intent.account_id, intent.client_order_id, at=self.client.now()
            )
            raise

    def reconcile(self, account_id: str, client_order_id: str) -> PaperOrderRecord:
        record = self.ledger.get(account_id, client_order_id)
        try:
            update, _ = self.client.order_status(record)
            return self.ledger.reconcile(update)
        except Exception:
            self.ledger.halt(account_id)
            raise

    def cancel(self, account_id: str, client_order_id: str) -> PaperAck:
        record = self.ledger.get(account_id, client_order_id)
        try:
            update, organization = self.client.order_status(record)
            record = self.ledger.reconcile(update)
        except Exception:
            self.ledger.halt(account_id)
            raise
        try:
            return self.client.send_cancellation(
                self.ledger, account_id, client_order_id, organization
            )
        except Exception:
            self.ledger.halt(account_id)
            raise

    def recover(self, request: PaperRecovery) -> PaperOrderRecord:
        request = PaperRecovery.model_validate(request.model_dump())
        record = self.ledger.get(request.account_id, request.client_order_id)
        now = self.client.now()
        if (
            self.recovery_authorizer is None
            or request.expires_at <= now
            or request.record_digest != evidence_digest(record)
            or self.recovery_authorizer(request) is not True
        ):
            raise PaperLedgerError(
                "explicit authenticated recovery authorization required"
            )
        if (
            record.broker_order_id is not None
            and record.broker_order_id != request.broker_order_id
        ):
            raise PaperLedgerError("recovery cannot replace a known broker identity")
        try:
            update, _ = self.client.order_status(
                record.model_copy(update={"broker_order_id": request.broker_order_id})
            )
            self.ledger.reconcile(update)
            return self.ledger.record_recovery(
                request.account_id,
                request.client_order_id,
                request.approval,
                resume=request.resume,
            )
        except Exception:
            self.ledger.halt(request.account_id)
            raise
