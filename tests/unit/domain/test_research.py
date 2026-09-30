from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from ats.domain.data import DataSnapshot, UniverseMembershipManifest
from ats.domain.governance import (
    PromotionDecision,
    PromotionGate,
    PromotionReview,
    evidence_digest,
)
from ats.domain.policy import PromotionPolicy
from ats.domain.research import EvaluationResult, ExperimentRun
from ats.domain.strategy import ArtifactRef, StrategySpec

FREEZE = datetime(2026, 9, 29, tzinfo=UTC)
START = FREEZE + timedelta(days=1)
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
ARTIFACT = ArtifactRef(artifact_id="research-artifact", version="1.0.0", digest=DIGEST)


def _snapshot() -> DataSnapshot:
    return DataSnapshot(
        snapshot_id="research-snapshot",
        observed_through=FREEZE,
        created_at=FREEZE + timedelta(minutes=1),
        universe_membership=UniverseMembershipManifest(
            manifest_id="historical-universe", as_of=FREEZE, digest=DIGEST
        ),
    )


def _strategy() -> StrategySpec:
    snapshot = _snapshot()
    return StrategySpec.model_validate(
        {
            "name": "Research baseline",
            "version": {
                "strategy_id": UUID(int=1),
                "version_id": UUID(int=2),
                "semantic_version": "1.0.0",
                "created_at": FREEZE,
            },
            "research_hypothesis": "Test momentum after transaction costs.",
            "universe": {
                "membership_snapshot_id": "historical-universe",
                "etf_allowlist_id": "approved-etfs",
            },
            "signal": {
                "family": "MOMENTUM",
                "implementation": ARTIFACT,
                "parameters": (),
            },
            "portfolio": {"method": "EQUAL_WEIGHT", "max_positions": 10},
            "risk_policy": {
                "policy_id": "paper-risk",
                "version": "1.0.0",
                "digest": DIGEST,
            },
            "mutation_policy": {},
            "dataset_snapshot": {
                "snapshot_id": snapshot.snapshot_id,
                "observed_through": FREEZE,
                "digest": snapshot.content_digest(),
            },
            "code_artifact": ARTIFACT,
            "container_image_digest": DIGEST,
            "provenance_hash": DIGEST,
        }
    )


def _run_payload() -> dict[str, object]:
    strategy = _strategy()
    return {
        "experiment_id": UUID(int=3),
        "strategy_version": strategy.version,
        "strategy_digest": strategy.content_digest(),
        "dataset_snapshot": strategy.dataset_snapshot,
        "hypothesis": strategy.research_hypothesis,
        "engine": "NATIVE",
        "engine_artifact": ARTIFACT,
        "code_artifact": ARTIFACT,
        "container_image_digest": DIGEST,
        "dependency_lock_digest": DIGEST,
        "random_seed": 42,
        "started_at": START,
        "finished_at": START + timedelta(minutes=5),
        "status": "SUCCEEDED",
        "artifacts": (ARTIFACT,),
    }


def _result_payload(run: ExperimentRun) -> dict[str, object]:
    return {
        "evaluation_id": UUID(int=4),
        "experiment_id": run.experiment_id,
        "experiment_digest": run.content_digest(),
        "protocol": ARTIFACT,
        "evaluated_at": START + timedelta(minutes=10),
        "period_start": FREEZE - timedelta(days=365),
        "period_end": FREEZE,
        "metrics": {
            "net_oos_sharpe": 0.9,
            "max_drawdown": 0.12,
            "net_benchmark_excess_return": -0.02,
            "observation_count": 250,
        },
        "artifacts": (ARTIFACT,),
        "data_integrity_violations": 0,
    }


