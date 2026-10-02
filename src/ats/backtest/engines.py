"""Bounded full-engine adapters for explicit synthetic next-open backtests."""

import hashlib
import json
import os
import re
import statistics
import struct
import subprocess
import tempfile
from datetime import UTC, date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from ats.data.datasets import DatasetManifest, StoredArtifactResolver
from ats.data.prices import normalize_daily_price
from ats.domain.governance import evidence_digest
from ats.domain.prices import SEOUL
from ats.domain.research import (
    EvaluationEngine,
    EvaluationMetrics,
    EvaluationResult,
    ExperimentRun,
    ExperimentStatus,
)
from ats.domain.strategy import (
    ArtifactRef,
    FrozenModel,
    Identifier,
    Sha256Digest,
    StrategyFamily,
)
from ats.ports import EvaluationOutput, EvaluationRequest

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
ENGINE_ABSOLUTE_TOLERANCE = 1e-7


def execution_price(
    price: float, *, buy: bool, slippage: float, tick: float | None
) -> float:
    value = Decimal(str(price)) * (1 + Decimal(str(slippage)) * (1 if buy else -1))
    if tick is not None:
        unit = Decimal(str(tick))
        value = (value / unit).to_integral_value(
            rounding=ROUND_CEILING if buy else ROUND_FLOOR
        ) * unit
    return float(value)


class CorporateAction(FrozenModel):
    action_id: Identifier
    kind: Literal["FORWARD_SPLIT", "NET_CASH_DIVIDEND"]
    session: date
    known_at: AwareDatetime
    evidence: ArtifactRef
    new_shares_per_old: Annotated[int, Field(strict=True, ge=1, le=100)] = 1
    net_cash_per_share: Nonnegative = 0
    payment_session: date | None = None

    @model_validator(mode="after")
    def valid_action(self) -> Self:
        if self.kind == "FORWARD_SPLIT":
            if (
                self.new_shares_per_old < 2
                or self.net_cash_per_share
                or self.payment_session is not None
            ):
                raise ValueError("forward split requires only an integer share ratio")
        elif (
            self.new_shares_per_old != 1
            or self.net_cash_per_share <= 0
            or self.payment_session is None
            or self.payment_session < self.session
        ):
            raise ValueError(
                "dividend requires a net amount and a valid payment session"
            )
        return self


class EngineCase(FrozenModel):
    mode: Literal["SYNTHETIC_OFFLINE_ONLY"]
    currency: Literal["KRW"]
    initial_cash: Positive
    quantity: Annotated[int, Field(strict=True, gt=0)]
    lookback: Annotated[int, Field(strict=True, ge=2, le=252)]
    next_session: date
    buy_fee: Annotated[float, Field(ge=0, le=0.1, allow_inf_nan=False)]
    sell_fee: Annotated[float, Field(ge=0, le=0.1, allow_inf_nan=False)]
    slippage: Annotated[float, Field(ge=0, le=0.05, allow_inf_nan=False)]
    price_tick: Positive | None = None
    corporate_actions: Annotated[tuple[CorporateAction, ...], Field(max_length=32)] = ()
    opening_capacity: (
        tuple[Annotated[int, Field(strict=True, ge=0, le=1000000)], ...] | None
    ) = None
    dates: Annotated[tuple[date, ...], Field(min_length=2, max_length=512)]
    open: tuple[Positive, ...]
    close: tuple[Positive, ...]
    volume: tuple[Annotated[int, Field(strict=True, ge=1)], ...]

    @model_validator(mode="after")
    def validate_case(self) -> Self:
        if (
            tuple(sorted(set(self.dates))) != self.dates
            or self.next_session <= self.dates[-1]
            or any(
                len(values) != len(self.dates)
                for values in (self.open, self.close, self.volume)
            )
        ):
            raise ValueError(
                "ordered calendar and complete equal-length prices required"
            )
        if (
            self.quantity
            * execution_price(
                max(self.open), buy=True, slippage=self.slippage, tick=self.price_tick
            )
            > self.initial_cash * 0.1
        ):
            raise ValueError("synthetic size exceeds initial concentration limit")
        if self.opening_capacity is None and self.quantity > min(self.volume) * 0.1:
            raise ValueError("synthetic size exceeds volume participation limit")
        if self.opening_capacity is not None and (
            len(self.opening_capacity) != len(self.dates)
            or any(
                capacity > volume * 0.1
                for capacity, volume in zip(
                    self.opening_capacity, self.volume, strict=True
                )
            )
        ):
            raise ValueError(
                "opening capacity must cover sessions within volume limits"
            )
        for price in (
            *self.open,
            *self.close,
            *(
                execution_price(
                    price, buy=side == 1, slippage=self.slippage, tick=self.price_tick
                )
                for price in self.open
                for side in (-1, 1)
            ),
        ):
            if price <= 0:
                raise ValueError("tick rounding produces a nonpositive execution price")
            if struct.unpack("<f", struct.pack("<f", price))[0] != price:
                raise ValueError(
                    "price cannot be represented exactly by the Qlib provider"
                )
        if len({action.action_id for action in self.corporate_actions}) != len(
            self.corporate_actions
        ) or len({action.session for action in self.corporate_actions}) != len(
            self.corporate_actions
        ):
            raise ValueError(
                "corporate action identities and ex-sessions must be unique"
            )
        for action in self.corporate_actions:
            if (
                action.session not in self.dates[1:]
                or action.payment_session is not None
                and action.payment_session not in self.dates
                or action.known_at
                >= datetime.combine(action.session, time(8), tzinfo=SEOUL)
            ):
                raise ValueError(
                    "corporate action requires known terms and full ex/payment calendar"
                )
        for index in range(1, len(self.dates)):
            reference = self.close[index - 1]
            for action in self.corporate_actions:
                if action.session == self.dates[index]:
                    reference = (
                        reference / action.new_shares_per_old
                        - action.net_cash_per_share
                    )
            if reference <= 0:
                raise ValueError("corporate action implies nonpositive reference price")
            if any(
                abs(value / reference - 1) >= 0.3
                for value in (self.open[index], self.close[index])
            ):
                raise ValueError(
                    "synthetic case reaches an unsupported price-limit boundary"
                )
        return self


