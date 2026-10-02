"""Point-in-time multi-sleeve portfolio checks and purged evaluation protocols."""

import math
import statistics
from collections.abc import Callable
from datetime import datetime, time
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from ats.backtest.engines import (
    EngineCase,
    EngineEvidence,
    TargetEngineCase,
    run_engine,
)
from ats.data.information_analysis import InformationAnalysis
from ats.domain.governance import evidence_digest
from ats.domain.prices import SEOUL
from ats.domain.strategy import ArtifactRef, FrozenModel, Identifier, Sha256Digest


class FactorComposition(FrozenModel):
    api_version: Literal["ats/factors-v1"] = "ats/factors-v1"
    lookback: Annotated[int, Field(strict=True, ge=2, le=252)]
    direction: Literal[-1, 1] = 1
    require_official: bool = False
    max_information_age_seconds: Annotated[int, Field(strict=True, ge=1, le=604800)] = (
        86400
    )


def compile_factors(
    case: EngineCase,
    composition: FactorComposition,
    *,
    instrument: str,
    information: tuple[InformationAnalysis, ...],
) -> TargetEngineCase:
    composition = FactorComposition.model_validate(composition.model_dump())
    case = EngineCase.model_validate(case.model_dump())
    information = tuple(
        InformationAnalysis.model_validate(item.model_dump()) for item in information
    )
    if len({item.cutoff for item in information}) != len(information):
        raise ValueError("ambiguous information snapshots")
    targets: list[int] = []
    decisions: list[datetime] = []
    for index, session in enumerate(case.dates):
        decided = datetime.combine(session, time(8), tzinfo=SEOUL)
        decisions.append(decided)
        prior = case.close[max(0, index - composition.lookback) : index]
        signal = (
            len(prior) == composition.lookback
            and composition.direction * (prior[-1] - statistics.mean(prior)) > 0
        )
        visible = [
            item
            for item in information
            if item.cutoff <= decided
            and (decided - item.cutoff).total_seconds()
            <= composition.max_information_age_seconds
        ]
        latest = max(visible, key=lambda item: item.cutoff) if visible else None
        feature = (
            next(
                (item for item in latest.features if item.instrument_id == instrument),
                None,
            )
            if latest
            else None
        )
        if feature is not None and feature.conflicting_evidence:
            signal = False
        if composition.require_official and (
            feature is None or feature.official_events < 1
        ):
            signal = False
        targets.append(case.quantity if signal else 0)
    material = FactorInputs(composition=composition, information=information)
    artifact = ArtifactRef(
        artifact_id="compiled-factor-stream",
        version="1",
        digest=evidence_digest(material),
    )
    return TargetEngineCase.model_validate(
        {
            **case.model_dump(),
            "targets": targets,
            "decided_at": decisions,
            "signal_artifact": artifact,
        }
    )


class FactorInputs(FrozenModel):
    composition: FactorComposition
    information: tuple[InformationAnalysis, ...]


class PortfolioAsset(FrozenModel):
    instrument_id: Identifier
    asset_class: Literal["EQUITY", "ETF"]
    leveraged_or_inverse: Literal[False] = False
    case: TargetEngineCase
    eligible: tuple[bool, ...]
    tradable: tuple[bool, ...]
    classification_known_at: tuple[AwareDatetime, ...]
    price_observed_at: tuple[AwareDatetime, ...]
    corporate_actions: tuple[Literal["NONE", "UNRESOLVED"], ...]

    @model_validator(mode="after")
    def point_in_time(self) -> Self:
        count = len(self.case.dates)
        if any(
            len(values) != count
            for values in (
                self.eligible,
                self.tradable,
                self.classification_known_at,
                self.price_observed_at,
                self.corporate_actions,
            )
        ):
            raise ValueError("asset history must cover the full calendar")
        previous = 0
        for index, session in enumerate(self.case.dates):
            execution = datetime.combine(session, time(9), tzinfo=SEOUL)
            if self.classification_known_at[index] >= execution:
                raise ValueError("future historical classification")
            if (
                index
                and self.price_observed_at[index - 1] > self.case.decided_at[index]
            ):
                raise ValueError("price was unavailable for this decision")
            if self.corporate_actions[index] != "NONE":
                raise ValueError("unresolved corporate action blocks evaluation")
            target = self.case.targets[index]
            if not self.eligible[index] and target:
                raise ValueError("ineligible instrument cannot have a target position")
            if not self.tradable[index] and target != previous:
                raise ValueError("suspended instrument cannot trade")
            previous = target
        return self