def test_round_trip_immutability_and_resolved_lineage() -> None:
    run = ExperimentRun.model_validate(_run_payload())
    rebuilt = ExperimentRun.model_validate_json(run.model_dump_json())
    assert rebuilt == run
    assert rebuilt.content_digest() == run.content_digest()
    rebuilt.validate_inputs(_strategy(), _snapshot())
    result = EvaluationResult.model_validate(_result_payload(run))
    assert EvaluationResult.model_validate_json(result.model_dump_json()) == result
    result.validate_against_run(run)
    with pytest.raises(ValidationError):
        run.__setattr__("random_seed", 100)
    with pytest.raises(ValidationError):
        result.metrics.__setattr__("net_oos_sharpe", 10)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("finished_at", START - timedelta(seconds=1)),
        ("started_at", FREEZE - timedelta(seconds=1)),
        ("started_at", datetime(2026, 9, 30)),
        ("random_seed", True),
        ("random_seed", -1),
        ("random_seed", 2**32),
        ("artifacts", ()),
        ("artifacts", (ARTIFACT, ARTIFACT)),
        ("failure_reason", "Unexpected failure"),
        ("status", "FAILED"),
    ],
)
def test_run_rejects_invalid_receipts(field: str, value: object) -> None:
    payload = _run_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        ExperimentRun.model_validate(payload)


@pytest.mark.parametrize("field", ["strategy_digest", "container_image_digest"])
def test_run_rejects_mismatched_inputs(field: str) -> None:
    payload = _run_payload()
    payload[field] = OTHER_DIGEST
    with pytest.raises(ValueError, match="does not match"):
        ExperimentRun.model_validate(payload).validate_inputs(_strategy(), _snapshot())


def test_run_rejects_wrong_snapshot_and_late_materialization() -> None:
    run = ExperimentRun.model_validate(_run_payload())
    changed = DataSnapshot.model_validate(
        {**_snapshot().model_dump(), "records": (), "snapshot_id": "different-snapshot"}
    )
    with pytest.raises(ValueError, match="snapshot reference"):
        run.validate_inputs(_strategy(), changed)
    payload = _run_payload()
    payload["started_at"] = FREEZE
    with pytest.raises(ValueError, match="snapshot creation"):
        ExperimentRun.model_validate(payload).validate_inputs(_strategy(), _snapshot())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "metric",
    [
        "net_oos_sharpe",
        "max_drawdown",
        "net_benchmark_excess_return",
        "deflated_sharpe_confidence",
    ],
)
def test_metrics_reject_nonfinite_values(metric: str, value: float) -> None:
    run = ExperimentRun.model_validate(_run_payload())
    payload = _result_payload(run)
    payload["metrics"] = {
        **EvaluationResult.model_validate(payload).metrics.model_dump(),
        metric: value,
    }
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("period_start", FREEZE),
        ("period_end", START + timedelta(days=1)),
        ("artifacts", ()),
        ("artifacts", (ARTIFACT, ARTIFACT)),
        ("data_integrity_violations", -1),
        ("approved", True),
    ],
)
def test_result_rejects_invalid_structure(field: str, value: object) -> None:
    payload = _result_payload(ExperimentRun.model_validate(_run_payload()))
    payload[field] = value
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("experiment_id", UUID(int=999)),
        ("experiment_digest", OTHER_DIGEST),
        ("evaluated_at", START),
        ("period_end", FREEZE + timedelta(hours=1)),
    ],
)
def test_result_rejects_wrong_run_or_timing(field: str, value: object) -> None:
    run = ExperimentRun.model_validate(_run_payload())
    payload = _result_payload(run)
    payload[field] = value
    with pytest.raises(ValueError):
        EvaluationResult.model_validate(payload).validate_against_run(run)


def test_failed_run_cannot_supply_evaluation() -> None:
    payload = _run_payload()
    payload.update(status="FAILED", artifacts=(), failure_reason="Runner timed out.")
    run = ExperimentRun.model_validate(payload)
    with pytest.raises(ValueError, match="successful experiment"):
        EvaluationResult.model_validate(_result_payload(run)).validate_against_run(run)


def test_run_digest_binds_environment_and_seed() -> None:
    run = ExperimentRun.model_validate(_run_payload())
    result = EvaluationResult.model_validate(_result_payload(run))
    for field, value in [("random_seed", 43), ("dependency_lock_digest", OTHER_DIGEST)]:
        changed = ExperimentRun.model_validate({**run.model_dump(), field: value})
        with pytest.raises(ValueError, match="reference"):
            result.validate_against_run(changed)


