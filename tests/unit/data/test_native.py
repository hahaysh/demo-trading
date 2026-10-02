from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from ats.backtest.candidates import compare_candidates
from ats.backtest.engines import (
    CorporateAction,
    EngineAsset,
    EngineCase,
    EngineResult,
    SharedEngineCase,
    TargetEngineCase,
    apply_corporate_actions,
    engine_results_match,
    execution_price,
    shared_account_reference,
    validate_engine_result,
)
from ats.backtest.native import SimulationAssumptions, run_native_backtest
from ats.data.artifacts import LocalArtifactResolver
from ats.data.replay import InputReplayRequest
from ats.demo import run_demo
from ats.domain.data import DataSnapshot, PointInTimeRecord, UniverseMembershipManifest
from ats.domain.prices import DailyPrice
from ats.domain.strategy import ArtifactRef, StrategySpec
from ats.domain.universe import UniverseMembershipArtifact


def test_corporate_action_ownership_and_payment_are_separate() -> None:
    from datetime import time

    from ats.domain.prices import SEOUL

    base = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    evidence = ArtifactRef(
        artifact_id="synthetic-action", version="1", digest="sha256:" + "a" * 64
    )
    known = datetime.combine(base.dates[0], time(7), tzinfo=SEOUL)
    split = CorporateAction(
        action_id="synthetic-split",
        kind="FORWARD_SPLIT",
        session=base.dates[1],
        known_at=known,
        evidence=evidence,
        new_shares_per_old=2,
    )
    dividend = CorporateAction(
        action_id="synthetic-dividend",
        kind="NET_CASH_DIVIDEND",
        session=base.dates[2],
        payment_session=base.dates[4],
        known_at=known,
        evidence=evidence,
        net_cash_per_share=1,
    )
    case = EngineCase.model_validate(
        {
            **base.model_dump(),
            "open": (100, 50, 49, 49, 49),
            "close": (100, 50, 49, 49, 49),
            "corporate_actions": (split, dividend),
        }
    )
    pending: dict[str, float] = {}
    assert apply_corporate_actions(case, 1, 10, pending) == (20, 0)
    assert apply_corporate_actions(case, 2, 20, pending) == (20, 0)
    assert sum(pending.values()) == 20
    assert apply_corporate_actions(case, 4, 0, pending) == (0, 20)
    assert not pending
    target = TargetEngineCase.model_validate(
        {
            **case.model_dump(),
            "quantity": 20,
            "targets": (10, 20, 0, 0, 0),
            "decided_at": tuple(
                datetime.combine(session, time(8), tzinfo=SEOUL)
                for session in case.dates
            ),
            "signal_artifact": evidence,
        }
    )
    shared = SharedEngineCase.model_validate(
        {
            **case.model_dump(),
            "assets": (
                EngineAsset(instrument_id="krx-one", case=target),
                EngineAsset(instrument_id="krx-two", case=target),
            ),
        }
    )
    fills, equity = shared_account_reference(shared)
    assert [fill.quantity for fill in fills] == [10, 10, 20, 20]
    assert equity[-1] == pytest.approx(99992.12)
    from ats.backtest.portfolio import FactorComposition, compile_factors

    with pytest.raises(ValueError, match="adjustment protocol"):
        compile_factors(
            case, FactorComposition(lookback=2), instrument="krx-one", information=()
        )
    adjusted = compile_factors(
        case,
        FactorComposition(lookback=2, price_adjustment="TOTAL_RETURN"),
        instrument="krx-one",
        information=(),
    )
    assert adjusted.targets == (0,) * 5
    changed = case.model_copy(update={"close": (*case.close[:-1], 55.0)})
    assert (
        compile_factors(
            changed,
            FactorComposition(lookback=2, price_adjustment="TOTAL_RETURN"),
            instrument="krx-one",
            information=(),
        ).targets
        == adjusted.targets
    )
    assert adjusted.open == case.open and adjusted.close == case.close
    with pytest.raises(ValueError, match="known terms"):
        EngineCase.model_validate(
            {
                **case.model_dump(),
                "corporate_actions": (
                    split.model_copy(
                        update={
                            "known_at": datetime.combine(
                                split.session, time(9), tzinfo=SEOUL
                            )
                        }
                    ),
                    dividend,
                ),
            }
        )