class TargetEngineCase(EngineCase):
    api_version: Literal["ats/target-engine-v1"] = "ats/target-engine-v1"
    targets: tuple[Annotated[int, Field(strict=True, ge=0)], ...]
    decided_at: tuple[AwareDatetime, ...]
    signal_artifact: ArtifactRef

    @model_validator(mode="after")
    def validate_targets(self) -> Self:
        if len(self.targets) != len(self.dates) or len(self.decided_at) != len(
            self.dates
        ):
            raise ValueError("signal stream must cover the exact engine calendar")
        if any(target > self.quantity for target in self.targets):
            raise ValueError("signal stream exceeds fixed sizing authority")
        for session, decided in zip(self.dates, self.decided_at, strict=True):
            if decided >= datetime.combine(session, time(9), tzinfo=SEOUL):
                raise ValueError("signal was unavailable before execution")
        for action in self.corporate_actions:
            if action.known_at > self.decided_at[self.dates.index(action.session)]:
                raise ValueError("corporate action was unknown at target decision")
        return self


class EngineAsset(FrozenModel):
    instrument_id: Identifier
    case: TargetEngineCase


class SharedEngineCase(EngineCase):
    api_version: Literal["ats/shared-engine-v1"] = "ats/shared-engine-v1"
    assets: Annotated[tuple[EngineAsset, ...], Field(min_length=2, max_length=10)]
    cash_settlement_sessions: Annotated[int, Field(strict=True, ge=0, le=5)] = 0

    @model_validator(mode="after")
    def common_account(self) -> Self:
        if len({asset.instrument_id for asset in self.assets}) != len(self.assets):
            raise ValueError("shared portfolio instruments must be unique")
        for asset in self.assets:
            if not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", asset.instrument_id):
                raise ValueError("unsupported engine instrument identifier")
            if (
                asset.case.buy_fee != self.buy_fee
                or asset.case.sell_fee != self.sell_fee
            ):
                raise ValueError("shared portfolio requires common fee rates")
            if (
                asset.case.dates != self.dates
                or asset.case.next_session != self.next_session
                or asset.case.initial_cash != self.initial_cash
            ):
                raise ValueError("shared portfolio calendar and cash must match")
        return self


def _case(value: EngineCase) -> EngineCase:
    model = (
        SharedEngineCase
        if isinstance(value, SharedEngineCase)
        else TargetEngineCase
        if isinstance(value, TargetEngineCase)
        else EngineCase
    )
    validated = model.model_validate(value.model_dump())
    if validated.corporate_actions and not isinstance(
        validated, (TargetEngineCase, SharedEngineCase)
    ):
        raise ValueError(
            "corporate actions require explicit raw-share target decisions"
        )
    if isinstance(validated, SharedEngineCase):
        shared_account_reference(validated)
    return validated


