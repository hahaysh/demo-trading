from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from ats.backtest.candidates import compare_candidates
from ats.backtest.engines import (
    EngineCase,
    EngineResult,
    TargetEngineCase,
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


def test_full_engine_result_validation_rejects_lookahead_and_bad_cash() -> None:
    case = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
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
                    "price": 100,
                    "cost": 1,
                },
                {
                    "date": "2026-09-24",
                    "side": "SELL",
                    "quantity": 10,
                    "price": 100,
                    "cost": 3,
                },
            ],
            "equity": [100000, 100000, 99899, 99996, 99996],
        }
    )
    validate_engine_result(case, result)
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