def test_tick_rounding_is_adverse_and_cannot_create_free_execution() -> None:
    assert execution_price(100, buy=True, slippage=0.001, tick=1) == 101
    assert execution_price(100, buy=False, slippage=0.001, tick=1) == 99
    case = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    assert (
        EngineCase.model_validate(
            {**case.model_dump(), "slippage": 0.001, "price_tick": 1}
        ).price_tick
        == 1
    )
    with pytest.raises(ValueError, match="nonpositive"):
        EngineCase.model_validate({**case.model_dump(), "price_tick": 1000})


def test_shared_capacity_leaves_unfilled_target_without_borrowing() -> None:
    from ats.backtest.portfolio import FactorComposition, compile_factors

    base = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    base = EngineCase.model_validate(
        {**base.model_dump(), "opening_capacity": (0, 0, 3, 2, 1)}
    )
    target = compile_factors(
        base, FactorComposition(lookback=2), instrument="krx-one", information=()
    )
    case = SharedEngineCase.model_validate(
        {
            **base.model_dump(),
            "assets": (
                EngineAsset(instrument_id="krx-one", case=target),
                EngineAsset(instrument_id="krx-two", case=target),
            ),
        }
    )
    fills, equity = shared_account_reference(case)
    assert [fill.quantity for fill in fills] == [3, 3, 2, 2, 1, 1]
    assert equity[-1] == pytest.approx(99997.6)
    with pytest.raises(ValueError, match="capacity"):
        EngineCase.model_validate({**base.model_dump(), "opening_capacity": (1,)})


def test_shared_settlement_rejects_buy_using_unsettled_sale_proceeds() -> None:
    from datetime import time

    from ats.domain.prices import SEOUL

    base = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    base = EngineCase.model_validate(
        {**base.model_dump(), "initial_cash": 10000.0, "close": (100.0,) * 5}
    )
    assets: list[EngineAsset] = []
    signal = ArtifactRef(
        artifact_id="synthetic-settlement-signal",
        version="1",
        digest="sha256:" + "a" * 64,
    )
    for index in range(10):
        targets = (
            (10, 0, 0, 0, 0)
            if index == 0
            else (0, 9, 9, 9, 9)
            if index == 9
            else (10,) * 5
        )
        target = TargetEngineCase.model_validate(
            {
                **base.model_dump(),
                "open": (110.5,) * 5 if index == 9 else base.open,
                "close": (110.5,) * 5 if index == 9 else base.close,
                "quantity": 9 if index == 9 else 10,
                "targets": targets,
                "decided_at": tuple(
                    datetime.combine(session, time(8), tzinfo=SEOUL)
                    for session in base.dates
                ),
                "signal_artifact": signal,
            }
        )
        assets.append(EngineAsset(instrument_id=f"krx-{index}", case=target))
    immediate = SharedEngineCase.model_validate({**base.model_dump(), "assets": assets})
    assert shared_account_reference(immediate)[1][-1] > 9900
    delayed = SharedEngineCase.model_validate(
        {**immediate.model_dump(), "cash_settlement_sessions": 2}
    )
    with pytest.raises(ValueError, match="unsettled"):
        shared_account_reference(delayed)
    waiting = assets[-1].model_copy(
        update={"case": assets[-1].case.model_copy(update={"targets": (0, 0, 0, 9, 9)})}
    )
    settled = SharedEngineCase.model_validate(
        {**delayed.model_dump(), "assets": (*assets[:-1], waiting)}
    )
    assert shared_account_reference(settled)[1][-1] == pytest.approx(
        shared_account_reference(immediate)[1][-1]
    )


