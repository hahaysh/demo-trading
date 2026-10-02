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
from ats.domain.strategy import ArtifactRef, FrozenModel, Sha256Digest, StrategyFamily
from ats.ports import EvaluationOutput, EvaluationRequest

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class EngineCase(FrozenModel):
    mode: Literal["SYNTHETIC_OFFLINE_ONLY"]
    currency: Literal["KRW"]
    initial_cash: Positive
    quantity: Annotated[int, Field(strict=True, gt=0)]
    lookback: Annotated[int, Field(strict=True, ge=2, le=252)]
    next_session: date
    buy_fee: Annotated[float, Field(ge=0, le=0.1, allow_inf_nan=False)]
    sell_fee: Annotated[float, Field(ge=0, le=0.1, allow_inf_nan=False)]
    slippage: Literal[0]
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
        if self.quantity * max(self.open) > self.initial_cash * 0.1:
            raise ValueError("synthetic size exceeds initial concentration limit")
        if self.quantity > min(self.volume) * 0.1:
            raise ValueError("synthetic size exceeds volume participation limit")
        for price in (*self.open, *self.close):
            if struct.unpack("<f", struct.pack("<f", price))[0] != price:
                raise ValueError(
                    "price cannot be represented exactly by the Qlib provider"
                )
        for index in range(1, len(self.dates)):
            if any(
                abs(value / self.close[index - 1] - 1) >= 0.3
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
        return self


def _case(value: EngineCase) -> EngineCase:
    model = TargetEngineCase if isinstance(value, TargetEngineCase) else EngineCase
    return model.model_validate(value.model_dump())


class EngineFill(FrozenModel):
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


def validate_engine_result(case: EngineCase, result: EngineResult) -> None:
    case = _case(case)
    result = EngineResult.model_validate(result.model_dump())
    if len(result.equity) != len(case.dates):
        raise ValueError("engine result does not cover every requested session")
    cash, held = case.initial_cash, 0.0
    expected: list[EngineFill] = []
    curve: list[float] = []
    for index, session in enumerate(case.dates):
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
        if difference:
            price = case.open[index]
            value = abs(difference) * price
            fee = value * (case.buy_fee if difference > 0 else case.sell_fee)
            if difference > 0 and (
                value + fee > cash or value > (cash + held * price) * 0.1
            ):
                raise ValueError(
                    "synthetic order fails independent cash or concentration check"
                )
            cash -= difference * price + fee
            held = target
            expected.append(
                EngineFill(
                    date=session,
                    side="BUY" if difference > 0 else "SELL",
                    quantity=abs(difference),
                    price=price,
                    cost=fee,
                )
            )
        curve.append(cash + held * case.close[index])
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