class PortfolioRequest(FrozenModel):
    assets: Annotated[tuple[PortfolioAsset, ...], Field(min_length=2, max_length=10)]
    strategy_digest: Sha256Digest
    snapshot_digest: Sha256Digest
    image_digest: Sha256Digest

    @model_validator(mode="after")
    def common_inputs(self) -> Self:
        first = self.assets[0].case
        if len({asset.instrument_id for asset in self.assets}) != len(self.assets):
            raise ValueError("duplicate portfolio instruments")
        if any(
            asset.case.dates != first.dates
            or asset.case.initial_cash != first.initial_cash
            for asset in self.assets
        ):
            raise ValueError("portfolio sleeves must share calendar and capital")
        return self


class PortfolioResult(FrozenModel):
    engine: Literal["QLIB", "LEAN"]
    request_digest: Sha256Digest
    sleeves: tuple[EngineEvidence, ...]
    equity: tuple[float, ...]
    cash: tuple[float, ...]
    daily_loss_breaches: tuple[int, ...]
    drawdown_breaches: tuple[int, ...]
    certified: Literal[False] = False


def evaluate_portfolio(
    request: PortfolioRequest, *, engine: Literal["QLIB", "LEAN"], lock: Path
) -> PortfolioResult:
    request = PortfolioRequest.model_validate(request.model_dump())
    results = tuple(
        run_engine(
            asset.case,
            engine=engine,
            image_digest=request.image_digest,
            dependency_lock=lock,
            strategy_digest=request.strategy_digest,
            snapshot_digest=request.snapshot_digest,
        )
        for asset in request.assets
    )
    initial = request.assets[0].case.initial_cash
    cash = initial
    equity: list[float] = []
    cash_curve: list[float] = []
    holdings = {asset.instrument_id: 0.0 for asset in request.assets}
    previous_equity, peak = initial, initial
    losses: list[int] = []
    drawdowns: list[int] = []
    for index, session in enumerate(request.assets[0].case.dates):
        open_equity = cash + sum(
            holdings[asset.instrument_id] * asset.case.open[index]
            for asset in request.assets
        )
        trades = [
            (asset, fill)
            for asset, result in zip(request.assets, results, strict=True)
            for fill in result.result.fills
            if fill.date == session
        ]
        if (losses or drawdowns) and trades:
            raise ValueError("new portfolio orders after a loss halt are forbidden")
        for asset, fill in sorted(
            trades, key=lambda item: (item[1].side == "BUY", item[0].instrument_id)
        ):
            units = fill.quantity if fill.side == "BUY" else -fill.quantity
            holdings[asset.instrument_id] += units
            cash -= units * fill.price + fill.cost
            if cash < -1e-7 or holdings[asset.instrument_id] < 0:
                raise ValueError("portfolio aggregation implies borrowing or shorting")
            if (
                fill.side == "BUY"
                and holdings[asset.instrument_id] * fill.price
                > open_equity * 0.1 + 1e-7
            ):
                raise ValueError("portfolio concentration limit exceeded")
        value = cash + sum(
            holdings[asset.instrument_id] * asset.case.close[index]
            for asset in request.assets
        )
        derived = (
            sum(result.result.equity[index] for result in results)
            - (len(results) - 1) * initial
        )
        if abs(value - derived) > 1e-6:
            raise ValueError("portfolio cash/engine equity mismatch")
        if previous_equity and 1 - value / previous_equity >= 0.01:
            losses.append(index)
        peak = max(peak, value)
        if 1 - value / peak >= 0.15:
            drawdowns.append(index)
        equity.append(value)
        cash_curve.append(cash)
        previous_equity = value
    return PortfolioResult(
        engine=engine,
        request_digest=evidence_digest(request),
        sleeves=results,
        equity=tuple(equity),
        cash=tuple(cash_curve),
        daily_loss_breaches=tuple(losses),
        drawdown_breaches=tuple(drawdowns),
    )