class EngineFill(FrozenModel):
    instrument_id: Identifier | None = None
    date: date
    side: Literal["BUY", "SELL"]
    quantity: Positive
    price: Positive
    cost: Nonnegative


class EngineResult(FrozenModel):
    engine: Literal["QLIB", "LEAN"]
    version: str
    full_backtest: Literal[True]
    certified: Literal[False]
    fills: tuple[EngineFill, ...]
    equity: tuple[Nonnegative, ...]
    input_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    lock_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    code_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


class EngineEvidence(FrozenModel):
    strategy_digest: Sha256Digest
    snapshot_digest: Sha256Digest
    case_digest: Sha256Digest
    image_digest: Sha256Digest
    output_digest: Sha256Digest
    result: EngineResult


def engine_results_match(first: EngineResult, second: EngineResult) -> bool:
    if (
        first.input_sha256 != second.input_sha256
        or len(first.fills) != len(second.fills)
        or len(first.equity) != len(second.equity)
    ):
        return False
    for left, right in zip(first.fills, second.fills, strict=True):
        if (
            left.instrument_id != right.instrument_id
            or left.date != right.date
            or left.side != right.side
            or any(
                abs(getattr(left, field) - getattr(right, field))
                > ENGINE_ABSOLUTE_TOLERANCE
                for field in ("quantity", "price", "cost")
            )
        ):
            return False
    return all(
        abs(left - right) <= ENGINE_ABSOLUTE_TOLERANCE
        for left, right in zip(first.equity, second.equity, strict=True)
    )


def apply_corporate_actions(
    case: EngineCase,
    index: int,
    held: float,
    pending: dict[str, float],
) -> tuple[float, float]:
    session = case.dates[index]
    for action in case.corporate_actions:
        if action.session == session:
            if action.kind == "FORWARD_SPLIT":
                held *= action.new_shares_per_old
            else:
                pending[action.action_id] = held * action.net_cash_per_share
    paid = sum(
        pending.pop(action.action_id, 0)
        for action in case.corporate_actions
        if action.payment_session == session
    )
    return held, paid


def shared_account_reference(
    case: SharedEngineCase,
) -> tuple[tuple[EngineFill, ...], tuple[float, ...]]:
    cash = case.initial_cash
    holdings = {asset.instrument_id: 0 for asset in case.assets}
    dividends: dict[str, dict[str, float]] = {
        asset.instrument_id: {} for asset in case.assets
    }
    fills: list[EngineFill] = []
    equity: list[float] = []
    peak = previous = case.initial_cash
    halted = False
    unsettled: list[tuple[int, float]] = []
    for index, session in enumerate(case.dates):
        for asset in case.assets:
            units, paid = apply_corporate_actions(
                asset.case,
                index,
                holdings[asset.instrument_id],
                dividends[asset.instrument_id],
            )
            holdings[asset.instrument_id] = int(units)
            cash += paid
        receivable = sum(sum(pending.values()) for pending in dividends.values())
        unsettled = [
            (release, amount) for release, amount in unsettled if release > index
        ]
        opening_equity = (
            cash
            + receivable
            + sum(
                holdings[asset.instrument_id] * asset.case.open[index]
                for asset in case.assets
            )
        )
        halted = (
            halted or opening_equity <= previous * 0.99 or opening_equity <= peak * 0.85
        )
        ordered = sorted(
            case.assets,
            key=lambda asset: (
                asset.case.targets[index] > holdings[asset.instrument_id],
                asset.instrument_id,
            ),
        )
        for asset in ordered:
            target = asset.case.targets[index]
            change = target - holdings[asset.instrument_id]
            if asset.case.opening_capacity is not None:
                change = min(abs(change), asset.case.opening_capacity[index]) * (
                    1 if change > 0 else -1
                )
                target = holdings[asset.instrument_id] + change
            if not change:
                continue
            if halted:
                raise ValueError("shared portfolio order after loss halt")
            price = execution_price(
                asset.case.open[index],
                buy=change > 0,
                slippage=asset.case.slippage,
                tick=asset.case.price_tick,
            )
            cost = (
                abs(change)
                * price
                * (asset.case.buy_fee if change > 0 else asset.case.sell_fee)
            )
            if (
                change > 0
                and change * price + cost
                > cash
                - sum(amount for _, amount in unsettled)
                + ENGINE_ABSOLUTE_TOLERANCE
            ):
                raise ValueError(
                    "shared portfolio cannot spend unsettled sale proceeds"
                )
            if change > 0 and target * price > opening_equity * 0.1 + 1e-7:
                raise ValueError("shared portfolio concentration exceeded")
            cash -= change * price + cost
            if change < 0 and case.cash_settlement_sessions:
                unsettled.append(
                    (index + case.cash_settlement_sessions, -change * price - cost)
                )
            if cash < -1e-7 or target < 0:
                raise ValueError("shared portfolio cannot borrow or short")
            holdings[asset.instrument_id] = target
            fills.append(
                EngineFill(
                    instrument_id=asset.instrument_id,
                    date=session,
                    side="BUY" if change > 0 else "SELL",
                    quantity=abs(change),
                    price=price,
                    cost=cost,
                )
            )
        value = (
            cash
            + receivable
            + sum(
                holdings[asset.instrument_id] * asset.case.close[index]
                for asset in case.assets
            )
        )
        peak = max(peak, value)
        halted = halted or value <= previous * 0.99 or value <= peak * 0.85
        equity.append(value)
        previous = value
    return tuple(fills), tuple(equity)


