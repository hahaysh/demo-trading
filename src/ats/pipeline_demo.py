"""Synthetic collection -> retained snapshot -> full engines -> mock paper transport."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from ats.backtest.engines import EngineCase, FullEngineEvaluationAdapter
from ats.data.collection import CalendarSession, CalendarWindow
from ats.data.datasets import publish_kis_snapshot, restore_dataset
from ats.data.jobs import CollectionJob, CollectionJobStore, run_collection_job
from ats.data.kis import KisDailyRequest, KisQuoteReceipt
from ats.data.storage import StoragePermit
from ats.demo import create_synthetic_case
from ats.domain.execution import OrderIntent
from ats.domain.governance import evidence_digest
from ats.domain.policy import RiskPolicy, SourceAllowlist
from ats.domain.prices import SEOUL
from ats.domain.research import EvaluationEngine
from ats.domain.strategy import ArtifactRef, StrategySpec
from ats.domain.universe import UniverseMembershipArtifact
from ats.kis_paper import KisPaperClient, PaperCredentials, PaperExecutionService
from ats.paper import PaperOrderLedger
from ats.ports import EvaluationOutput, EvaluationRequest, run_evaluation
from ats.risk.assessor import RiskState


def run_pipeline(output: Path, image: str) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False)
    case = EngineCase.model_validate_json(
        Path("research/engines/case.json").read_bytes()
    )
    _, template, _, _ = create_synthetic_case(output / "template")
    start = datetime.combine(case.dates[0], time(0), tzinfo=SEOUL)
    period_end = datetime.combine(case.dates[-1], time(15, 30), tzinfo=SEOUL)
    freeze = datetime.combine(case.dates[-1] + timedelta(days=1), time(6), tzinfo=SEOUL)
    now = datetime.now(UTC)
    if now < freeze:
        raise ValueError("synthetic fixture is in the future")
    metadata = {
        "policy_id": "pipeline-synthetic",
        "version": "1",
        "status": "APPROVED",
        "approved_by": "synthetic-only-not-an-operator",
        "approved_at": start,
    }
    policy = SourceAllowlist.model_validate(
        {
            "metadata": metadata,
            "sources": [
                {
                    "source_id": "kis-market",
                    "category": "MARKET",
                    "enabled": True,
                    "legal_review": "APPROVED",
                    "rights": {"classification": "LICENSED", "retention_days": 36500},
                    "rate_limit_per_minute": 1,
                    "notes": "Fabricated values only; no real source approval.",
                }
            ],
        }
    )
    permit = StoragePermit.model_validate(
        {
            "metadata": metadata,
            "source_id": "kis-market",
            "source_policy_digest": evidence_digest(policy),
            "allow_persistence": True,
            "retention_days": 36500,
            "expires_at": start + timedelta(days=36500),
        }
    )
    evidence = ArtifactRef(
        artifact_id="synthetic-calendar", version="1", digest=evidence_digest(case)
    )
    store = CollectionJobStore(output / "collection.sqlite3")
    names: list[str] = []
    for index, session in enumerate(case.dates):
        close = datetime.combine(session, time(15, 30), tzinfo=SEOUL)
        observed = datetime.combine(session + timedelta(days=1), time(6), tzinfo=SEOUL)
        request = KisDailyRequest(symbol="005930", start=session, end=session)
        calendar = CalendarWindow(
            start=session,
            end=session,
            known_at=start,
            evidence=evidence,
            sessions=(CalendarSession(session=session, close=close),),
        )
        raw = json.dumps(
            {
                "rt_cd": "0",
                "output1": {"stck_shrn_iscd": "005930"},
                "output2": [
                    {
                        "stck_bsop_date": session.strftime("%Y%m%d"),
                        "stck_oprc": str(case.open[index]),
                        "stck_hgpr": str(max(case.open[index], case.close[index])),
                        "stck_lwpr": str(min(case.open[index], case.close[index])),
                        "stck_clpr": str(case.close[index]),
                        "acml_vol": str(case.volume[index]),
                        "flng_cls_code": "00",
                        "prtt_rate": "0",
                        "revl_issu_reas": "00",
                        "mod_yn": "N",
                    }
                ],
            }
        ).encode()
        receipt = KisQuoteReceipt(
            request=request,
            observed_at=observed,
            policy_digest=evidence_digest(policy),
            raw_payload=raw,
            row_count=1,
        )
        job = CollectionJob(
            job_id=f"synthetic-{session:%Y%m%d}",
            request=request,
            calendar=calendar,
            policy_digest=evidence_digest(policy),
            permit_digest=evidence_digest(permit),
            created_at=observed,
        )
        run_collection_job(
            store,
            job,
            source_policy=policy,
            permit=permit,
            fetch=lambda requested, receipt=receipt: receipt,
            clock=lambda observed=observed: observed,
        )
        names.append(job.job_id)
    universe = UniverseMembershipArtifact.model_validate(
        {
            "manifest_id": "pipeline-universe",
            "as_of": freeze,
            "members": [
                {
                    "instrument_id": "krx-005930",
                    "asset_class": "EQUITY",
                    "membership_basis": "KOSPI_200",
                    "observed_at": start,
                    "effective_from": start,
                    "evidence": evidence,
                }
            ],
        }
    )
    publish_kis_snapshot(
        store,
        tuple(names),
        snapshot_id="pipeline-snapshot",
        universe=universe,
        observed_through=freeze,
        now=now,
        source_policy=policy,
        permit=permit,
    )
    manifest, resolver = restore_dataset(
        CollectionJobStore(store.database),
        "pipeline-snapshot",
        source_policy=policy,
        permit=permit,
        now=now,
    )
    code = ArtifactRef(
        artifact_id="fixed-next-open-trend",
        version="1",
        digest="sha256:"
        + hashlib.sha256(
            Path("research/engines/qlib_run.py").read_bytes()
            + Path("research/engines/LeanRunner.cs").read_bytes()
        ).hexdigest(),
    )
    strategy = StrategySpec.model_validate(
        {
            **template.model_dump(),
            "version": {
                "strategy_id": uuid4(),
                "version_id": uuid4(),
                "semantic_version": "1.0.0",
                "created_at": start,
            },
            "universe": {
                **template.universe.model_dump(),
                "membership_snapshot_id": universe.manifest_id,
            },
            "dataset_snapshot": {
                "snapshot_id": manifest.snapshot.snapshot_id,
                "observed_through": freeze,
                "digest": manifest.snapshot.content_digest(),
            },
            "container_image_digest": image,
            "code_artifact": code,
            "signal": {**template.signal.model_dump(), "implementation": code},
            "research_hypothesis": "Fixed synthetic development protocol, cash benchmark; no performance certification.",
        }
    )
    protocol = ArtifactRef(
        artifact_id="synthetic-only-fixed-strategy-no-training",
        version="1",
        digest=evidence_digest(case),
    )
    outputs: list[EvaluationOutput] = []
    for engine, lock in (
        (EvaluationEngine.QLIB, Path("research/qlib/requirements.lock")),
        (EvaluationEngine.LEAN, Path("research/engines/packages.lock.json")),
    ):
        worker = Path(
            "research/engines/qlib_run.py"
            if engine is EvaluationEngine.QLIB
            else "research/engines/LeanRunner.cs"
        )
        engine_artifact = ArtifactRef(
            artifact_id=f"{engine.value.lower()}-full-engine",
            version="1",
            digest="sha256:" + hashlib.sha256(worker.read_bytes()).hexdigest(),
        )
        requested = datetime.now(UTC)
        evaluation = EvaluationRequest(
            experiment_id=uuid4(),
            strategy=strategy,
            snapshot=manifest.snapshot,
            engine=engine,
            engine_artifact=engine_artifact,
            dependency_lock_digest="sha256:"
            + hashlib.sha256(lock.read_bytes()).hexdigest(),
            random_seed=0,
            protocol=protocol,
            period_start=start,
            period_end=period_end,
            requested_at=requested,
        )
        adapter = FullEngineEvaluationAdapter(
            engine,
            case,
            manifest,
            resolver,
            image_digest=image,
            dependency_lock=lock,
            engine_artifact=engine_artifact,
            protocol=protocol,
            output=output / "evaluations",
        )
        result = run_evaluation(adapter, evaluation)
        (output / f"{engine.value.lower()}-evaluation.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        outputs.append(result)
    if (
        outputs[0].result is None
        or outputs[1].result is None
        or outputs[0].result.metrics != outputs[1].result.metrics
    ):
        raise ValueError("full-engine metrics do not match")
    paper_at = datetime.combine(case.next_session, time(9), tzinfo=SEOUL)
    risk = RiskPolicy.model_validate(
        {
            "metadata": metadata,
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
            "intent_id": uuid4(),
            "client_order_id": "synthetic-pipeline-order",
            "account_id": "synthetic-paper",
            "strategy_version_id": strategy.version.version_id,
            "strategy_digest": strategy.content_digest(),
            "champion_selection": code,
            "dataset_snapshot": strategy.dataset_snapshot,
            "signal": code,
            "risk_policy": {
                "policy_id": risk.metadata.policy_id,
                "version": risk.metadata.version,
                "digest": evidence_digest(risk),
            },
            "instrument_id": "krx-005930",
            "asset_class": "EQUITY",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": case.quantity,
            "limit_price": 100,
            "signal_session": case.dates[-1],
            "execution_session": case.next_session,
            "created_at": freeze,
            "expires_at": paper_at + timedelta(seconds=60),
        }
    )
    state = RiskState.model_validate(
        {
            "account_id": intent.account_id,
            "instrument_id": intent.instrument_id,
            "observed_at": paper_at,
            "valid_until": intent.expires_at,
            "equity": case.initial_cash,
            "cash": case.initial_cash,
            "gross_exposure": 0,
            "reserved_buy_notional": 0,
            "reserved_cash": 0,
            "held_quantity": 0,
            "reserved_sell_quantity": 0,
            "quote_price": 100,
            "quote_at": paper_at,
            "daily_loss_fraction": 0,
            "drawdown_fraction": 0,
            "champion_version_id": strategy.version.version_id,
            "champion_digest": strategy.content_digest(),
            "champion_selection": code,
            "universe_members": (intent.instrument_id,),
            "duplicate_client_order_id": False,
            "kill_switch_active": False,
            "opening_window_start": paper_at,
            "opening_window_end": intent.expires_at,
            "fee_reserve_bps": 10,
        }
    )
    calls: list[str] = []

    def broker(request: httpx.Request) -> httpx.Response:
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append(route)
        if route == "tokenP":
            return httpx.Response(
                200, json={"access_token": "synthetic-only", "expires_in": 3600}
            )
        if route == "inquire-balance":
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output1": [],
                    "output2": [
                        {
                            "dnca_tot_amt": "100000",
                            "tot_evlu_amt": "100000",
                            "scts_evlu_amt": "0",
                        }
                    ],
                },
            )
        if route == "inquire-psbl-order":
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "ord_psbl_cash": "100000",
                        "nrcvb_buy_amt": "100000",
                        "nrcvb_buy_qty": "1000",
                    },
                },
            )
        if route == "inquire-daily-ccld":
            return httpx.Response(200, json={"rt_cd": "0", "output1": []})
        if route == "order-cash":
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {"ODNO": "00001", "KRX_FWDG_ORD_ORGNO": "01234"},
                },
            )
        raise AssertionError("unexpected mock broker route")

    credentials = PaperCredentials.model_validate(
        {
            "app_key": "synthetic-key",
            "app_secret": "synthetic-secret",
            "account_number": "12345678",
            "product_code": "01",
            "account_id": intent.account_id,
        }
    )
    client = KisPaperClient(
        credentials, transport=httpx.MockTransport(broker), clock=lambda: paper_at
    )
    try:
        accepted = PaperExecutionService(
            client, PaperOrderLedger(output / "paper.sqlite3"), code
        ).submit(intent, risk, state)
    finally:
        client.close()
    report: dict[str, object] = {
        "mode": "SYNTHETIC_OFFLINE_ONLY",
        "certified": False,
        "real_broker_requests": 0,
        "collection_jobs": len(names),
        "snapshot_digest": manifest.snapshot.content_digest(),
        "strategy_digest": strategy.content_digest(),
        "engine_runs": 2,
        "metrics_equal": True,
        "paper_status": accepted.status.value,
        "mock_routes": calls,
        "image_digest": image,
    }
    (output / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--image", required=True, help="Pinned local sha256 image ID")
    args = parser.parse_args()
    print(json.dumps(run_pipeline(Path(args.output), args.image), indent=2))


if __name__ == "__main__":
    main()