def test_negative_metrics_and_integrity_failures_are_recorded_not_promoted() -> None:
    run = ExperimentRun.model_validate(_run_payload())
    payload = _result_payload(run)
    payload["data_integrity_violations"] = 2
    result = EvaluationResult.model_validate(payload)
    result.validate_against_run(run)
    assert result.metrics.net_benchmark_excess_return < 0
    assert result.metrics.deflated_sharpe_confidence is None
    assert result.data_integrity_violations == 2


def _promotion_evidence() -> tuple[
    PromotionPolicy, tuple[EvaluationResult, ...], tuple[ExperimentRun, ...]
]:
    policy = PromotionPolicy.model_validate(
        {
            "metadata": {
                "policy_id": "promotion-policy",
                "version": "1.0.0",
                "status": "APPROVED",
                "approved_by": "test-operator",
                "approved_at": FREEZE,
            },
            "gates": {
                "min_net_oos_sharpe": 0.8,
                "max_drawdown": 0.15,
                "walk_forward_folds": 5,
                "min_positive_excess_folds": 4,
                "min_deflated_sharpe_confidence": 0.95,
                "min_paper_sessions": 20,
            },
        }
    )
    runs: list[ExperimentRun] = []
    results: list[EvaluationResult] = []
    for index, engine in enumerate(["QLIB", "LEAN"], start=1):
        run = ExperimentRun.model_validate(
            {
                **_run_payload(),
                "experiment_id": UUID(int=10 + index),
                "engine": engine,
            }
        )
        payload = _result_payload(run)
        payload["evaluation_id"] = UUID(int=20 + index)
        payload["metrics"] = {
            **EvaluationResult.model_validate(payload).metrics.model_dump(),
            "deflated_sharpe_confidence": 0.95,
        }
        runs.append(run)
        results.append(EvaluationResult.model_validate(payload))
    return policy, tuple(results), tuple(runs)


def _promotion_payload(
    policy: PromotionPolicy,
    results: tuple[EvaluationResult, ...],
    runs: tuple[ExperimentRun, ...],
) -> dict[str, object]:
    review = PromotionReview.model_validate(
        {
            "decision_id": UUID(int=100),
            "strategy_version_id": runs[0].strategy_version.version_id,
            "strategy_digest": runs[0].strategy_digest,
            "policy": {
                "policy_id": policy.metadata.policy_id,
                "version": policy.metadata.version,
                "digest": evidence_digest(policy),
            },
            "evaluations": tuple(
                {
                    "evaluation_id": result.evaluation_id,
                    "digest": evidence_digest(result),
                }
                for result in results
            ),
            "gates": tuple(
                {"gate": gate, "status": "PASSED", "report": ARTIFACT}
                for gate in PromotionGate
            ),
            "reviewed_at": START + timedelta(minutes=15),
            "rationale": "Synthetic review evidence for contract testing only.",
        }
    )
    return {
        "review": review,
        "outcome": "APPROVED",
        "decided_at": START + timedelta(minutes=20),
        "approval": {
            "approval_id": "test-approval",
            "approver_id": "test-reviewer",
            "approved_at": START + timedelta(minutes=18),
            "review_digest": evidence_digest(review),
            "evidence": ARTIFACT,
        },
    }


def test_promotion_receipt_round_trip_and_resolved_evidence() -> None:
    evidence = _promotion_evidence()
    decision = PromotionDecision.model_validate(_promotion_payload(*evidence))
    rebuilt = PromotionDecision.model_validate_json(decision.model_dump_json())
    assert rebuilt == decision
    rebuilt.validate_evidence(*evidence)
    with pytest.raises(ValidationError):
        rebuilt.review.__setattr__("strategy_digest", OTHER_DIGEST)