def validate_engine_result(case: EngineCase, result: EngineResult) -> None:
    case = _case(case)
    result = EngineResult.model_validate(result.model_dump())
    if len(result.equity) != len(case.dates):
        raise ValueError("engine result does not cover every requested session")
    if isinstance(case, SharedEngineCase):
        fills, shared_curve = shared_account_reference(case)
        if len(fills) != len(result.fills):
            raise ValueError("shared portfolio fill count mismatch")
        for actual, expected_fill in zip(result.fills, fills, strict=True):
            if (
                actual.instrument_id != expected_fill.instrument_id
                or actual.date != expected_fill.date
                or actual.side != expected_fill.side
                or any(
                    abs(getattr(actual, field) - getattr(expected_fill, field)) > 1e-7
                    for field in ("quantity", "price", "cost")
                )
            ):
                raise ValueError("shared portfolio fill mismatch")
        if any(
            abs(actual - expected) > 1e-7
            for actual, expected in zip(result.equity, shared_curve, strict=True)
        ):
            raise ValueError("shared portfolio cash conservation mismatch")
        return
    cash, held = case.initial_cash, 0.0
    dividends: dict[str, float] = {}
    expected: list[EngineFill] = []
    curve: list[float] = []
    for index, session in enumerate(case.dates):
        held, paid = apply_corporate_actions(case, index, held, dividends)
        cash += paid
        history = case.close[max(0, index - case.lookback) : index]
        long = len(history) == case.lookback and history[-1] > sum(history) / len(
            history
        )
        target = (
            float(case.targets[index])
            if isinstance(case, TargetEngineCase)
            else float(case.quantity)
            if long
            else 0.0
        )
        difference = target - held
        if case.opening_capacity is not None:
            difference = min(abs(difference), case.opening_capacity[index]) * (
                1 if difference > 0 else -1
            )
        if difference:
            price = execution_price(
                case.open[index],
                buy=difference > 0,
                slippage=case.slippage,
                tick=case.price_tick,
            )
            value = abs(difference) * price
            fee = value * (case.buy_fee if difference > 0 else case.sell_fee)
            if difference > 0 and (
                value + fee > cash or value > (cash + held * price) * 0.1
            ):
                raise ValueError(
                    "synthetic order fails independent cash or concentration check"
                )
            cash -= difference * price + fee
            held += difference
            expected.append(
                EngineFill(
                    date=session,
                    side="BUY" if difference > 0 else "SELL",
                    quantity=abs(difference),
                    price=price,
                    cost=fee,
                )
            )
        curve.append(cash + sum(dividends.values()) + held * case.close[index])
    if len(expected) != len(result.fills):
        raise ValueError("engine fills differ from prior-close next-open decisions")
    for actual, reference in zip(result.fills, expected, strict=True):
        if (
            actual.date != reference.date
            or actual.side != reference.side
            or any(
                abs(getattr(actual, name) - getattr(reference, name)) > 1e-7
                for name in ("quantity", "price", "cost")
            )
        ):
            raise ValueError("engine fill or costs differ from explicit assumptions")
    if any(
        abs(actual - reference) > 1e-7
        for actual, reference in zip(result.equity, curve, strict=True)
    ):
        raise ValueError("engine equity violates cash/holdings conservation")