class WalkForwardFold(FrozenModel):
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def purged_folds(
    *,
    observations: int,
    minimum_train: int,
    test_size: int,
    label_horizon: int,
    embargo: int,
) -> tuple[WalkForwardFold, ...]:
    if (
        any(
            type(value) is not int or value < 1
            for value in (observations, minimum_train, test_size, label_horizon)
        )
        or type(embargo) is not int
        or embargo < 0
    ):
        raise ValueError(
            "explicit positive fold sizes and nonnegative embargo required"
        )
    folds: list[WalkForwardFold] = []
    cursor = minimum_train + label_horizon + embargo
    while cursor + test_size <= observations:
        folds.append(
            WalkForwardFold(
                train_start=0,
                train_end=cursor - label_horizon - embargo,
                test_start=cursor,
                test_end=cursor + test_size,
            )
        )
        cursor += test_size + embargo
    if len(folds) < 2:
        raise ValueError("at least two disjoint OOS folds required")
    return tuple(folds)


def corrected_mean_lower_bound(
    returns: tuple[float, ...], *, trials: int, alpha: float = 0.05
) -> float:
    if (
        len(returns) < 3
        or trials < 1
        or not 0 < alpha < 1
        or not all(math.isfinite(value) for value in returns)
    ):
        raise ValueError("finite OOS samples and total trial count required")
    quantile = statistics.NormalDist().inv_cdf(1 - alpha / trials)
    return statistics.mean(returns) - quantile * statistics.stdev(returns) / (
        len(returns) ** 0.5
    )


class OosProtocol(FrozenModel):
    minimum_train: Annotated[int, Field(strict=True, ge=2)]
    test_size: Annotated[int, Field(strict=True, ge=3)]
    label_horizon: Annotated[int, Field(strict=True, ge=1)]
    embargo: Annotated[int, Field(strict=True, ge=0)]
    total_trials: Annotated[int, Field(strict=True, ge=1)]
    cost_multipliers: tuple[Annotated[float, Field(ge=1, le=5)], ...] = (1, 2)


class OosEvidence(FrozenModel):
    protocol_digest: Sha256Digest
    candidate_digest: Sha256Digest
    case_digest: Sha256Digest
    selection_cutoff: AwareDatetime
    fold_results: tuple[EngineEvidence, ...]
    corrected_lower_bound: float
    deployment_ready: Literal[False] = False
    model_receipt: dict[str, Any] | None = None
    positive_baseline_folds: int = 0
    baseline_fold_count: int = 0
    maximum_participation: float = 0
    regime_returns: dict[str, tuple[float, ...]] = {}
    dsr_diagnostic: dict[str, Any] | None = None