def test_versioned_target_stream_rejects_same_open_decisions() -> None:
    from datetime import time

    from ats.domain.prices import SEOUL

    case = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    payload = {
        **case.model_dump(),
        "targets": (0, 0, 10, 0, 0),
        "decided_at": tuple(
            datetime.combine(session, time(6), tzinfo=SEOUL) for session in case.dates
        ),
        "signal_artifact": {
            "artifact_id": "fixture-signal",
            "version": "1",
            "digest": "sha256:" + "a" * 64,
        },
    }
    assert TargetEngineCase.model_validate(payload).targets[2] == 10
    payload["decided_at"] = tuple(
        datetime.combine(session, time(9), tzinfo=SEOUL) for session in case.dates
    )
    with pytest.raises(ValueError, match="unavailable"):
        TargetEngineCase.model_validate(payload)


def test_shared_account_counts_initial_cash_once_and_rejects_duplicate_symbols() -> (
    None
):
    from ats.backtest.portfolio import FactorComposition, compile_factors

    base = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    target = compile_factors(
        base, FactorComposition(lookback=2), instrument="krx-one", information=()
    )
    assets = (
        EngineAsset(instrument_id="krx-one", case=target),
        EngineAsset(instrument_id="krx-two", case=target),
    )
    case = SharedEngineCase.model_validate({**base.model_dump(), "assets": assets})
    fills, equity = shared_account_reference(case)
    assert len(fills) == 4 and equity[0] == 100000 and equity[-1] == 99992
    assert {fill.instrument_id for fill in fills} == {"krx-one", "krx-two"}
    concentrated = target.model_copy(update={"initial_cash": 11000.0})
    gapped = concentrated.model_copy(
        update={
            "open": (100.0, 100.0, 100.0, 80.0, 100.0),
            "close": (100.0, 110.0, 100.0, 100.0, 100.0),
        }
    )
    gap_case = SharedEngineCase.model_validate(
        {
            **base.model_dump(),
            "initial_cash": 11000.0,
            "assets": (
                EngineAsset(instrument_id="krx-one", case=gapped),
                EngineAsset(instrument_id="krx-two", case=gapped),
            ),
        }
    )
    with pytest.raises(ValueError, match="loss halt"):
        shared_account_reference(gap_case)
    with pytest.raises(ValueError, match="unique"):
        SharedEngineCase.model_validate(
            {**base.model_dump(), "assets": (assets[0], assets[0])}
        )


def test_shared_portfolio_checks_guard_and_price_availability_before_engines() -> None:
    from datetime import time

    from ats.backtest.portfolio import (
        FactorComposition,
        PortfolioAsset,
        PortfolioRequest,
        compile_factors,
        evaluate_shared_portfolio,
    )
    from ats.domain.prices import SEOUL

    base = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    target = compile_factors(
        base, FactorComposition(lookback=2), instrument="krx-one", information=()
    )
    asset = PortfolioAsset(
        instrument_id="krx-one",
        asset_class="EQUITY",
        case=target,
        eligible=(True,) * 5,
        tradable=(True,) * 5,
        classification_known_at=(target.decided_at[0],) * 5,
        price_observed_at=tuple(
            datetime.combine(session, time(15, 31), tzinfo=SEOUL)
            for session in base.dates
        ),
        corporate_actions=("NONE",) * 5,
    )
    request = PortfolioRequest(
        assets=(asset, asset.model_copy(update={"instrument_id": "krx-two"})),
        strategy_digest="sha256:" + "a" * 64,
        snapshot_digest="sha256:" + "b" * 64,
        image_digest="sha256:" + "c" * 64,
    )

    def deny() -> None:
        raise ValueError("operator halted")

    with pytest.raises(ValueError, match="halted"):
        evaluate_shared_portfolio(
            request, qlib_lock=Path("absent"), lean_lock=Path("absent"), guard=deny
        )
    with pytest.raises(ValueError, match="before close"):
        PortfolioAsset.model_validate(
            {**asset.model_dump(), "price_observed_at": target.decided_at}
        )
    with pytest.raises(ValueError, match="future historical"):
        PortfolioAsset.model_validate(
            {
                **asset.model_dump(),
                "classification_known_at": tuple(
                    value + timedelta(minutes=1) for value in target.decided_at
                ),
            }
        )
    with pytest.raises(ValueError, match="suspended"):
        PortfolioAsset.model_validate(
            {
                **asset.model_dump(),
                "eligible": (True, True, True, False, False),
                "tradable": (True, True, True, False, False),
            }
        )