def run_engine(
    case: EngineCase,
    *,
    engine: Literal["QLIB", "LEAN"],
    image_digest: str,
    dependency_lock: Path,
    strategy_digest: str,
    snapshot_digest: str,
) -> EngineEvidence:
    case = _case(case)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
        raise ValueError("a pinned local image digest is required")
    payload = json.dumps(
        case.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    container = "ats-eval-" + uuid4().hex
    docker = [
        "docker",
        "--host",
        "npipe:////./pipe/dockerDesktopLinuxEngine"
        if os.name == "nt"
        else "unix:///var/run/docker.sock",
    ]
    command = [
        *docker,
        "run",
        "--pull",
        "never",
        "--rm",
        "-i",
        "--name",
        container,
        "--network",
        "none",
        "--read-only",
        "--user",
        "65532:65532",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "2g",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--entrypoint",
        "timeout",
        image_digest,
        "120s",
        *(
            ["python", "/opt/qlib_run.py"]
            if engine == "QLIB"
            else ["dotnet", "/opt/runner/Runner.dll"]
        ),
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {"PATH", "SYSTEMROOT", "USERPROFILE", "HOME", "LOCALAPPDATA", "TEMP", "TMP"}
    }
    with tempfile.TemporaryFile() as log:
        try:
            completed = subprocess.run(
                command,
                input=payload,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                timeout=150,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            subprocess.run(
                [*docker, "rm", "-f", container],
                env=environment,
                capture_output=True,
                timeout=30,
                check=False,
            )
            raise ValueError("engine exceeded execution budget") from error
        log.seek(0)
        output = log.read(1024 * 1024 + 1)
    if completed.returncode or len(output) > 1024 * 1024:
        detail = output[-4096:].decode("utf-8", errors="replace")
        raise ValueError(
            f"{engine} isolated synthetic engine failed (exit {completed.returncode}): {detail}"
        )
    if b"ERROR::" in output:
        raise ValueError("LEAN reported an internal error")
    rows = [line for line in output.splitlines() if line.startswith(b"{")]
    if len(rows) != 1:
        raise ValueError("expected exactly one engine result")
    result = EngineResult.model_validate_json(rows[0])
    if (
        result.engine != engine
        or result.version != ("0.9.7" if engine == "QLIB" else "2.5.18127")
        or result.input_sha256 != hashlib.sha256(payload).hexdigest()
        or result.lock_sha256
        != hashlib.sha256(dependency_lock.read_bytes()).hexdigest()
    ):
        raise ValueError("engine runtime or input binding mismatch")
    validate_engine_result(case, result)
    return EngineEvidence(
        strategy_digest=strategy_digest,
        snapshot_digest=snapshot_digest,
        case_digest=evidence_digest(case),
        image_digest=image_digest,
        output_digest=evidence_digest(result),
        result=result,
    )


class FullEngineEvaluationAdapter:
    def __init__(
        self,
        engine: EvaluationEngine,
        case: EngineCase,
        manifest: DatasetManifest,
        resolver: StoredArtifactResolver,
        *,
        image_digest: str,
        dependency_lock: Path,
        engine_artifact: ArtifactRef,
        protocol: ArtifactRef,
        output: Path,
    ) -> None:
        if engine not in (EvaluationEngine.QLIB, EvaluationEngine.LEAN):
            raise ValueError("unsupported external engine")
        self.engine = engine
        self.case = EngineCase.model_validate(case.model_dump())
        self.manifest, self.resolver = manifest, resolver
        self.image_digest, self.dependency_lock = image_digest, dependency_lock
        self.engine_artifact, self.protocol, self.output = (
            engine_artifact,
            protocol,
            output,
        )

    def _artifact(self, name: str, payload: bytes) -> ArtifactRef:
        digest = hashlib.sha256(payload).hexdigest()
        path = self.output / "sha256" / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != payload:
                raise ValueError("existing engine artifact is corrupt")
        else:
            with path.open("xb") as stream:
                stream.write(payload)
        return ArtifactRef(artifact_id=name, version="1", digest="sha256:" + digest)

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutput:
        request = EvaluationRequest.model_validate(request.model_dump())
        case = self.case
        if (
            request.engine != self.engine
            or request.engine_artifact != self.engine_artifact
            or request.protocol != self.protocol
            or request.snapshot != self.manifest.snapshot
            or request.strategy.container_image_digest != self.image_digest
            or request.dependency_lock_digest
            != "sha256:" + hashlib.sha256(self.dependency_lock.read_bytes()).hexdigest()
            or request.random_seed != 0
            or request.strategy.signal.family is not StrategyFamily.TREND
        ):
            raise ValueError(
                "evaluation inputs do not match pinned adapter configuration"
            )
        parameters = {
            item.name: item.value for item in request.strategy.signal.parameters
        }
        if parameters != {"signal.lookback_days": case.lookback}:
            raise ValueError("unsupported strategy parameters")
        selected = self.resolver.select_verified_records_as_of(
            request.snapshot,
            at=request.snapshot.observed_through,
            revision_orders=self.manifest.revisions_at(
                request.snapshot.observed_through
            ),
        )
        ordered = sorted(
            (
                (normalize_daily_price(self.resolver, record), record)
                for record in selected
            ),
            key=lambda item: item[0].session,
        )
        if (
            len(ordered) != len(case.dates)
            or len({price.instrument_id for price, _ in ordered}) != 1
            or tuple(price.session for price, _ in ordered) != case.dates
            or tuple(float(price.open) for price, _ in ordered) != case.open
            or tuple(float(price.close) for price, _ in ordered) != case.close
            or tuple(price.volume for price, _ in ordered) != case.volume
            or request.period_start.astimezone(SEOUL).date() != case.dates[0]
            or request.period_end.astimezone(SEOUL).date() != case.dates[-1]
        ):
            raise ValueError("engine case does not match verified snapshot and period")
        for index, (_, record) in enumerate(ordered[:-1]):
            next_open = datetime.combine(case.dates[index + 1], time(9), tzinfo=SEOUL)
            if record.observed_at >= next_open:
                raise ValueError(
                    "price revision was unavailable for the next-open decision"
                )
        started = datetime.now(UTC)
        if started < request.requested_at:
            raise ValueError("evaluation request is in the future")
        input_artifact = self._artifact(
            "evaluation-request", request.model_dump_json().encode()
        )
        evidence = run_engine(
            case,
            engine="QLIB" if self.engine is EvaluationEngine.QLIB else "LEAN",
            image_digest=self.image_digest,
            dependency_lock=self.dependency_lock,
            strategy_digest=request.strategy.content_digest(),
            snapshot_digest=request.snapshot.content_digest(),
        )
        if "sha256:" + evidence.result.code_sha256 != request.engine_artifact.digest:
            raise ValueError("executed engine code differs from the pinned artifact")
        output_artifact = self._artifact(
            "synthetic-engine-evidence", evidence.model_dump_json().encode()
        )
        case_artifact = self._artifact(
            "synthetic-engine-case", case.model_dump_json().encode()
        )
        finished = datetime.now(UTC)
        run = ExperimentRun(
            experiment_id=request.experiment_id,
            strategy_version=request.strategy.version,
            strategy_digest=request.strategy.content_digest(),
            dataset_snapshot=request.strategy.dataset_snapshot,
            hypothesis=request.strategy.research_hypothesis,
            engine=request.engine,
            engine_artifact=request.engine_artifact,
            code_artifact=request.strategy.code_artifact,
            container_image_digest=self.image_digest,
            dependency_lock_digest=request.dependency_lock_digest,
            random_seed=request.random_seed,
            started_at=started,
            finished_at=finished,
            status=ExperimentStatus.SUCCEEDED,
            artifacts=(input_artifact, output_artifact, case_artifact),
        )
        curve = (case.initial_cash, *evidence.result.equity)
        returns = [
            current / previous - 1
            for previous, current in zip(curve, curve[1:], strict=False)
        ]
        deviation = statistics.stdev(returns)
        metrics = EvaluationMetrics(
            net_oos_sharpe=statistics.mean(returns) / deviation * (252**0.5)
            if deviation
            else 0,
            max_drawdown=max(
                1 - value / max(curve[: index + 1]) for index, value in enumerate(curve)
            ),
            net_benchmark_excess_return=curve[-1] / curve[0] - 1,
            observation_count=len(returns),
        )
        result = EvaluationResult(
            evaluation_id=uuid4(),
            experiment_id=run.experiment_id,
            experiment_digest=run.content_digest(),
            protocol=request.protocol,
            evaluated_at=finished,
            period_start=request.period_start,
            period_end=request.period_end,
            metrics=metrics,
            artifacts=(output_artifact, case_artifact),
            data_integrity_violations=0,
        )
        return EvaluationOutput(run=run, result=result)
