from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ats.backtest.engines import EngineCase
from ats.backtest.portfolio import (
    FactorComposition,
    OosEvidence,
    OosProtocol,
    compile_factors,
    evaluate_oos,
)
from ats.demo import create_synthetic_case
from ats.domain.governance import evidence_digest
from ats.research_validation import (
    FrozenValidation,
    ValidationStore,
    validate_candidate,
    validator_digest,
)
from ats.rsi import (
    ResearchBudget,
    ResearchCampaign,
    ResearchError,
    ResearchStore,
    ResearchTrial,
)


def campaign(tmp_path: Path) -> ResearchCampaign:
    _, parent, _, _ = create_synthetic_case(tmp_path / "template")
    case = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    return ResearchCampaign(
        campaign_id="fixture-research",
        parent=parent,
        case=case,
        budget=ResearchBudget(max_trials=4, max_engine_calls=8, max_seconds=1200),
        proposer_image="sha256:" + "a" * 64,
        proposer_code="sha256:" + "b" * 64,
        qlib_lock="sha256:" + "c" * 64,
        lean_lock="sha256:" + "d" * 64,
        created_at=datetime.now(UTC),
    )


def test_research_ledger_fences_workers_and_never_changes_champion(
    tmp_path: Path,
) -> None:
    configured = campaign(tmp_path)
    store = ResearchStore(tmp_path / "research.sqlite3")
    now = datetime.now(UTC)
    token = store.claim(configured, at=now)
    with pytest.raises(ResearchError, match="leased"):
        ResearchStore(store.database).claim(configured, at=now)
    trial = ResearchTrial(
        campaign_digest=evidence_digest(configured),
        sequence=0,
        candidate=configured.parent,
        parameters=(2,),
        hypothesis="fixture",
        memory_digest="sha256:" + "e" * 64,
        proposer_receipt={},
        created_at=now,
        status="RUNNING",
    )
    store.save(configured, token, trial, at=now)
    newer = now + timedelta(seconds=451)
    next_token = ResearchStore(store.database).claim(configured, at=newer)
    assert (
        next_token != token
        and store.trials(configured.campaign_id)[0].status == "FAILED"
    )
    with pytest.raises(ResearchError, match="stale"):
        store.save(configured, token, trial, at=newer)
    assert store.trials(configured.campaign_id)[0].promoted is False


def test_research_budget_cannot_be_mutated_in_place(tmp_path: Path) -> None:
    configured = campaign(tmp_path)
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.register(configured)
    changed = configured.model_copy(
        update={"budget": configured.budget.model_copy(update={"max_trials": 10})}
    )
    with pytest.raises(ResearchError, match="immutable"):
        store.register(changed)


def test_factor_composition_never_uses_future_prices_or_rumor_alone(
    tmp_path: Path,
) -> None:
    case = campaign(tmp_path).case
    trend = compile_factors(
        case, FactorComposition(lookback=2), instrument="krx-test", information=()
    )
    reverse = compile_factors(
        case,
        FactorComposition(lookback=2, direction=-1),
        instrument="krx-test",
        information=(),
    )
    gated = compile_factors(
        case,
        FactorComposition(lookback=2, require_official=True),
        instrument="krx-test",
        information=(),
    )
    assert trend.targets == (0, 0, 10, 0, 0)
    assert reverse.targets == (0, 0, 0, 10, 10)
    assert gated.targets == (0, 0, 0, 0, 0)
    changed = case.model_copy(update={"close": (*case.close[:-1], 101.0)})
    assert (
        compile_factors(
            changed,
            FactorComposition(lookback=2),
            instrument="krx-test",
            information=(),
        ).targets
        == trend.targets
    )


def test_operator_guard_precedes_research_budget_and_engine_access(
    tmp_path: Path,
) -> None:
    from ats.rsi import run_research_cycle

    configured = campaign(tmp_path)
    store = ResearchStore(tmp_path / "guarded.sqlite3")

    def deny(target: str) -> None:
        raise ValueError("synthetic operator halt")

    with pytest.raises(ValueError, match="operator halt"):
        run_research_cycle(
            store,
            configured,
            qlib_lock=tmp_path / "missing",
            lean_lock=tmp_path / "missing",
            research_guard=deny,
        )
    assert not store.database.exists()