def test_purged_folds_do_not_train_on_future_labels_and_correct_search_bias() -> None:
    from ats.backtest.portfolio import corrected_mean_lower_bound, purged_folds

    folds = purged_folds(
        observations=100, minimum_train=30, test_size=10, label_horizon=3, embargo=2
    )
    assert len(folds) >= 2
    assert all(fold.train_end + 3 + 2 <= fold.test_start for fold in folds)
    assert all(
        previous.test_end + 2 <= current.test_start
        for previous, current in zip(folds, folds[1:], strict=False)
    )
    returns = (0.01, -0.02, 0.03, 0.015, 0.02)
    assert corrected_mean_lower_bound(returns, trials=100) < corrected_mean_lower_bound(
        returns, trials=1
    )


def _fixture(
    root: Path,
) -> tuple[
    InputReplayRequest, StrategySpec, tuple[datetime, ...], SimulationAssumptions
]:
    import hashlib

    def store(payload: bytes) -> str:
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        directory = root / "sha256"
        directory.mkdir(exist_ok=True)
        (directory / digest[7:]).write_bytes(payload)
        return digest

    digest = store(b"synthetic evidence")
    artifact = ArtifactRef(artifact_id="synthetic", version="1", digest=digest)
    start = datetime(2026, 9, 21, 6, 30, tzinfo=UTC)
    cutoffs = tuple(start + timedelta(days=index) for index in range(5))
    records: list[PointInTimeRecord] = []
    for index, close in enumerate((100, 110, 90, 80, 100)):
        price = DailyPrice.model_validate(
            {
                "instrument_id": "krx-test",
                "session": cutoffs[index].date(),
                "session_close": cutoffs[index],
                "open": 100,
                "high": 120,
                "low": 70,
                "close": close,
                "volume": 100000,
            }
        )
        records.append(
            PointInTimeRecord.model_validate(
                {
                    "source_id": "synthetic-prices",
                    "source_item_id": f"day-{index}",
                    "revision": "rev-001",
                    "instrument_id": "krx-test",
                    "observed_at": cutoffs[index],
                    "effective_at": cutoffs[index],
                    "rights_class": "APPROVED_PUBLIC",
                    "credibility_tier": "PRIMARY",
                    "content_hash": price.content_digest(),
                    "raw_payload_digest": store(price.model_dump_json().encode()),
                }
            )
        )
    universe = UniverseMembershipArtifact.model_validate(
        {
            "manifest_id": "synthetic-universe",
            "as_of": cutoffs[-1],
            "members": (
                {
                    "instrument_id": "krx-test",
                    "asset_class": "EQUITY",
                    "membership_basis": "KOSPI_200",
                    "observed_at": start,
                    "effective_from": start,
                    "evidence": artifact,
                },
            ),
        }
    )
    snapshot = DataSnapshot(
        snapshot_id="synthetic-snapshot",
        observed_through=cutoffs[-1],
        created_at=cutoffs[-1],
        universe_membership=UniverseMembershipManifest(
            manifest_id=universe.manifest_id,
            as_of=cutoffs[-1],
            digest=store(universe.model_dump_json().encode()),
        ),
        records=tuple(records),
    )
    request = InputReplayRequest.model_validate(
        {
            "snapshot": snapshot,
            "cutoffs": cutoffs,
            "data_requirements": (
                {
                    "requirement_id": "prices",
                    "source_id": "synthetic-prices",
                    "min_records": 1,
                    "freshness_basis": "EFFECTIVE",
                    "max_age_seconds": 10 * 86400,
                },
            ),
            "source_policy": {
                "metadata": {
                    "policy_id": "synthetic-policy",
                    "version": "1",
                    "status": "APPROVED",
                    "approved_by": "synthetic-test-only",
                    "approved_at": start,
                },
                "sources": (
                    {
                        "source_id": "synthetic-prices",
                        "category": "MARKET",
                        "enabled": True,
                        "legal_review": "APPROVED",
                        "rights": {"classification": "APPROVED_PUBLIC"},
                        "rate_limit_per_minute": 1,
                        "notes": "Synthetic tests only.",
                    },
                ),
            },
        }
    )
    strategy = StrategySpec.model_validate(
        {
            "name": "Synthetic trend baseline",
            "version": {
                "strategy_id": UUID(int=1),
                "version_id": UUID(int=2),
                "semantic_version": "1.0.0",
                "created_at": start,
            },
            "research_hypothesis": "Synthetic next-open trend test.",
            "universe": {
                "membership_snapshot_id": universe.manifest_id,
                "etf_allowlist_id": "synthetic-etfs",
            },
            "signal": {
                "family": "TREND",
                "implementation": artifact,
                "parameters": ({"name": "signal.lookback_days", "value": 2},),
            },
            "portfolio": {"method": "EQUAL_WEIGHT", "max_positions": 10},
            "risk_policy": {
                "policy_id": "synthetic-risk",
                "version": "1",
                "digest": digest,
            },
            "mutation_policy": {
                "allowed_parameters": (
                    {
                        "name": "signal.lookback_days",
                        "minimum": 2,
                        "maximum": 5,
                        "step": 1,
                    },
                )
            },
            "dataset_snapshot": {
                "snapshot_id": snapshot.snapshot_id,
                "observed_through": cutoffs[-1],
                "digest": snapshot.content_digest(),
            },
            "code_artifact": artifact,
            "container_image_digest": digest,
            "provenance_hash": digest,
        }
    )
    assumptions = SimulationAssumptions(
        initial_cash=Decimal(100000),
        commission_bps=10,
        sell_tax_bps=20,
        slippage_bps=10,
        max_symbol_weight=Decimal("0.10"),
        participation_bps=1000,
    )
    return (
        request,
        strategy,
        tuple(cutoff - timedelta(hours=6, minutes=30) for cutoff in cutoffs),
        assumptions,
    )


