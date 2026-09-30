"""Promotion review receipts; these models do not authorize activation."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ats.domain.policy import PolicyStatus, PromotionPolicy
from ats.domain.research import EvaluationEngine, EvaluationResult, ExperimentRun
from ats.domain.strategy import (
    ArtifactRef,
    FrozenModel,
    Identifier,
    NonEmptyText,
    PolicyRef,
    Sha256Digest,
)


def evidence_digest(evidence: FrozenModel) -> str:
    """Hash a complete validated evidence document using canonical JSON keys."""
    payload = json.dumps(
        evidence.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class PromotionOutcome(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


class PromotionGate(StrEnum):
    NET_OOS_SHARPE = "NET_OOS_SHARPE"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    WALK_FORWARD = "WALK_FORWARD"
    DEFLATED_SHARPE = "DEFLATED_SHARPE"
    DUAL_ENGINE = "DUAL_ENGINE"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    PAPER_SESSIONS = "PAPER_SESSIONS"
    HARD_RISK = "HARD_RISK"
    REALIZED_SLIPPAGE = "REALIZED_SLIPPAGE"
    REPRODUCIBILITY = "REPRODUCIBILITY"


class GateStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class GateAttestation(FrozenModel):
    gate: PromotionGate
    status: GateStatus
    report: ArtifactRef


class EvaluationRef(FrozenModel):
    evaluation_id: UUID
    digest: Sha256Digest


class PromotionReview(FrozenModel):
    """The exact review subject to which an approval is bound."""

    decision_id: UUID
    strategy_version_id: UUID
    strategy_digest: Sha256Digest
    policy: PolicyRef
    evaluations: Annotated[tuple[EvaluationRef, ...], Field(min_length=1)]
    gates: tuple[GateAttestation, ...] = ()
    reviewed_at: AwareDatetime
    rationale: NonEmptyText

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> Self:
        ids = [item.evaluation_id for item in self.evaluations]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation references must be unique")
        gates = [item.gate for item in self.gates]
        if len(gates) != len(set(gates)):
            raise ValueError("gate attestations must be unique")
        return self


class HumanApproval(FrozenModel):
    approval_id: Identifier
    actor: Literal["HUMAN"] = "HUMAN"
    approver_id: NonEmptyText
    approved_at: AwareDatetime
    review_digest: Sha256Digest
    evidence: ArtifactRef


class PromotionDecision(FrozenModel):
    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["PromotionDecision"] = "PromotionDecision"
    review: PromotionReview
    outcome: PromotionOutcome
    decided_at: AwareDatetime
    approval: HumanApproval | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.decided_at < self.review.reviewed_at:
            raise ValueError("decision cannot precede review")
        if self.outcome is not PromotionOutcome.APPROVED:
            if self.approval is not None:
                raise ValueError("non-approved decisions cannot carry an approval")
            return self
        if self.approval is None:
            raise ValueError("approved decisions require human approval evidence")
        if self.approval.review_digest != evidence_digest(self.review):
            raise ValueError("human approval does not match the exact review")
        if not self.review.reviewed_at <= self.approval.approved_at <= self.decided_at:
            raise ValueError("approval must occur between review and decision")
        gates = {item.gate: item.status for item in self.review.gates}
        if set(gates) != set(PromotionGate) or any(
            status is not GateStatus.PASSED for status in gates.values()
        ):
            raise ValueError("approval requires every gate to have passing evidence")
        return self

    def validate_evidence(
        self,
        policy: PromotionPolicy,
        evaluations: tuple[EvaluationResult, ...],
        runs: tuple[ExperimentRun, ...],
    ) -> None:
        """Resolve receipts, not artifact bytes, signatures, or reviewer authority."""
        expected = self.review.policy
        if (
            expected.policy_id != policy.metadata.policy_id
            or expected.version != policy.metadata.version
            or expected.digest != evidence_digest(policy)
        ):
            raise ValueError("promotion policy reference mismatch")
        results = {result.evaluation_id: result for result in evaluations}
        referenced_ids = {item.evaluation_id for item in self.review.evaluations}
        if len(results) != len(evaluations) or set(results) != referenced_ids:
            raise ValueError(
                "evaluation evidence is missing, duplicated, or unexpected"
            )
        experiments = {run.experiment_id: run for run in runs}
        if len(experiments) != len(runs) or set(experiments) != {
            result.experiment_id for result in evaluations
        }:
            raise ValueError(
                "experiment evidence is missing, duplicated, or unexpected"
            )
        for reference in self.review.evaluations:
            result = results[reference.evaluation_id]
            if reference.digest != evidence_digest(result):
                raise ValueError("evaluation digest mismatch")
            run = experiments[result.experiment_id]
            result.validate_against_run(run)
            if (
                run.strategy_version.version_id != self.review.strategy_version_id
                or run.strategy_digest != self.review.strategy_digest
            ):
                raise ValueError("evaluation belongs to a different strategy")
            if result.evaluated_at > self.review.reviewed_at:
                raise ValueError("review cannot precede its evaluations")
        if self.outcome is not PromotionOutcome.APPROVED:
            return
        if (
            policy.metadata.status is not PolicyStatus.APPROVED
            or policy.metadata.approved_at is None
            or policy.metadata.approved_at > self.review.reviewed_at
        ):
            raise ValueError("promotion requires a policy approved before review")
        if not {EvaluationEngine.QLIB, EvaluationEngine.LEAN}.issubset(
            {run.engine for run in runs}
        ):
            raise ValueError("promotion requires Qlib and LEAN evaluation evidence")
        baseline = evaluations[0]
        snapshot = runs[0].dataset_snapshot
        if any(run.dataset_snapshot != snapshot for run in runs) or any(
            (result.protocol, result.period_start, result.period_end)
            != (baseline.protocol, baseline.period_start, baseline.period_end)
            for result in evaluations
        ):
            raise ValueError("evaluation inputs and protocols must be comparable")
        for result in evaluations:
            metrics = result.metrics
            if (
                metrics.net_oos_sharpe < policy.gates.min_net_oos_sharpe
                or metrics.max_drawdown > policy.gates.max_drawdown
                or metrics.deflated_sharpe_confidence is None
                or metrics.deflated_sharpe_confidence
                < policy.gates.min_deflated_sharpe_confidence
                or result.data_integrity_violations != 0
            ):
                raise ValueError("evaluation fails promotion metric gates")
