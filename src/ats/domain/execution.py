"""Paper order and risk receipts, not broker submission capabilities."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ats.domain.governance import evidence_digest
from ats.domain.policy import PolicyStatus, RiskPolicy
from ats.domain.strategy import (
    ArtifactRef,
    AssetClass,
    DatasetSnapshotRef,
    FrozenModel,
    Identifier,
    NonEmptyText,
    PolicyRef,
    Sha256Digest,
)


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderIntent(FrozenModel):
    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["OrderIntent"] = "OrderIntent"
    intent_id: UUID
    client_order_id: Identifier
    account_id: Identifier
    broker_environment: Literal["KIS_PAPER"] = "KIS_PAPER"
    market: Literal["KRX"] = "KRX"
    currency: Literal["KRW"] = "KRW"
    funding: Literal["CASH"] = "CASH"
    position_scope: Literal["LONG_ONLY"] = "LONG_ONLY"
    strategy_version_id: UUID
    strategy_digest: Sha256Digest
    champion_selection: ArtifactRef
    dataset_snapshot: DatasetSnapshotRef
    signal: ArtifactRef
    risk_policy: PolicyRef
    instrument_id: Identifier
    asset_class: AssetClass
    side: OrderSide
    order_type: OrderType
    quantity: Annotated[int, Field(strict=True, gt=0)]
    limit_price: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None = None
    signal_session: date
    execution_session: date
    execution_phase: Literal["NEXT_SESSION_OPEN"] = "NEXT_SESSION_OPEN"
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require a positive limit price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market orders cannot carry a limit price")
        if self.execution_session <= self.signal_session:
            raise ValueError("execution session must follow signal session")
        if self.created_at < self.dataset_snapshot.observed_through:
            raise ValueError("intent cannot precede snapshot freeze")
        if self.expires_at <= self.created_at:
            raise ValueError("intent expiry must follow creation")
        return self


class RiskOutcome(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class RiskCheck(StrEnum):
    CHAMPION = "CHAMPION"
    UNIVERSE = "UNIVERSE"
    PRICE_FRESHNESS = "PRICE_FRESHNESS"
    SYMBOL_WEIGHT = "SYMBOL_WEIGHT"
    GROSS_EXPOSURE = "GROSS_EXPOSURE"
    CASH_AVAILABLE = "CASH_AVAILABLE"
    LONG_ONLY = "LONG_ONLY"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    MARKET_SESSION = "MARKET_SESSION"
    DAILY_LOSS = "DAILY_LOSS"
    DRAWDOWN = "DRAWDOWN"
    KILL_SWITCH = "KILL_SWITCH"


class RiskCheckStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class RiskCheckEvidence(FrozenModel):
    check: RiskCheck
    status: RiskCheckStatus
    report: ArtifactRef


class RiskDecision(FrozenModel):
    """An independent assessor's claimed result; identity is verified elsewhere."""

    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["RiskDecision"] = "RiskDecision"
    decision_id: UUID
    intent_id: UUID
    intent_digest: Sha256Digest
    policy: PolicyRef
    assessor: ArtifactRef
    outcome: RiskOutcome = RiskOutcome.DENY
    reason: NonEmptyText
    checked_at: AwareDatetime
    expires_at: AwareDatetime
    checks: tuple[RiskCheckEvidence, ...] = ()

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if self.expires_at <= self.checked_at:
            raise ValueError("risk decision expiry must follow assessment")
        names = [item.check for item in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("risk checks must be unique")
        if self.outcome is RiskOutcome.ALLOW and (
            set(names) != set(RiskCheck)
            or any(item.status is not RiskCheckStatus.PASSED for item in self.checks)
        ):
            raise ValueError("ALLOW requires every risk check to pass")
        return self

    def validate_for_intent(
        self, intent: OrderIntent, policy: RiskPolicy, *, at: datetime
    ) -> None:
        """Check receipt consistency, not live risk, authorization, or replay state."""
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("validation time must be timezone-aware")
        if self.outcome is not RiskOutcome.ALLOW:
            raise ValueError("risk decision denies the intent")
        if self.intent_id != intent.intent_id or self.intent_digest != evidence_digest(
            intent
        ):
            raise ValueError("risk decision does not match the exact intent")
        if (
            self.policy != intent.risk_policy
            or self.policy.policy_id != policy.metadata.policy_id
            or self.policy.version != policy.metadata.version
            or self.policy.digest != evidence_digest(policy)
        ):
            raise ValueError("risk policy reference mismatch")
        if (
            policy.metadata.status is not PolicyStatus.APPROVED
            or policy.metadata.approved_at is None
            or policy.metadata.approved_at > self.checked_at
        ):
            raise ValueError("risk policy must be approved before assessment")
        if intent.asset_class not in policy.scope.allowed_asset_classes:
            raise ValueError("asset class is not allowed by risk policy")
        if self.checked_at < intent.created_at or self.expires_at > intent.expires_at:
            raise ValueError("risk decision validity is outside intent lifetime")
        if not self.checked_at <= at < self.expires_at:
            raise ValueError("risk decision is not yet valid or has expired")