@pytest.mark.parametrize("gate", list(PromotionGate))
@pytest.mark.parametrize("status", ["MISSING", "FAILED", "UNKNOWN"])
def test_approval_requires_all_passing_gate_evidence(
    gate: PromotionGate, status: str
) -> None:
    payload = _promotion_payload(*_promotion_evidence())
    original = PromotionDecision.model_validate(payload)
    gates = [item.model_dump() for item in original.review.gates if item.gate != gate]
    if status != "MISSING":
        gates.append({"gate": gate, "status": status, "report": ARTIFACT})
    review = PromotionReview.model_validate(
        {**original.review.model_dump(), "gates": gates}
    )
    payload["review"] = review
    assert original.approval is not None
    payload["approval"] = {
        **original.approval.model_dump(),
        "review_digest": evidence_digest(review),
    }
    with pytest.raises(ValidationError, match="every gate"):
        PromotionDecision.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("review_digest", OTHER_DIGEST),
        ("actor", "SYSTEM"),
        ("approver_id", " "),
        ("approved_at", START),
        ("approved_at", START + timedelta(minutes=30)),
    ],
)
def test_invalid_approval_evidence_is_rejected(field: str, value: object) -> None:
    payload = _promotion_payload(*_promotion_evidence())
    approval = PromotionDecision.model_validate(payload).approval
    assert approval is not None
    payload["approval"] = {**approval.model_dump(), field: value}
    with pytest.raises(ValidationError):
        PromotionDecision.model_validate(payload)


def test_missing_approval_and_changed_review_are_rejected() -> None:
    payload = _promotion_payload(*_promotion_evidence())
    review = PromotionDecision.model_validate(payload).review
    with pytest.raises(ValidationError, match="human approval"):
        PromotionDecision.model_validate({**payload, "approval": None})
    payload["review"] = {**review.model_dump(), "rationale": "A different review."}
    with pytest.raises(ValidationError, match="exact review"):
        PromotionDecision.model_validate(payload)


@pytest.mark.parametrize("outcome", ["REJECTED", "DEFERRED"])
def test_nonapproval_can_record_incomplete_review(outcome: str) -> None:
    evidence = _promotion_evidence()
    payload = _promotion_payload(*evidence)
    review = PromotionDecision.model_validate(payload).review
    with pytest.raises(ValidationError, match="cannot carry"):
        PromotionDecision.model_validate({**payload, "outcome": outcome})
    payload.update(
        outcome=outcome, approval=None, review={**review.model_dump(), "gates": ()}
    )
    PromotionDecision.model_validate(payload).validate_evidence(*evidence)


@pytest.mark.parametrize("field", ["evaluations", "gates"])
def test_review_rejects_duplicate_evidence(field: str) -> None:
    review = PromotionDecision.model_validate(
        _promotion_payload(*_promotion_evidence())
    ).review
    values = review.evaluations if field == "evaluations" else review.gates
    with pytest.raises(ValidationError, match="unique"):
        PromotionReview.model_validate(
            {**review.model_dump(), field: (*values, values[0])}
        )