def evaluate_oos(
    case: EngineCase,
    composition: FactorComposition,
    protocol: OosProtocol,
    *,
    selection_cutoff: datetime,
    information: tuple[InformationAnalysis, ...],
    instrument: str,
    candidate_digest: str,
    snapshot_digest: str,
    image_digest: str,
    qlib_lock: Path,
    lean_lock: Path,
    guard: Callable[[], None],
    prepared_stream: TargetEngineCase | None = None,
    model_receipt: dict[str, Any] | None = None,
) -> OosEvidence:
    case = EngineCase.model_validate(case.model_dump())
    protocol = OosProtocol.model_validate(protocol.model_dump())
    if (
        not protocol.cost_multipliers
        or 1 not in protocol.cost_multipliers
        or len(set(protocol.cost_multipliers)) != len(protocol.cost_multipliers)
    ):
        raise ValueError("unique stresses including baseline cost are required")
    if selection_cutoff.tzinfo is None or selection_cutoff.utcoffset() is None:
        raise ValueError("aware candidate selection cutoff required")
    folds = purged_folds(
        observations=len(case.dates),
        minimum_train=protocol.minimum_train,
        test_size=protocol.test_size,
        label_horizon=protocol.label_horizon,
        embargo=protocol.embargo,
    )
    if len(folds) * len(protocol.cost_multipliers) * 2 > 24:
        raise ValueError("OOS engine-call budget exceeded")
    if selection_cutoff >= datetime.combine(
        case.dates[folds[0].test_start], time(0), tzinfo=SEOUL
    ):
        raise ValueError("candidate was selected after OOS began")
    streams = prepared_stream or compile_factors(
        case, composition, instrument=instrument, information=information
    )
    if prepared_stream is not None:
        streams = TargetEngineCase.model_validate(prepared_stream.model_dump())
        base = EngineCase.model_validate(
            streams.model_dump(
                exclude={"api_version", "targets", "decided_at", "signal_artifact"}
            )
        )
        if base != case:
            raise ValueError(
                "prepared OOS signal stream is bound to different market inputs"
            )
    evidence: list[EngineEvidence] = []
    returns: list[float] = []
    regimes: dict[str, list[float]] = {"UP": [], "DOWN": [], "FLAT": []}
    positive, maximum_participation = 0, 0.0
    for fold in folds:
        for multiplier in protocol.cost_multipliers:
            guard()
            first, last = fold.test_start, fold.test_end
            targets = streams.targets[first:last]
            next_session = (
                case.dates[last] if last < len(case.dates) else case.next_session
            )
            sample = TargetEngineCase.model_validate(
                {
                    **case.model_dump(),
                    "dates": case.dates[first:last],
                    "next_session": next_session,
                    "open": case.open[first:last],
                    "close": case.close[first:last],
                    "volume": case.volume[first:last],
                    "targets": targets,
                    "decided_at": streams.decided_at[first:last],
                    "signal_artifact": streams.signal_artifact,
                    "buy_fee": case.buy_fee * multiplier,
                    "sell_fee": case.sell_fee * multiplier,
                }
            )
            engines: tuple[tuple[Literal["QLIB", "LEAN"], Path], ...] = (
                ("QLIB", qlib_lock),
                ("LEAN", lean_lock),
            )
            paired: list[EngineEvidence] = []
            for engine, lock in engines:
                guard()
                paired.append(
                    run_engine(
                        sample,
                        engine=engine,
                        image_digest=image_digest,
                        dependency_lock=lock,
                        strategy_digest=candidate_digest,
                        snapshot_digest=snapshot_digest,
                    )
                )
            if (
                paired[0].result.fills != paired[1].result.fills
                or paired[0].result.equity != paired[1].result.equity
            ):
                raise ValueError("OOS engines disagree")
            evidence.extend(paired)
            for fill in paired[0].result.fills:
                participation = (
                    fill.quantity / sample.volume[sample.dates.index(fill.date)]
                )
                maximum_participation = max(maximum_participation, participation)
                if participation > 0.1:
                    raise ValueError("holdout liquidity participation exceeded")
            if multiplier == 1:
                fold_return = paired[0].result.equity[-1] / case.initial_cash - 1
                positive += fold_return > 0
                market_return = case.close[last - 1] / case.open[first] - 1
                regime = (
                    "UP"
                    if market_return > 0
                    else "DOWN"
                    if market_return < 0
                    else "FLAT"
                )
                regimes[regime].append(fold_return)
                curve = (case.initial_cash, *paired[0].result.equity)
                returns.extend(
                    current / previous - 1
                    for previous, current in zip(curve, curve[1:], strict=False)
                )
    return OosEvidence(
        protocol_digest=evidence_digest(protocol),
        candidate_digest=candidate_digest,
        case_digest=evidence_digest(case),
        selection_cutoff=selection_cutoff,
        model_receipt=model_receipt,
        positive_baseline_folds=positive,
        baseline_fold_count=len(folds),
        maximum_participation=maximum_participation,
        regime_returns={name: tuple(values) for name, values in regimes.items()},
        fold_results=tuple(evidence),
        corrected_lower_bound=corrected_mean_lower_bound(
            tuple(returns), trials=protocol.total_trials
        ),
    )
