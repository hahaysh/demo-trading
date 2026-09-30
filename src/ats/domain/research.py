"""Reproducible experiment receipts and evaluation evidence, not approvals."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, FiniteFloat, model_validator

from ats.domain.data import DataSnapshot
from ats.domain.strategy import (
    ArtifactRef,
    DatasetSnapshotRef,
    FrozenModel,
    NonEmptyText,
    Sha256Digest,
    StrategySpec,
    StrategyVersion,
)


class ExperimentStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class EvaluationEngine(StrEnum):
    NATIVE = "NATIVE"
    QLIB = "QLIB"
    LEAN = "LEAN"


class ExperimentRun(FrozenModel):
    """A terminal run receipt; scheduling and execution are separate concerns."""

    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["ExperimentRun"] = "ExperimentRun"
    experiment_id: UUID
    strategy_version: StrategyVersion
    strategy_digest: Sha256Digest
    dataset_snapshot: DatasetSnapshotRef
    hypothesis: NonEmptyText
    engine: EvaluationEngine
    engine_artifact: ArtifactRef
    code_artifact: ArtifactRef
    container_image_digest: Sha256Digest
    dependency_lock_digest: Sha256Digest
    random_seed: Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)]
    started_at: AwareDatetime
    finished_at: AwareDatetime
    status: ExperimentStatus
    artifacts: tuple[ArtifactRef, ...] = ()
    failure_reason: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.started_at < self.strategy_version.created_at:
            raise ValueError("experiment cannot start before strategy creation")
        if self.started_at < self.dataset_snapshot.observed_through:
            raise ValueError("experiment cannot start before snapshot freeze")
        keys = [(artifact.artifact_id, artifact.version) for artifact in self.artifacts]
        if len(keys) != len(set(keys)):
            raise ValueError("experiment artifacts must have unique identities")
        if self.status is ExperimentStatus.SUCCEEDED:
            if not self.artifacts or self.failure_reason is not None:
                raise ValueError(
                    "successful experiments require artifacts and no failure"
                )
        elif self.failure_reason is None:
            raise ValueError("failed experiments require a failure reason")
        return self

    def validate_inputs(self, strategy: StrategySpec, snapshot: DataSnapshot) -> None:
        """Resolve declared inputs before accepting a receipt for evaluation."""
        if (
            self.strategy_version != strategy.version
            or self.strategy_digest != strategy.content_digest()
        ):
            raise ValueError("experiment strategy reference does not match strategy")
        if (
            self.dataset_snapshot != strategy.dataset_snapshot
            or self.dataset_snapshot.snapshot_id != snapshot.snapshot_id
            or self.dataset_snapshot.observed_through != snapshot.observed_through
            or self.dataset_snapshot.digest != snapshot.content_digest()
        ):
            raise ValueError("experiment snapshot reference does not match inputs")
        if (
            self.code_artifact != strategy.code_artifact
            or self.container_image_digest != strategy.container_image_digest
        ):
            raise ValueError("experiment runtime does not match strategy")
        if self.started_at < snapshot.created_at:
            raise ValueError("experiment cannot start before snapshot creation")

    def content_digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class EvaluationMetrics(FrozenModel):
    net_oos_sharpe: FiniteFloat
    max_drawdown: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    net_benchmark_excess_return: FiniteFloat
    observation_count: Annotated[int, Field(strict=True, ge=2)]
    deflated_sharpe_confidence: (
        Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None
    ) = None


class EvaluationResult(FrozenModel):
    """One engine's measured results; never a promotion or risk decision."""

    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["EvaluationResult"] = "EvaluationResult"
    evaluation_id: UUID
    experiment_id: UUID
    experiment_digest: Sha256Digest
    protocol: ArtifactRef
    evaluated_at: AwareDatetime
    period_start: AwareDatetime
    period_end: AwareDatetime
    metrics: EvaluationMetrics
    artifacts: Annotated[tuple[ArtifactRef, ...], Field(min_length=1)]
    data_integrity_violations: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def validate_evaluation(self) -> Self:
        if self.period_start >= self.period_end:
            raise ValueError("evaluation period must be nonempty and ordered")
        if self.period_end > self.evaluated_at:
            raise ValueError("evaluation period cannot end after evaluation")
        keys = [(artifact.artifact_id, artifact.version) for artifact in self.artifacts]
        if len(keys) != len(set(keys)):
            raise ValueError("evaluation artifacts must have unique identities")
        return self

    def validate_against_run(self, run: ExperimentRun) -> None:
        """Bind results to the exact successful run, including its input digests."""
        if (
            self.experiment_id != run.experiment_id
            or self.experiment_digest != run.content_digest()
        ):
            raise ValueError("evaluation reference does not match experiment")
        if run.status is not ExperimentStatus.SUCCEEDED:
            raise ValueError("evaluation requires a successful experiment")
        if self.evaluated_at < run.finished_at:
            raise ValueError("evaluation cannot precede experiment completion")
        if self.period_end > run.dataset_snapshot.observed_through:
            raise ValueError("evaluation period exceeds snapshot freeze")