def test_native_backtest_executes_next_open_and_charges_costs(tmp_path: Path) -> None:
    request, strategy, opens, assumptions = _fixture(tmp_path)
    report = run_native_backtest(
        LocalArtifactResolver(tmp_path),
        request,
        strategy,
        instrument_id="krx-test",
        session_opens=opens,
        assumptions=assumptions,
    )
    assert [fill.side for fill in report.fills] == ["BUY", "SELL"]
    assert report.fills[0].decided_at == request.cutoffs[1]
    assert report.fills[0].filled_at == opens[2]
    assert report.fills[0].price == Decimal("100.1")
    assert all(
        fill.filled_at > fill.decided_at and fill.costs > 0 for fill in report.fills
    )
    assert all(point.cash >= 0 and point.quantity >= 0 for point in report.equity_curve)
    assert report.net_return < 0
    assert report.max_drawdown > 0
    assert report.certified is False
    assert (
        run_native_backtest(
            LocalArtifactResolver(tmp_path),
            request,
            strategy,
            instrument_id="krx-test",
            session_opens=opens,
            assumptions=assumptions,
        ).content_digest()
        == report.content_digest()
    )


def test_native_rejects_missing_calendar_and_wrong_provenance(tmp_path: Path) -> None:
    request, strategy, opens, assumptions = _fixture(tmp_path)
    with pytest.raises(ValueError, match="opens"):
        run_native_backtest(
            LocalArtifactResolver(tmp_path),
            request,
            strategy,
            instrument_id="krx-test",
            session_opens=(),
            assumptions=assumptions,
        )
    with pytest.raises(ValueError, match="missing exact"):
        run_native_backtest(
            LocalArtifactResolver(tmp_path),
            request,
            strategy,
            instrument_id="krx-other",
            session_opens=opens,
            assumptions=assumptions,
        )


