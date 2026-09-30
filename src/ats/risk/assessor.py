"""Numerical paper-limit risk checks over an externally verified account snapshot."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field

from ats.domain.execution import (
    OrderIntent,
    OrderSide,
    OrderType,
    RiskCheck,
    RiskCheckEvidence,
    RiskCheckStatus,
    RiskDecision,
    RiskOutcome,
)
from ats.domain.governance import evidence_digest
from ats.domain.policy import PolicyStatus, RiskPolicy
from ats.domain.prices import SEOUL
from ats.domain.strategy import (
    ArtifactRef,
    FrozenModel,
    Identifier,
    PolicyRef,
    Sha256Digest,
)

Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class RiskState(FrozenModel):
    account_id: Identifier
    instrument_id: Identifier
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    equity: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    cash: Money
    gross_exposure: Money
    reserved_buy_notional: Money
    reserved_cash: Money
    held_quantity: Annotated[int, Field(strict=True, ge=0)]
    reserved_sell_quantity: Annotated[int, Field(strict=True, ge=0)]
    quote_price: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    quote_at: AwareDatetime
    daily_loss_fraction: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    drawdown_fraction: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    champion_version_id: UUID
    champion_digest: Sha256Digest
    champion_selection: ArtifactRef
    universe_members: tuple[Identifier, ...]
    duplicate_client_order_id: Annotated[bool, Field(strict=True)]
    kill_switch_active: Annotated[bool, Field(strict=True)]
    opening_window_start: AwareDatetime
    opening_window_end: AwareDatetime
    fee_reserve_bps: Annotated[int, Field(strict=True, ge=0, le=1000)]


def assess_limit_intent(
    intent: OrderIntent,
    policy: RiskPolicy,
    state: RiskState,
    *,
    at: datetime,
    assessor: ArtifactRef,
) -> RiskDecision:
    """Compute every check. Caller still must authenticate state and atomically reserve funds."""
    intent = OrderIntent.model_validate(intent.model_dump())
    policy = RiskPolicy.model_validate(policy.model_dump())
    state = RiskState.model_validate(state.model_dump())
    assessor = ArtifactRef.model_validate(assessor.model_dump())
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("assessment time must be timezone-aware")
    at = at.astimezone(UTC)
    if intent.order_type is not OrderType.LIMIT or intent.limit_price is None:
        raise ValueError(
            "independent assessor currently supports paper LIMIT orders only"
        )
    reference = PolicyRef(
        policy_id=policy.metadata.policy_id,
        version=policy.metadata.version,
        digest=evidence_digest(policy),
    )
    if (
        intent.risk_policy != reference
        or policy.metadata.status is not PolicyStatus.APPROVED
    ):
        raise ValueError("risk policy is mismatched or unapproved")
    if policy.metadata.approved_at is None or policy.metadata.approved_at > at:
        raise ValueError("risk policy approval is not yet effective")
    if (
        state.account_id != intent.account_id
        or state.instrument_id != intent.instrument_id
    ):
        raise ValueError("account or instrument state mismatch")
    if (
        not intent.created_at <= at < intent.expires_at
        or not state.observed_at <= at < state.valid_until
    ):
        raise ValueError("intent or account state is expired or future")
    if (
        state.cash + state.gross_exposure != state.equity
        or state.gross_exposure < state.held_quantity * state.quote_price
    ):
        raise ValueError("inconsistent cash-only portfolio valuation")
    expires = min(
        intent.expires_at,
        state.valid_until,
        state.quote_at + timedelta(seconds=policy.limits.max_price_age_seconds),
    )
    notional = intent.limit_price * intent.quantity
    fee_reserve = notional * state.fee_reserve_bps / 10000
    projected_equity = state.equity - fee_reserve
    buy = intent.side is OrderSide.BUY
    projected_symbol = state.held_quantity * state.quote_price + (
        notional if buy else -intent.quantity * state.quote_price
    )
    projected_gross = (
        state.gross_exposure
        + state.reserved_buy_notional
        + (notional if buy else -intent.quantity * state.quote_price)
    )
    quote_age = (at - state.quote_at).total_seconds()
    checks = {
        RiskCheck.CHAMPION: intent.strategy_version_id == state.champion_version_id
        and intent.strategy_digest == state.champion_digest
        and intent.champion_selection == state.champion_selection,
        RiskCheck.UNIVERSE: intent.instrument_id in state.universe_members
        and intent.asset_class in policy.scope.allowed_asset_classes,
        RiskCheck.PRICE_FRESHNESS: 0 <= quote_age < policy.limits.max_price_age_seconds,
        RiskCheck.SYMBOL_WEIGHT: projected_equity > 0
        and 0
        <= projected_symbol + state.reserved_buy_notional
        <= projected_equity * Decimal(str(policy.limits.max_symbol_weight)),
        RiskCheck.GROSS_EXPOSURE: projected_equity > 0
        and 0
        <= projected_gross
        <= projected_equity * Decimal(str(policy.limits.max_gross_exposure)),
        RiskCheck.CASH_AVAILABLE: state.cash - state.reserved_cash
        >= (notional + fee_reserve if buy else fee_reserve),
        RiskCheck.LONG_ONLY: buy
        or intent.quantity <= state.held_quantity - state.reserved_sell_quantity,
        RiskCheck.DUPLICATE_ORDER: not state.duplicate_client_order_id,
        RiskCheck.MARKET_SESSION: state.opening_window_start
        <= at
        < state.opening_window_end
        and at.astimezone(SEOUL).date() == intent.execution_session,
        RiskCheck.DAILY_LOSS: state.daily_loss_fraction
        < Decimal(str(policy.limits.daily_portfolio_loss_halt)),
        RiskCheck.DRAWDOWN: state.drawdown_fraction
        < Decimal(str(policy.limits.portfolio_drawdown_halt)),
        RiskCheck.KILL_SWITCH: not state.kill_switch_active,
    }
    failed = [check.value for check, passed in checks.items() if not passed]
    report = ArtifactRef(
        artifact_id="risk-state", version="1", digest=evidence_digest(state)
    )
    decision = RiskDecision(
        decision_id=uuid5(
            NAMESPACE_URL, f"{evidence_digest(intent)}:{report.digest}:{at.isoformat()}"
        ),
        intent_id=intent.intent_id,
        intent_digest=evidence_digest(intent),
        policy=reference,
        assessor=assessor,
        outcome=RiskOutcome.DENY if failed else RiskOutcome.ALLOW,
        reason=", ".join(failed)
        if failed
        else "All independent numerical checks passed.",
        checked_at=at,
        expires_at=expires if expires > at else at + timedelta(microseconds=1),
        checks=tuple(
            RiskCheckEvidence(
                check=check,
                status=RiskCheckStatus.PASSED if passed else RiskCheckStatus.FAILED,
                report=report,
            )
            for check, passed in checks.items()
        ),
    )
    if not failed:
        decision.validate_for_intent(intent, policy, at=at)
    return decision