def test_resolution_rejects_missing_duplicate_or_tampered_inputs() -> None:
    policy, results, runs = _promotion_evidence()
    decision = PromotionDecision.model_validate(
        _promotion_payload(policy, results, runs)
    )
    for supplied in [(), results[:1], (*results, results[0])]:
        with pytest.raises(ValueError, match="evaluation evidence"):
            decision.validate_evidence(policy, supplied, runs)
    for supplied_runs in [(), runs[:1], (*runs, runs[0])]:
        with pytest.raises(ValueError, match="experiment evidence"):
            decision.validate_evidence(policy, results, supplied_runs)
    changed = EvaluationResult.model_validate(
        {**results[0].model_dump(), "data_integrity_violations": 1}
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        decision.validate_evidence(policy, (changed, results[1]), runs)
    changed_policy = PromotionPolicy.model_validate(
        {
            **policy.model_dump(),
            "metadata": {**policy.metadata.model_dump(), "version": "2.0.0"},
        }
    )
    with pytest.raises(ValueError, match="policy reference"):
        decision.validate_evidence(changed_policy, results, runs)


@pytest.mark.parametrize("status", ["DRAFT", "RETIRED"])
def test_approval_cannot_use_inactive_policy(status: str) -> None:
    policy, results, runs = _promotion_evidence()
    policy = PromotionPolicy.model_validate(
        {
            **policy.model_dump(),
            "metadata": {
                **policy.metadata.model_dump(),
                "status": status,
                "approved_at": None,
                "approved_by": None,
            },
        }
    )
    decision = PromotionDecision.model_validate(
        _promotion_payload(policy, results, runs)
    )
    with pytest.raises(ValueError, match="policy approved"):
        decision.validate_evidence(policy, results, runs)


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("net_oos_sharpe", 0.79),
        ("max_drawdown", 0.16),
        ("deflated_sharpe_confidence", None),
        ("deflated_sharpe_confidence", 0.94),
    ],
)
def test_approval_checks_metrics_even_with_passing_attestations(
    metric: str, value: object
) -> None:
    policy, results, runs = _promotion_evidence()
    changed = EvaluationResult.model_validate(
        {
            **results[0].model_dump(),
            "metrics": {**results[0].metrics.model_dump(), metric: value},
        }
    )
    results = (changed, results[1])
    decision = PromotionDecision.model_validate(
        _promotion_payload(policy, results, runs)
    )
    with pytest.raises(ValueError, match="metric gates"):
        decision.validate_evidence(policy, results, runs)


def test_approval_requires_both_certification_engines() -> None:
    policy, results, runs = _promotion_evidence()
    results, runs = results[:1], runs[:1]
    decision = PromotionDecision.model_validate(
        _promotion_payload(policy, results, runs)
    )
    with pytest.raises(ValueError, match="Qlib and LEAN"):
        decision.validate_evidence(policy, results, runs)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("data_integrity_violations", 1, "metric gates"),
        ("evaluated_at", START + timedelta(minutes=16), "review cannot precede"),
        (
            "protocol",
            ArtifactRef(
                artifact_id="other-protocol", version="1.0.0", digest=OTHER_DIGEST
            ),
            "comparable",
        ),
        ("period_start", FREEZE - timedelta(days=200), "comparable"),
    ],
)
def test_resolved_promotion_rejects_inconsistent_results(
    field: str,
    value: object,
    message: str,
) -> None:
    policy, results, runs = _promotion_evidence()
    changed = EvaluationResult.model_validate({**results[0].model_dump(), field: value})
    results = (changed, results[1])
    decision = PromotionDecision.model_validate(
        _promotion_payload(policy, results, runs)
    )
    with pytest.raises(ValueError, match=message):
        decision.validate_evidence(policy, results, runs)


def test_result_for_another_strategy_cannot_be_mixed_into_review() -> None:
    policy, results, runs = _promotion_evidence()
    run = ExperimentRun.model_validate(
        {**runs[1].model_dump(), "strategy_digest": OTHER_DIGEST}
    )
    result = EvaluationResult.model_validate(
        {**results[1].model_dump(), "experiment_digest": run.content_digest()}
    )
    runs, results = (runs[0], run), (results[0], result)
    decision = PromotionDecision.model_validate(
        _promotion_payload(policy, results, runs)
    )
    with pytest.raises(ValueError, match="different strategy"):
        decision.validate_evidence(policy, results, runs)


def test_policy_approval_must_predate_review() -> None:
    policy, results, runs = _promotion_evidence()
    policy = PromotionPolicy.model_validate(
        {
            **policy.model_dump(),
            "metadata": {
                **policy.metadata.model_dump(),
                "approved_at": START + timedelta(minutes=16),
            },
        }
    )
    decision = PromotionDecision.model_validate(
        _promotion_payload(policy, results, runs)
    )
    with pytest.raises(ValueError, match="policy approved"):
        decision.validate_evidence(policy, results, runs)


def test_decision_cannot_predate_review() -> None:
    payload = _promotion_payload(*_promotion_evidence())
    with pytest.raises(ValidationError, match="decision cannot precede"):
        PromotionDecision.model_validate({**payload, "decided_at": START})