def test_demo_is_reproducible_and_does_not_claim_deployment_readiness(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    run_demo(first)
    run_demo(second)
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
    import json

    report = json.loads((first / "report.json").read_text(encoding="utf-8"))
    assert report["deployment_ready"] is False
    assert report["promoted"] is False
    assert len(report["candidates"]) == 3
    assert report["risk_smoke"]["allow"]["outcome"] == "ALLOW"
    assert report["risk_smoke"]["kill_switch"]["outcome"] == "DENY"
    before = (first / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_demo(first)
    assert (first / "report.json").read_bytes() == before


def test_data_to_paper_smoke_is_offline_reproducible_and_not_certified(
    tmp_path: Path,
) -> None:
    first = run_demo(tmp_path / "first", exercise_data_to_paper=True)
    second = run_demo(tmp_path / "second", exercise_data_to_paper=True)
    assert first == second
    assert first["data_to_paper_smoke"] == {
        "mode": "SYNTHETIC_BOUNDARY_SMOKE",
        "raw_roundtrips": 5,
        "expired_rows_removed": 5,
        "paper_status": "UNKNOWN",
        "retry_blocked_after_restart": True,
        "broker_requests_sent": 0,
        "external_engine_runs": 0,
        "certified": False,
    }
    assert first["deployment_ready"] is False
    assert first["promoted"] is False


@pytest.mark.parametrize("slippage", [0.0, 0.01])
def test_full_engine_result_validation_rejects_lookahead_and_bad_cash(
    slippage: float,
) -> None:
    case = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    case = EngineCase.model_validate({**case.model_dump(), "slippage": slippage})
    result = EngineResult.model_validate(
        {
            "engine": "QLIB",
            "version": "0.9.7",
            "full_backtest": True,
            "certified": False,
            "input_sha256": "a" * 64,
            "lock_sha256": "b" * 64,
            "code_sha256": "c" * 64,
            "fills": [
                {
                    "date": "2026-09-23",
                    "side": "BUY",
                    "quantity": 10,
                    "price": 100 * (1 + slippage),
                    "cost": 1 * (1 + slippage),
                },
                {
                    "date": "2026-09-24",
                    "side": "SELL",
                    "quantity": 10,
                    "price": 100 * (1 - slippage),
                    "cost": 3 * (1 - slippage),
                },
            ],
            "equity": [100000, 100000, 99888.99, 99976.02, 99976.02]
            if slippage
            else [100000, 100000, 99899, 99996, 99996],
        }
    )
    validate_engine_result(case, result)
    roundoff = result.model_copy(
        update={"equity": tuple(value + 1e-11 for value in result.equity)}
    )
    assert engine_results_match(result, roundoff)
    changed = result.model_copy(
        update={"equity": (*result.equity[:-1], result.equity[-1] + 0.01)}
    )
    assert not engine_results_match(result, changed)
    assert not engine_results_match(
        result, result.model_copy(update={"input_sha256": "d" * 64})
    )
    with pytest.raises(ValueError, match="conservation"):
        validate_engine_result(
            case, result.model_copy(update={"equity": (100000,) * 5})
        )
    with pytest.raises(ValueError, match="fill"):
        validate_engine_result(
            case, result.model_copy(update={"fills": result.fills[:1]})
        )


@pytest.mark.parametrize("lookbacks", [(3, 3), (3, 4, 5), (), (True,), (100,)])
def test_candidate_search_rejects_budget_duplicates_and_unauthorized_values(
    tmp_path: Path, lookbacks: tuple[int, ...]
) -> None:
    request, strategy, opens, assumptions = _fixture(tmp_path)
    with pytest.raises(ValueError):
        compare_candidates(
            LocalArtifactResolver(tmp_path),
            request,
            strategy,
            instrument_id="krx-test",
            session_opens=opens,
            assumptions=assumptions,
            lookbacks=lookbacks,
            max_trials=2,
        )
