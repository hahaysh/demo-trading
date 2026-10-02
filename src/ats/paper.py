"""Durable paper-order reservations and reconciliation; deliberately no broker I/O."""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from ats.domain.execution import OrderIntent, OrderSide, RiskDecision, RiskOutcome
from ats.domain.governance import evidence_digest
from ats.domain.policy import RiskPolicy
from ats.domain.strategy import ArtifactRef, FrozenModel, Identifier
from ats.risk.assessor import RiskState, assess_limit_intent


class PaperLedgerError(ValueError):
    """An intent or state transition cannot safely proceed."""


class PaperOrderStatus(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


_TERMINAL = {
    PaperOrderStatus.FILLED,
    PaperOrderStatus.CANCELED,
    PaperOrderStatus.REJECTED,
    PaperOrderStatus.EXPIRED,
}


class PaperOrderRecord(FrozenModel):
    intent: OrderIntent
    decision: RiskDecision
    status: PaperOrderStatus = PaperOrderStatus.PREPARED
    fee_reserve_bps: Annotated[int, Field(strict=True, ge=0, le=1000)]
    filled_quantity: Annotated[int, Field(strict=True, ge=0)] = 0
    broker_order_id: Identifier | None = None
    updated_at: AwareDatetime
    reconciliation: ArtifactRef | None = None
    transmission_started: bool = False
    recovery_approval: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if (
            self.decision.outcome is not RiskOutcome.ALLOW
            or self.decision.intent_id != self.intent.intent_id
            or self.decision.intent_digest != evidence_digest(self.intent)
            or self.filled_quantity > self.intent.quantity
            or self.updated_at < self.intent.created_at
        ):
            raise ValueError("paper record does not match approved intent")
        return self

    @property
    def remaining_quantity(self) -> int:
        return (
            0
            if self.status in _TERMINAL
            else self.intent.quantity - self.filled_quantity
        )


class PaperBrokerUpdate(FrozenModel):
    broker_environment: Literal["KIS_PAPER"] = "KIS_PAPER"
    account_id: Identifier
    client_order_id: Identifier
    status: Literal["ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED"]
    filled_quantity: Annotated[int, Field(strict=True, ge=0)]
    broker_order_id: Identifier
    observed_at: AwareDatetime
    evidence: ArtifactRef


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise PaperLedgerError("paper ledger requires an aware timestamp")
    return at.astimezone(UTC)


class PaperOrderLedger:
    """Trusted local store. Callers must authenticate account and broker evidence."""

    def __init__(self, database: Path) -> None:
        self.database = database.absolute()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        for path in (self.database, *self.database.parents):
            if path.is_symlink():
                raise PaperLedgerError("ledger links are forbidden")
            if path.exists():
                attributes: int = getattr(path.lstat(), "st_file_attributes", 0)
                if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise PaperLedgerError("ledger reparse points are forbidden")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=5)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS orders (account_id TEXT, client_id TEXT, "
                "intent_id TEXT UNIQUE, record TEXT NOT NULL, PRIMARY KEY(account_id, client_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS halts (account_id TEXT PRIMARY KEY, reason TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cancellation_claims (account_id TEXT, client_id TEXT, "
                "state TEXT NOT NULL, PRIMARY KEY(account_id,client_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, "
                "account_id TEXT NOT NULL, client_id TEXT NOT NULL, record TEXT NOT NULL)"
            )
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _get(
        connection: sqlite3.Connection, account: str, client_id: str
    ) -> PaperOrderRecord:
        row = connection.execute(
            "SELECT record FROM orders WHERE account_id=? AND client_id=?",
            (account, client_id),
        ).fetchone()
        if row is None:
            raise PaperLedgerError("unknown paper order")
        return PaperOrderRecord.model_validate_json(row[0])

    @staticmethod
    def _write(connection: sqlite3.Connection, record: PaperOrderRecord) -> None:
        encoded = record.model_dump_json()
        connection.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?) ON CONFLICT(account_id, client_id) "
            "DO UPDATE SET record=excluded.record",
            (
                record.intent.account_id,
                record.intent.client_order_id,
                str(record.intent.intent_id),
                encoded,
            ),
        )
        connection.execute(
            "INSERT INTO events(account_id, client_id, record) VALUES (?, ?, ?)",
            (record.intent.account_id, record.intent.client_order_id, encoded),
        )

    @staticmethod
    def _state(
        connection: sqlite3.Connection, state: RiskState, *, exclude: str | None = None
    ) -> RiskState:
        state = RiskState.model_validate(state.model_dump())
        if (
            state.reserved_cash
            or state.reserved_buy_notional
            or state.reserved_sell_quantity
        ):
            raise PaperLedgerError("external reservations need explicit reconciliation")
        if connection.execute(
            "SELECT 1 FROM halts WHERE account_id=?", (state.account_id,)
        ).fetchone():
            raise PaperLedgerError(
                "account is halted; authenticated operator recovery required"
            )
        if connection.execute(
            "SELECT 1 FROM cancellation_claims WHERE account_id=? AND state='PENDING'",
            (state.account_id,),
        ).fetchone():
            raise PaperLedgerError("unresolved cancellation blocks new work")
        records = [
            PaperOrderRecord.model_validate_json(row[0])
            for row in connection.execute(
                "SELECT record FROM orders WHERE account_id=?", (state.account_id,)
            )
        ]
        cash, notional, sells = Decimal(0), Decimal(0), 0
        for record in records:
            if record.status in (PaperOrderStatus.SUBMITTING, PaperOrderStatus.UNKNOWN):
                raise PaperLedgerError(
                    "pending submission must be reconciled before new work"
                )
            if record.filled_quantity and state.observed_at <= record.updated_at:
                raise PaperLedgerError(
                    "fresh reconciled account state required after fills"
                )
            if (
                record.intent.client_order_id == exclude
                or not record.remaining_quantity
            ):
                continue
            price = record.intent.limit_price
            if price is None:
                raise PaperLedgerError("ledger only supports limit orders")
            value = price * record.remaining_quantity
            cash += value * record.fee_reserve_bps / 10000
            if record.intent.side is OrderSide.BUY:
                cash += value
                notional += value
            elif record.intent.instrument_id == state.instrument_id:
                sells += record.remaining_quantity
        return RiskState.model_validate(
            {
                **state.model_dump(),
                "reserved_cash": cash,
                "reserved_buy_notional": notional,
                "reserved_sell_quantity": sells,
            }
        )

    def prepare(
        self,
        intent: OrderIntent,
        policy: RiskPolicy,
        state: RiskState,
        *,
        at: datetime,
        assessor: ArtifactRef,
    ) -> PaperOrderRecord:
        intent = OrderIntent.model_validate(intent.model_dump())
        at = _utc(at)
        self._persist_risk_stop(policy, state)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT record FROM orders WHERE account_id=? AND client_id=?",
                (intent.account_id, intent.client_order_id),
            ).fetchone()
            if row is not None:
                previous = PaperOrderRecord.model_validate_json(row[0])
                if previous.intent != intent:
                    raise PaperLedgerError(
                        "client order ID conflicts with an existing intent"
                    )
                return previous
            checked = self._state(connection, state)
            decision = assess_limit_intent(
                intent, policy, checked, at=at, assessor=assessor
            )
            decision.validate_for_intent(intent, policy, at=at)
            record = PaperOrderRecord(
                intent=intent,
                decision=decision,
                fee_reserve_bps=checked.fee_reserve_bps,
                updated_at=at,
            )
            self._write(connection, record)
            return record

    def claim_submission(
        self,
        account_id: str,
        client_order_id: str,
        policy: RiskPolicy,
        state: RiskState,
        *,
        at: datetime,
        assessor: ArtifactRef,
    ) -> PaperOrderRecord:
        at = _utc(at)
        self._persist_risk_stop(policy, state)
        with self._transaction() as connection:
            record = self._get(connection, account_id, client_order_id)
            if record.status is not PaperOrderStatus.PREPARED or at < record.updated_at:
                raise PaperLedgerError(
                    "submission already claimed or timestamp regressed"
                )
            checked = self._state(connection, state, exclude=client_order_id)
            decision = assess_limit_intent(
                record.intent, policy, checked, at=at, assessor=assessor
            )
            decision.validate_for_intent(record.intent, policy, at=at)
            claimed = PaperOrderRecord.model_validate(
                {
                    **record.model_dump(),
                    "decision": decision,
                    "status": "SUBMITTING",
                    "updated_at": at,
                    "fee_reserve_bps": checked.fee_reserve_bps,
                }
            )
            self._write(connection, claimed)
            return claimed

    def start_transmission(
        self, account_id: str, client_order_id: str, policy: RiskPolicy, *, at: datetime
    ) -> PaperOrderRecord:
        at = _utc(at)
        with self._transaction() as connection:
            record = self._get(connection, account_id, client_order_id)
            if connection.execute(
                "SELECT 1 FROM halts WHERE account_id=?", (account_id,)
            ).fetchone():
                raise PaperLedgerError("account halted before transmission")
            if connection.execute(
                "SELECT 1 FROM cancellation_claims WHERE account_id=? AND state='PENDING'",
                (account_id,),
            ).fetchone():
                raise PaperLedgerError("cancellation pending before transmission")
            if (
                record.status is not PaperOrderStatus.SUBMITTING
                or record.transmission_started
                or at < record.updated_at
            ):
                raise PaperLedgerError(
                    "transmission is unavailable or already consumed"
                )
            record.decision.validate_for_intent(record.intent, policy, at=at)
            started = PaperOrderRecord.model_validate(
                {**record.model_dump(), "transmission_started": True, "updated_at": at}
            )
            self._write(connection, started)
            return started

    def claim_cancellation(
        self, account_id: str, client_order_id: str, *, at: datetime
    ) -> PaperOrderRecord:
        at = _utc(at)
        with self._transaction() as connection:
            record = self._get(connection, account_id, client_order_id)
            if (
                record.status
                not in (PaperOrderStatus.ACCEPTED, PaperOrderStatus.PARTIALLY_FILLED)
                or at < record.updated_at
            ):
                raise PaperLedgerError("only a confirmed open order can be canceled")
            if connection.execute(
                "SELECT 1 FROM cancellation_claims WHERE account_id=? AND client_id=?",
                (account_id, client_order_id),
            ).fetchone():
                raise PaperLedgerError(
                    "cancellation already claimed; reconcile instead of resending"
                )
            connection.execute(
                "INSERT INTO cancellation_claims VALUES (?, ?, 'PENDING')",
                (account_id, client_order_id),
            )
            return record

    def halt(self, account_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO halts VALUES (?, 'BROKER_RECONCILIATION')",
                (account_id,),
            )

    def _persist_risk_stop(self, policy: RiskPolicy, state: RiskState) -> None:
        policy = RiskPolicy.model_validate(policy.model_dump())
        state = RiskState.model_validate(state.model_dump())
        if (
            state.kill_switch_active
            or state.daily_loss_fraction
            >= Decimal(str(policy.limits.daily_portfolio_loss_halt))
            or state.drawdown_fraction
            >= Decimal(str(policy.limits.portfolio_drawdown_halt))
        ):
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO halts VALUES (?, 'RISK_STOP') ON CONFLICT(account_id) DO UPDATE SET reason='RISK_STOP'",
                    (state.account_id,),
                )
            raise PaperLedgerError(
                "risk policy denies submission; persistent risk halt requires operator review"
            )

    def record_recovery(
        self,
        account_id: str,
        client_order_id: str,
        approval: ArtifactRef,
        *,
        resume: bool,
    ) -> PaperOrderRecord:
        approval = ArtifactRef.model_validate(approval.model_dump())
        with self._transaction() as connection:
            record = self._get(connection, account_id, client_order_id)
            if (
                record.status
                in (
                    PaperOrderStatus.PREPARED,
                    PaperOrderStatus.SUBMITTING,
                    PaperOrderStatus.UNKNOWN,
                )
                or record.reconciliation is None
            ):
                raise PaperLedgerError(
                    "recovery requires confirmed broker reconciliation"
                )
            if resume:
                halt = connection.execute(
                    "SELECT reason FROM halts WHERE account_id=?", (account_id,)
                ).fetchone()
                if halt is not None and halt[0] == "RISK_STOP":
                    raise PaperLedgerError(
                        "broker recovery cannot clear a portfolio risk stop"
                    )
                records = [
                    PaperOrderRecord.model_validate_json(row[0])
                    for row in connection.execute(
                        "SELECT record FROM orders WHERE account_id=?", (account_id,)
                    )
                ]
                if any(
                    item.status
                    in (PaperOrderStatus.SUBMITTING, PaperOrderStatus.UNKNOWN)
                    for item in records
                ):
                    raise PaperLedgerError("other uncertain orders block recovery")
                if connection.execute(
                    "SELECT 1 FROM cancellation_claims WHERE account_id=? AND state='PENDING'",
                    (account_id,),
                ).fetchone():
                    raise PaperLedgerError("uncertain cancellation blocks recovery")
                connection.execute(
                    "DELETE FROM halts WHERE account_id=?", (account_id,)
                )
            recovered = PaperOrderRecord.model_validate(
                {**record.model_dump(), "recovery_approval": approval}
            )
            self._write(connection, recovered)
            return recovered

    def expire_prepared(self, account_id: str, *, at: datetime) -> int:
        at = _utc(at)
        with self._transaction() as connection:
            records = [
                PaperOrderRecord.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT record FROM orders WHERE account_id=?", (account_id,)
                )
            ]
            expired = 0
            for record in records:
                if (
                    record.status is PaperOrderStatus.PREPARED
                    and record.intent.expires_at <= at
                    and record.updated_at <= at
                ):
                    self._write(
                        connection,
                        PaperOrderRecord.model_validate(
                            {
                                **record.model_dump(),
                                "status": "EXPIRED",
                                "updated_at": at,
                            }
                        ),
                    )
                    expired += 1
            return expired

    def mark_unknown(
        self, account_id: str, client_order_id: str, *, at: datetime
    ) -> None:
        at = _utc(at)
        with self._transaction() as connection:
            record = self._get(connection, account_id, client_order_id)
            if (
                record.status
                not in (PaperOrderStatus.SUBMITTING, PaperOrderStatus.UNKNOWN)
                or at < record.updated_at
            ):
                raise PaperLedgerError("only unresolved submissions can become unknown")
            unknown = PaperOrderRecord.model_validate(
                {
                    **record.model_dump(),
                    "status": "UNKNOWN",
                    "updated_at": at,
                }
            )
            self._write(connection, unknown)
            connection.execute(
                "INSERT OR IGNORE INTO halts VALUES (?, ?)",
                (account_id, "UNKNOWN_SUBMISSION"),
            )

    def reconcile(self, update: PaperBrokerUpdate) -> PaperOrderRecord:
        update = PaperBrokerUpdate.model_validate(update.model_dump())
        with self._transaction() as connection:
            record = self._get(connection, update.account_id, update.client_order_id)
            if record.reconciliation == update.evidence:
                if (
                    record.status.value == update.status
                    and record.filled_quantity == update.filled_quantity
                    and record.broker_order_id == update.broker_order_id
                    and record.updated_at == update.observed_at
                ):
                    return record
                raise PaperLedgerError(
                    "reconciliation evidence reused with different data"
                )
            if (
                record.status in _TERMINAL
                and record.status.value == update.status
                and record.filled_quantity == update.filled_quantity
                and record.broker_order_id == update.broker_order_id
                and update.observed_at >= record.updated_at
            ):
                return record
            if (
                record.status is PaperOrderStatus.PREPARED
                or record.status in _TERMINAL
                or update.observed_at < record.updated_at
                or not record.filled_quantity
                <= update.filled_quantity
                <= record.intent.quantity
                or (
                    record.broker_order_id is not None
                    and record.broker_order_id != update.broker_order_id
                )
                or (
                    update.status == "FILLED"
                    and update.filled_quantity != record.intent.quantity
                )
                or (
                    update.status != "FILLED"
                    and update.filled_quantity == record.intent.quantity
                )
                or (update.status == "PARTIALLY_FILLED" and update.filled_quantity == 0)
                or (
                    update.status in ("ACCEPTED", "REJECTED")
                    and update.filled_quantity != 0
                )
            ):
                raise PaperLedgerError("invalid or regressive broker reconciliation")
            reconciled = PaperOrderRecord.model_validate(
                {
                    **record.model_dump(),
                    "status": update.status,
                    "filled_quantity": update.filled_quantity,
                    "broker_order_id": update.broker_order_id,
                    "updated_at": _utc(update.observed_at),
                    "reconciliation": update.evidence,
                }
            )
            self._write(connection, reconciled)
            if reconciled.status in _TERMINAL:
                connection.execute(
                    "UPDATE cancellation_claims SET state='RESOLVED' WHERE account_id=? AND client_id=?",
                    (update.account_id, update.client_order_id),
                )
            return reconciled

    def get(self, account_id: str, client_order_id: str) -> PaperOrderRecord:
        with self._transaction() as connection:
            return self._get(connection, account_id, client_order_id)

    def account_orders(self, account_id: str) -> tuple[PaperOrderRecord, ...]:
        with self._transaction() as connection:
            return tuple(
                PaperOrderRecord.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT record FROM orders WHERE account_id=? ORDER BY client_id",
                    (account_id,),
                )
            )
