"""Validated adapter boundaries, without engine, broker, or network implementations."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ats.domain.data import DataSnapshot
from ats.domain.research import (
    EvaluationEngine,
    EvaluationResult,
    ExperimentRun,
    ExperimentStatus,
)
from ats.domain.strategy import (
    ArtifactRef,
    DatasetSnapshotRef,
    FrozenModel,
    Sha256Digest,
    StrategySpec,
)


def _validate_inputs(strategy: StrategySpec, snapshot: DataSnapshot) -> None:
    reference = strategy.dataset_snapshot
    if (
        reference.snapshot_id != snapshot.snapshot_id
        or reference.observed_through != snapshot.observed_through
        or reference.digest != snapshot.content_digest()
        or strategy.universe.membership_snapshot_id
        != snapshot.universe_membership.manifest_id
    ):
        raise ValueError("adapter inputs do not match the strategy snapshot")


class EvaluationRequest(FrozenModel):
    experiment_id: UUID
    strategy: StrategySpec
    snapshot: DataSnapshot
    engine: EvaluationEngine
    engine_artifact: ArtifactRef
    dependency_lock_digest: Sha256Digest
    random_seed: Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)]
    protocol: ArtifactRef
    period_start: AwareDatetime
    period_end: AwareDatetime
    requested_at: AwareDatetime

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _validate_inputs(self.strategy, self.snapshot)
        if not self.period_start < self.period_end <= self.snapshot.observed_through:
            raise ValueError("evaluation period must be ordered and within snapshot")
        if self.requested_at < max(
            self.snapshot.created_at, self.strategy.version.created_at
        ):
            raise ValueError("request cannot precede input creation")
        return self


class EvaluationOutput(FrozenModel):
    run: ExperimentRun
    result: EvaluationResult | None = None

    @model_validator(mode="after")
    def validate_output(self) -> Self:
        if self.run.status is ExperimentStatus.SUCCEEDED:
            if self.result is None:
                raise ValueError("successful evaluation requires a result")
            self.result.validate_against_run(self.run)
        elif self.result is not None:
            raise ValueError("failed evaluation cannot carry a result")
        return self


class EvaluationPort(Protocol):
    @property
    def engine(self) -> EvaluationEngine: ...

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutput: ...


class QlibResearchPort(EvaluationPort, Protocol):
    @property
    def engine(self) -> Literal[EvaluationEngine.QLIB]: ...


class LeanCertificationPort(EvaluationPort, Protocol):
    @property
    def engine(self) -> Literal[EvaluationEngine.LEAN]: ...


def run_evaluation(
    port: EvaluationPort, request: EvaluationRequest
) -> EvaluationOutput:
    """Validate before dispatch and before accepting output; propagate failures."""
    request = EvaluationRequest.model_validate(request.model_dump())
    if port.engine != request.engine:
        raise ValueError("requested engine does not match adapter")
    output = EvaluationOutput.model_validate(port.evaluate(request).model_dump())
    run = output.run
    run.validate_inputs(request.strategy, request.snapshot)
    if (
        run.experiment_id != request.experiment_id
        or run.engine != request.engine
        or run.engine_artifact != request.engine_artifact
        or run.dependency_lock_digest != request.dependency_lock_digest
        or run.random_seed != request.random_seed
        or run.hypothesis != request.strategy.research_hypothesis
        or run.started_at < request.requested_at
    ):
        raise ValueError("adapter run does not match evaluation request")
    if output.result is not None and (
        output.result.protocol != request.protocol
        or output.result.period_start != request.period_start
        or output.result.period_end != request.period_end
    ):
        raise ValueError("adapter result does not match evaluation protocol or period")
    return output


class SignalRequest(FrozenModel):
    request_id: UUID
    strategy: StrategySpec
    snapshot: DataSnapshot
    requested_at: AwareDatetime

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _validate_inputs(self.strategy, self.snapshot)
        if self.requested_at < max(
            self.snapshot.created_at, self.strategy.version.created_at
        ):
            raise ValueError("signal request cannot precede input creation")
        return self


class SignalOutput(FrozenModel):
    request_id: UUID
    strategy_version_id: UUID
    strategy_digest: Sha256Digest
    dataset_snapshot: DatasetSnapshotRef
    evaluator: ArtifactRef
    generated_at: AwareDatetime
    signal: ArtifactRef


class SignalEvaluatorPort(Protocol):
    def evaluate(self, request: SignalRequest) -> SignalOutput: ...


def run_signal_evaluation(
    port: SignalEvaluatorPort, request: SignalRequest
) -> SignalOutput:
    """Accept only a signal artifact bound to the exact requested input set."""
    request = SignalRequest.model_validate(request.model_dump())
    output = SignalOutput.model_validate(port.evaluate(request).model_dump())
    if (
        output.request_id != request.request_id
        or output.strategy_version_id != request.strategy.version.version_id
        or output.strategy_digest != request.strategy.content_digest()
        or output.dataset_snapshot != request.strategy.dataset_snapshot
        or output.evaluator != request.strategy.signal.implementation
        or output.generated_at < request.requested_at
    ):
        raise ValueError("signal output does not match request")
    return output
