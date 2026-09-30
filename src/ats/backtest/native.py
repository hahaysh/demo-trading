"""Single-instrument trend smoke backtest over verified, point-in-time inputs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from ats.data.artifacts import LocalArtifactResolver
from ats.data.prices import normalize_daily_price
from ats.data.replay import InputReplayRequest, replay_inputs
from ats.domain.governance import evidence_digest
from ats.domain.prices import SEOUL, DailyPrice
from ats.domain.strategy import (
    FrozenModel,
    Identifier,
    Sha256Digest,
    StrategyFamily,
    StrategySpec,
)

NonnegativeMoney = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Bps = Annotated[int, Field(strict=True, ge=0, le=1000)]


class SimulationAssumptions(FrozenModel):
    initial_cash: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    commission_bps: Bps
    sell_tax_bps: Bps
    slippage_bps: Bps
    max_symbol_weight: Annotated[
        Decimal, Field(gt=0, le=Decimal("0.10"), allow_inf_nan=False)
    ]
    participation_bps: Annotated[int, Field(strict=True, gt=0, le=1000)]


class SimulatedFill(FrozenModel):
    side: Literal["BUY", "SELL"]
    decided_at: AwareDatetime
    filled_at: AwareDatetime
    quantity: Annotated[int, Field(strict=True, gt=0)]
    price: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    costs: NonnegativeMoney


class EquityPoint(FrozenModel):
    at: AwareDatetime
    cash: NonnegativeMoney
    quantity: Annotated[int, Field(strict=True, ge=0)]
    equity: NonnegativeMoney


class NativeBacktestReport(FrozenModel):
    kind: Literal["NativeSmokeBacktest"] = "NativeSmokeBacktest"
    certified: Literal[False] = False
    instrument_id: Identifier
    strategy_digest: Sha256Digest
    replay_digest: Sha256Digest
    assumptions: SimulationAssumptions
    fills: tuple[SimulatedFill, ...]
    equity_curve: tuple[EquityPoint, ...]
    net_return: Decimal
    max_drawdown: Decimal

    def content_digest(self) -> str:
        return evidence_digest(self)


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def run_native_backtest(
    resolver: LocalArtifactResolver,
    request: InputReplayRequest,
    strategy: StrategySpec,
    *,
    instrument_id: str,
    session_opens: tuple[datetime, ...],
    assumptions: SimulationAssumptions,
) -> NativeBacktestReport:
    """Compute a synthetic/local smoke result, with no promotion or execution rights."""
    request = InputReplayRequest.model_validate(request.model_dump())
    strategy = StrategySpec.model_validate(strategy.model_dump())
    assumptions = SimulationAssumptions.model_validate(assumptions.model_dump())
    reference = strategy.dataset_snapshot
    if (
        reference.snapshot_id != request.snapshot.snapshot_id
        or reference.digest != request.snapshot.content_digest()
        or reference.observed_through != request.snapshot.observed_through
        or strategy.universe.membership_snapshot_id
        != request.snapshot.universe_membership.manifest_id
    ):
        raise ValueError("strategy snapshot reference mismatch")
    if strategy.signal.family is not StrategyFamily.TREND:
        raise ValueError("native smoke runner supports TREND only")
    parameters = {
        parameter.name: parameter.value for parameter in strategy.signal.parameters
    }
    if set(parameters) != {"signal.lookback_days"}:
        raise ValueError("native smoke runner requires only signal.lookback_days")
    lookback = int(parameters["signal.lookback_days"])
    if lookback != parameters["signal.lookback_days"] or not 2 <= lookback <= 252:
        raise ValueError("lookback must be an integer between 2 and 252")
    if not request.data_requirements:
        raise ValueError("backtest requires explicit data requirements")
    if len(session_opens) != len(request.cutoffs) or len(session_opens) < 2:
        raise ValueError("opens must cover at least two replay cutoffs")
    opens: list[datetime] = []
    for index, (opening, cutoff) in enumerate(
        zip(session_opens, request.cutoffs, strict=True)
    ):
        if opening.tzinfo is None or opening.utcoffset() is None:
            raise ValueError("session opens must be timezone-aware")
        opening = opening.astimezone(UTC)
        if opening >= cutoff or (index > 0 and opening <= request.cutoffs[index - 1]):
            raise ValueError(
                "session opens must follow the previous decision and precede close"
            )
        opens.append(opening)
    replay = replay_inputs(resolver, request)
    known_prices: list[tuple[DailyPrice, ...]] = []
    for bundle in replay.bundles:
        prices = tuple(
            sorted(
                (
                    normalize_daily_price(resolver, record)
                    for record in bundle.records
                    if record.instrument_id == instrument_id
                ),
                key=lambda price: price.session,
            )
        )
        sessions = [price.session for price in prices]
        if len(sessions) != len(set(sessions)):
            raise ValueError("ambiguous daily prices for one instrument/session")
        if any(price.session_close > bundle.cutoff for price in prices):
            raise ValueError("future price in decision bundle")
        known_prices.append(prices)

    cash = assumptions.initial_cash
    quantity = 0
    fills: list[SimulatedFill] = []
    curve: list[EquityPoint] = []
    target_long = False
    volume_cap = 0
    peak = cash
    drawdown = Decimal(0)
    with localcontext() as context:
        context.prec = 28
        for index, bundle in enumerate(replay.bundles):
            prices = known_prices[index]
            current = next(
                (
                    price
                    for price in prices
                    if price.session == opens[index].astimezone(SEOUL).date()
                ),
                None,
            )
            if current is None or current.session_close != bundle.cutoff:
                raise ValueError(
                    "missing exact current-session price; no stale valuation fallback"
                )
            if index > 0:
                decided_at = replay.bundles[index - 1].cutoff
                if target_long and quantity == 0:
                    fill_price = current.open * (
                        1 + Decimal(assumptions.slippage_bps) / 10000
                    )
                    gross_limit = cash * assumptions.max_symbol_weight
                    unit_cost = fill_price * (
                        1 + Decimal(assumptions.commission_bps) / 10000
                    )
                    units = min(_floor(gross_limit / unit_cost), volume_cap)
                    if units > 0 and current.volume > 0:
                        costs = (
                            fill_price
                            * units
                            * Decimal(assumptions.commission_bps)
                            / 10000
                        )
                        cash -= fill_price * units + costs
                        quantity += units
                        fills.append(
                            SimulatedFill(
                                side="BUY",
                                decided_at=decided_at,
                                filled_at=opens[index],
                                quantity=units,
                                price=fill_price,
                                costs=costs,
                            )
                        )
                elif not target_long and quantity > 0:
                    units = min(quantity, volume_cap)
                    if units > 0 and current.volume > 0:
                        fill_price = current.open * (
                            1 - Decimal(assumptions.slippage_bps) / 10000
                        )
                        costs = (
                            fill_price
                            * units
                            * Decimal(
                                assumptions.commission_bps + assumptions.sell_tax_bps
                            )
                            / 10000
                        )
                        cash += fill_price * units - costs
                        quantity -= units
                        fills.append(
                            SimulatedFill(
                                side="SELL",
                                decided_at=decided_at,
                                filled_at=opens[index],
                                quantity=units,
                                price=fill_price,
                                costs=costs,
                            )
                        )
            equity = cash + quantity * current.close
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
            curve.append(
                EquityPoint(
                    at=bundle.cutoff, cash=cash, quantity=quantity, equity=equity
                )
            )
            target_long = (
                len(prices) >= lookback
                and prices[-1].close
                > sum((price.close for price in prices[-lookback:]), Decimal(0))
                / lookback
            )
            volume_cap = current.volume * assumptions.participation_bps // 10000
        net_return = curve[-1].equity / assumptions.initial_cash - 1
    return NativeBacktestReport(
        instrument_id=instrument_id,
        strategy_digest=strategy.content_digest(),
        replay_digest=replay.content_digest(),
        assumptions=assumptions,
        fills=tuple(fills),
        equity_curve=tuple(curve),
        net_return=net_return,
        max_drawdown=drawdown,
    )