def test_oos_rejects_late_candidate_selection_before_engine(tmp_path: Path) -> None:
    configured = campaign(tmp_path)
    first = configured.case.dates[0]
    dates = tuple(first + timedelta(days=index) for index in range(14))
    case = configured.case.model_copy(
        update={
            "dates": dates,
            "next_session": dates[-1] + timedelta(days=1),
            "open": (100.0,) * 14,
            "close": (100.0,) * 14,
            "volume": (100000,) * 14,
        }
    )
    with pytest.raises(ValueError, match="selected after"):
        evaluate_oos(
            case,
            FactorComposition(lookback=2),
            OosProtocol(
                minimum_train=3, test_size=3, label_horizon=1, embargo=1, total_trials=4
            ),
            selection_cutoff=datetime(2027, 1, 1, tzinfo=UTC),
            information=(),
            instrument="krx-test",
            candidate_digest=configured.parent.content_digest(),
            snapshot_digest=configured.parent.dataset_snapshot.digest,
            image_digest=configured.parent.container_image_digest,
            qlib_lock=tmp_path / "missing",
            lean_lock=tmp_path / "missing",
            guard=lambda: None,
        )


def test_holdout_is_frozen_cached_and_never_becomes_search_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = campaign(tmp_path)
    store = ValidationStore(tmp_path / "research.sqlite3")
    now = datetime.now(UTC)
    lease = store.claim(configured, at=now)
    running = ResearchTrial(
        campaign_digest=evidence_digest(configured),
        sequence=0,
        candidate=configured.parent,
        parameters=(2,),
        hypothesis="synthetic holdout",
        memory_digest="sha256:" + "a" * 64,
        proposer_receipt={},
        created_at=now,
        status="RUNNING",
    )
    store.save(configured, lease, running, at=now)
    trial = running.model_copy(update={"status": "REJECTED", "score": 0.0})
    store.save(configured, lease, trial, at=now, elapsed=1)
    original = configured.case
    dates = original.dates + tuple(
        original.next_session + timedelta(days=index) for index in range(10)
    )
    market = EngineCase.model_validate(
        {
            **original.model_dump(),
            "dates": dates,
            "next_session": dates[-1] + timedelta(days=1),
            "open": original.open + (100.0,) * 10,
            "close": original.close + (100.0,) * 10,
            "volume": original.volume + (100000,) * 10,
        }
    )
    protocol = OosProtocol(
        minimum_train=5, test_size=3, label_horizon=1, embargo=1, total_trials=1
    )
    request = FrozenValidation(
        campaign=configured,
        candidate=trial,
        market=market,
        protocol=protocol,
        validator_digest=validator_digest(),
    )
    evidence = OosEvidence(
        protocol_digest=evidence_digest(protocol),
        candidate_digest=trial.candidate.content_digest(),
        case_digest=evidence_digest(market),
        selection_cutoff=configured.parent.dataset_snapshot.observed_through,
        fold_results=(),
        corrected_lower_bound=-0.1,
    )
    calls: list[int] = []

    def evaluate(*args: object, **kwargs: object) -> OosEvidence:
        calls.append(1)
        return evidence

    monkeypatch.setattr("ats.research_validation.evaluate_oos", evaluate)
    assert (
        validate_candidate(
            store,
            request,
            qlib_lock=tmp_path / "unused",
            lean_lock=tmp_path / "unused",
            guard=lambda target: None,
        )
        == evidence
    )
    assert (
        validate_candidate(
            ValidationStore(store.database),
            request,
            qlib_lock=tmp_path / "unused",
            lean_lock=tmp_path / "unused",
            guard=lambda target: None,
        )
        == evidence
    )
    assert calls == [1] and store.trials(configured.campaign_id) == (trial,)
    with pytest.raises(ResearchError, match="frozen for holdout"):
        store.claim(configured, at=now)
    altered = request.model_copy(
        update={"protocol": protocol.model_copy(update={"cost_multipliers": (1,)})}
    )
    with pytest.raises(ResearchError, match="already frozen"):
        validate_candidate(
            store,
            altered,
            qlib_lock=tmp_path / "unused",
            lean_lock=tmp_path / "unused",
            guard=lambda target: None,
        )
