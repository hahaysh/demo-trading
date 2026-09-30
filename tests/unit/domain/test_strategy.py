from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import given, strategies
from pydantic import ValidationError

from ats.domain.strategy import (
    ArtifactRef,
    DatasetSnapshotRef,
    LifecycleActor,
    LifecycleState,
    MutationPolicy,
    ParameterBounds,
    PolicyRef,
    PortfolioMethod,
    PortfolioSpec,
    SignalParameter,
    SignalSpec,
    StrategyFamily,
    StrategyLifecycleEvent,
    StrategySpec,
    StrategyVersion,
    UniverseSpec,
    create_challenger,
)


def _digest(character: str) -> str:
    return f"sha256:{character * 64}"


def _artifact(artifact_id: str, character: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        version="1.0.0",
        digest=_digest(character),
    )


def _root_strategy() -> StrategySpec:
    return StrategySpec(
        name="KOSPI 200 momentum baseline",
        version=StrategyVersion(
            strategy_id=UUID(int=100),
            version_id=UUID(int=1),
            semantic_version="1.0.0",
            created_at=datetime(2026, 9, 30, tzinfo=UTC),
        ),
        research_hypothesis=(
            "Medium-term relative strength persists after modeled trading costs."
        ),
        universe=UniverseSpec(
            membership_snapshot_id="kospi200-20260930",
            etf_allowlist_id="major-etf-v1",
        ),
        features=(_artifact("adjusted-close", "a"),),
        signal=SignalSpec(
            family=StrategyFamily.MOMENTUM,
            implementation=_artifact("momentum-signal", "b"),
            parameters=(
                SignalParameter(name="signal.lookback_days", value=20),
                SignalParameter(name="signal.skip_days", value=5),
            ),
        ),
        portfolio=PortfolioSpec(
            method=PortfolioMethod.EQUAL_WEIGHT,
            max_positions=10,
            cash_buffer_bps=100,
            turnover_budget=0.5,
        ),
        risk_policy=PolicyRef(
            policy_id="paper-risk-policy",
            version="1.0.0",
            digest=_digest("c"),
        ),
        mutation_policy=MutationPolicy(
            allowed_parameters=(
                ParameterBounds(
                    name="signal.lookback_days",
                    minimum=5,
                    maximum=60,
                    step=5,
                ),
            ),
            max_parameter_changes=1,
        ),
        dataset_snapshot=DatasetSnapshotRef(
            snapshot_id="eod-20260930-v1",
            observed_through=datetime(2026, 9, 30, 7, tzinfo=UTC),
            digest=_digest("d"),
        ),
        code_artifact=_artifact("strategy-runtime", "e"),
        container_image_digest=_digest("f"),
        provenance_hash=_digest("1"),
    )


def _child_version(version_id: int, patch: int) -> StrategyVersion:
    return StrategyVersion(
        strategy_id=UUID(int=100),
        version_id=UUID(int=version_id),
        semantic_version=f"1.0.{patch}",
        parent_version_id=UUID(int=1),
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_strategy_is_frozen_and_hash_is_deterministic() -> None:
    strategy = _root_strategy()
    rebuilt = StrategySpec.model_validate_json(strategy.model_dump_json())

    assert rebuilt == strategy
    assert rebuilt.content_digest() == strategy.content_digest()
    assert strategy.content_digest().startswith("sha256:")

    with pytest.raises(ValidationError):
        strategy.__setattr__("name", "Mutated in place")


@given(strategies.sampled_from([5, 10, 15, 25, 30, 35, 40, 45, 50, 55, 60]))
def test_challenger_accepts_only_declared_grid_values(new_value: int) -> None:
    parent = _root_strategy()
    challenger = create_challenger(
        parent,
        _child_version(1_000 + new_value, new_value),
        {"signal.lookback_days": new_value},
        hypothesis="Test a bounded momentum lookback variation.",
        provenance_hash=_digest("2"),
    )

    values = {
        parameter.name: parameter.value for parameter in challenger.signal.parameters
    }
    assert values["signal.lookback_days"] == new_value
    assert parent.signal.parameters[0].value == 20
    assert challenger.version.parent_version_id == parent.version.version_id
    assert challenger.mutations[0].previous_value == 20
    assert challenger.content_digest() != parent.content_digest()


@pytest.mark.parametrize("new_value", [0, 7, 65])
def test_challenger_rejects_values_outside_declared_grid(new_value: int) -> None:
    with pytest.raises(ValueError, match="outside declared bounds"):
        create_challenger(
            _root_strategy(),
            _child_version(200 + new_value, 1),
            {"signal.lookback_days": new_value},
            hypothesis="Attempt an invalid parameter mutation.",
            provenance_hash=_digest("3"),
        )


def test_challenger_rejects_risk_mutation() -> None:
    with pytest.raises(ValueError, match="not permitted"):
        create_challenger(
            _root_strategy(),
            _child_version(300, 1),
            {"risk.max_symbol_weight": 0.2},
            hypothesis="Attempt to bypass the operator-owned risk policy.",
            provenance_hash=_digest("4"),
        )


def test_challenger_requires_exact_parent() -> None:
    version = _child_version(400, 1).model_copy(
        update={"parent_version_id": UUID(int=999)}
    )

    with pytest.raises(ValueError, match="exact parent"):
        create_challenger(
            _root_strategy(),
            version,
            {"signal.lookback_days": 25},
            hypothesis="Attempt to break immutable lineage.",
            provenance_hash=_digest("5"),
        )


def test_invalid_lifecycle_jump_is_rejected() -> None:
    with pytest.raises(ValidationError, match="transition is not allowed"):
        StrategyLifecycleEvent(
            event_id=UUID(int=500),
            strategy_version_id=UUID(int=1),
            from_state=LifecycleState.DRAFT,
            to_state=LifecycleState.CHAMPION,
            actor=LifecycleActor.HUMAN,
            occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
            evidence=(_artifact("promotion-report", "6"),),
            approval_id="approval-001",
        )


def test_champion_promotion_requires_human_approval() -> None:
    with pytest.raises(ValidationError, match="human approval"):
        StrategyLifecycleEvent(
            event_id=UUID(int=600),
            strategy_version_id=UUID(int=1),
            from_state=LifecycleState.PAPER,
            to_state=LifecycleState.CHAMPION,
            actor=LifecycleActor.SYSTEM,
            occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
            evidence=(_artifact("paper-report", "7"),),
        )

    event = StrategyLifecycleEvent(
        event_id=UUID(int=601),
        strategy_version_id=UUID(int=1),
        from_state=LifecycleState.PAPER,
        to_state=LifecycleState.CHAMPION,
        actor=LifecycleActor.HUMAN,
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        evidence=(_artifact("paper-report", "8"),),
        approval_id="approval-001",
    )

    assert event.to_state is LifecycleState.CHAMPION
