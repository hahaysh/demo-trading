"""Run a fully local synthetic data-to-backtest exercise, never paper/live orders."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from ats.backtest.candidates import compare_candidates
from ats.backtest.native import SimulationAssumptions, run_native_backtest
from ats.data.artifacts import LocalArtifactResolver
from ats.data.replay import InputReplayRequest
from ats.domain.data import DataSnapshot, PointInTimeRecord, UniverseMembershipManifest
from ats.domain.execution import OrderIntent
from ats.domain.governance import evidence_digest
from ats.domain.policy import RiskPolicy
from ats.domain.prices import DailyPrice
from ats.domain.strategy import ArtifactRef, StrategySpec
from ats.domain.universe import UniverseMembershipArtifact
from ats.risk.assessor import RiskState, assess_limit_intent


def create_synthetic_case(
    root: Path,
) -> tuple[
    InputReplayRequest, StrategySpec, tuple[datetime, ...], SimulationAssumptions
]:
    """Create deterministic, artificial fixtures. No rights or source approvals implied."""

    def store(payload: bytes) -> str:
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        directory = root / "sha256"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / digest[7:]
        if path.exists() and path.read_bytes() != payload:
            raise ValueError("existing synthetic artifact is corrupt")
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(payload)
        return digest

    digest = store(b"synthetic evidence; not a signed operational artifact")
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


def run_demo(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False)
    request, strategy, opens, assumptions = create_synthetic_case(output)
    resolver = LocalArtifactResolver(output)
    baseline = run_native_backtest(
        resolver,
        request,
        strategy,
        instrument_id="krx-test",
        session_opens=opens,
        assumptions=assumptions,
    )
    candidates = compare_candidates(
        resolver,
        request,
        strategy,
        instrument_id="krx-test",
        session_opens=opens,
        assumptions=assumptions,
        lookbacks=(3, 4, 5),
        max_trials=3,
    )
    fill = baseline.fills[0]
    decision_at = request.cutoffs[-1]
    paper_open = datetime(2026, 9, 28, tzinfo=UTC)
    policy = RiskPolicy.model_validate(
        {
            "metadata": {
                "policy_id": "synthetic-risk",
                "version": "1",
                "status": "APPROVED",
                "approved_by": "synthetic-test-only",
                "approved_at": request.cutoffs[0],
            },
            "limits": {
                "max_symbol_weight": 0.1,
                "max_gross_exposure": 1,
                "daily_portfolio_loss_halt": 0.01,
                "portfolio_drawdown_halt": 0.15,
                "max_price_age_seconds": 60,
            },
        }
    )
    intent = OrderIntent.model_validate(
        {
            "intent_id": UUID(int=100),
            "client_order_id": "synthetic-order",
            "account_id": "synthetic-account",
            "strategy_version_id": strategy.version.version_id,
            "strategy_digest": strategy.content_digest(),
            "champion_selection": strategy.code_artifact,
            "dataset_snapshot": strategy.dataset_snapshot,
            "signal": strategy.signal.implementation,
            "risk_policy": {
                "policy_id": policy.metadata.policy_id,
                "version": policy.metadata.version,
                "digest": evidence_digest(policy),
            },
            "instrument_id": "krx-test",
            "asset_class": "EQUITY",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": fill.quantity,
            "limit_price": fill.price,
            "signal_session": decision_at.date(),
            "execution_session": paper_open.date(),
            "created_at": decision_at,
            "expires_at": paper_open + timedelta(minutes=1),
        }
    )
    state = RiskState.model_validate(
        {
            "account_id": intent.account_id,
            "instrument_id": intent.instrument_id,
            "observed_at": paper_open,
            "valid_until": intent.expires_at,
            "equity": assumptions.initial_cash,
            "cash": assumptions.initial_cash,
            "gross_exposure": 0,
            "reserved_buy_notional": 0,
            "reserved_cash": 0,
            "held_quantity": 0,
            "reserved_sell_quantity": 0,
            "quote_price": fill.price,
            "quote_at": paper_open,
            "daily_loss_fraction": 0,
            "drawdown_fraction": 0,
            "champion_version_id": strategy.version.version_id,
            "champion_digest": strategy.content_digest(),
            "champion_selection": intent.champion_selection,
            "universe_members": (intent.instrument_id,),
            "duplicate_client_order_id": False,
            "kill_switch_active": False,
            "opening_window_start": paper_open,
            "opening_window_end": intent.expires_at,
            "fee_reserve_bps": 10,
        }
    )
    allow = assess_limit_intent(
        intent, policy, state, at=paper_open, assessor=strategy.code_artifact
    )
    halted = RiskState.model_validate(
        {**state.model_dump(), "kill_switch_active": True}
    )
    deny = assess_limit_intent(
        intent, policy, halted, at=paper_open, assessor=strategy.code_artifact
    )
    report: dict[str, object] = {
        "mode": "SYNTHETIC_OFFLINE_ONLY",
        "deployment_ready": False,
        "promoted": False,
        "baseline": baseline.model_dump(mode="json"),
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        "risk_smoke": {
            "allow": allow.model_dump(mode="json"),
            "kill_switch": deny.model_dump(mode="json"),
        },
        "blockers": [
            "Approved real-data sources",
            "Qlib and LEAN integration and certification",
            "Independent runtime risk and KIS paper reconciliation",
            "Authenticated human approvals",
            "At least 20 observed paper sessions",
            "Isolated generated-code research and evaluation",
            "Operator API and deployment security/cost validation",
        ],
    }
    (output / "request.json").write_text(
        request.model_dump_json(indent=2), encoding="utf-8"
    )
    (output / "strategy.json").write_text(
        strategy.model_dump_json(indent=2), encoding="utf-8"
    )
    (output / "risk-state.json").write_text(
        state.model_dump_json(indent=2), encoding="utf-8"
    )
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


class Arguments(argparse.Namespace):
    output: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        help="New directory; existing paths are never overwritten.",
    )
    args = parser.parse_args(namespace=Arguments())
    run_demo(Path(args.output))
    print(f"Synthetic offline workflow complete: {Path(args.output) / 'report.json'}")
    print(
        "Deployment readiness: BLOCKED. No broker, cloud, or promotion action performed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
