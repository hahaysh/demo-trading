"""Resumable synthetic information -> learned factors -> dual engines -> review."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import SecretStr

from ats.backtest.engines import EngineCase
from ats.data.artifacts import LocalArtifactResolver
from ats.data.information import InformationClient, InformationSource, collect_dart
from ats.data.information_analysis import (
    AnalysisPolicy,
    InformationAnalysis,
    analyze_information,
)
from ats.data.information_jobs import (
    InformationSchedule,
    InformationStore,
    run_information_schedule,
)
from ats.data.prices import normalize_daily_price
from ats.data.storage import StoragePermit
from ats.demo import create_synthetic_case
from ats.domain.data import DataSnapshot, PointInTimeRecord, UniverseMembershipManifest
from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.domain.prices import SEOUL, DailyPrice
from ats.domain.strategy import ArtifactRef, StrategySpec
from ats.domain.universe import UniverseMembershipArtifact
from ats.operator import OperatorCommand, OperatorIdentity, OperatorStore
from ats.rsi import ResearchBudget, ResearchCampaign, ResearchStore, run_research_cycle


def run_workflow(
    root: Path,
    *,
    engine_image: str,
    proposer_image: str,
    cycles: int = 4,
    signal_model: Literal["FACTORS", "RIDGE"] = "FACTORS",
    case_file: Path = Path("research/engines/case.json"),
) -> dict[str, object]:
    if not 2 <= cycles <= 6:
        raise ValueError("bounded synthetic workflow requires two to six cycles")
    root.mkdir(parents=True, exist_ok=True)
    configuration = root / "campaign.json"
    operator = OperatorStore(root / "operator.sqlite3")
    qlib_lock, lean_lock = (
        Path("research/qlib/requirements.lock"),
        Path("research/engines/packages.lock.json"),
    )
    if configuration.exists():
        campaign = ResearchCampaign.model_validate_json(configuration.read_bytes())
        if (
            campaign.parent.container_image_digest != engine_image
            or campaign.proposer_image != proposer_image
            or campaign.signal_model != signal_model
            or campaign.case != EngineCase.model_validate_json(case_file.read_bytes())
        ):
            raise ValueError("resume runtime/model/input mismatch")
    else:
        case = EngineCase.model_validate_json(case_file.read_bytes())
        _, template, _, _ = create_synthetic_case(root / "template")
        start = datetime.combine(case.dates[0], time(8), tzinfo=SEOUL)
        metadata = {
            "policy_id": "synthetic-information",
            "version": "1",
            "status": "APPROVED",
            "approved_by": "fixture-only",
            "approved_at": start - timedelta(days=2),
        }
        policy = SourceAllowlist.model_validate(
            {
                "metadata": metadata,
                "sources": [
                    {
                        "source_id": "dart",
                        "category": "DISCLOSURE",
                        "enabled": True,
                        "legal_review": "APPROVED",
                        "rights": {
                            "classification": "APPROVED_PUBLIC",
                            "retention_days": 36500,
                        },
                        "rate_limit_per_minute": 1,
                        "notes": "Synthetic fixture; not real source authority",
                    }
                ],
            }
        )
        permit = StoragePermit.model_validate(
            {
                "metadata": metadata,
                "source_id": "dart",
                "source_policy_digest": evidence_digest(policy),
                "allow_persistence": True,
                "retention_days": 36500,
                "expires_at": start + timedelta(days=36500),
            }
        )
        source = InformationSource.model_validate(
            {
                "source_id": "dart",
                "format": "DART",
                "category": "DISCLOSURE",
                "endpoint": "https://opendart.fss.or.kr/api/list.json",
                "allowed_scope": ["all-filings"],
                "allow_collection": True,
                "terms": template.code_artifact,
            }
        )
        schedule = InformationSchedule(
            schedule_id="synthetic-dart-daily",
            source=source,
            policy_digest=evidence_digest(policy),
            permit_digest=evidence_digest(permit),
            starts_at=start - timedelta(days=1),
        )
        analysis_policy = AnalysisPolicy.model_validate(
            {
                "artifact": template.code_artifact,
                "sources": [
                    {
                        "source_id": "dart",
                        "publisher_group": "synthetic-primary",
                        "tier": "PRIMARY",
                    }
                ],
                "aliases": [
                    {
                        "instrument_id": "krx-test",
                        "symbol": "005930",
                        "company_id": "12345678",
                        "aliases": ["합성기업"],
                        "known_at": start - timedelta(days=1),
                        "effective_from": start - timedelta(days=1),
                    }
                ],
            }
        )
        store = InformationStore(root / "information.sqlite3")
        analyses: list[InformationAnalysis] = []
        for index, day in enumerate(case.dates):
            at = datetime.combine(day, time(8), tzinfo=SEOUL)
            body = {
                "status": "000",
                "page_no": 1,
                "page_count": 100,
                "total_count": 1,
                "total_page": 1,
                "list": [
                    {
                        "rcept_no": f"{day:%Y%m%d}000001",
                        "rcept_dt": f"{day - timedelta(days=1):%Y%m%d}",
                        "report_nm": "합성기업 계약 사실무근"
                        if index == 3
                        else "합성기업 계약 공시",
                        "corp_code": "12345678",
                        "stock_code": "005930",
                        "corp_name": "합성기업",
                    }
                ],
            }

            def respond(
                request: httpx.Request, body: dict[str, Any] = body
            ) -> httpx.Response:
                response = json.loads(json.dumps(body))
                received = request.url.params["end_de"]
                response["list"][0]["rcept_dt"] = received
                response["list"][0]["rcept_no"] = received + "000001"
                return httpx.Response(200, json=response)

            client = InformationClient(
                source,
                policy,
                permit,
                store,
                transport=httpx.MockTransport(respond),
                clock=lambda at=at: at,
            )
            try:
                while (
                    run_information_schedule(
                        store,
                        schedule,
                        client,
                        lambda lower, upper, client=client: collect_dart(
                            client,
                            start=lower.astimezone(SEOUL).date(),
                            end=upper.astimezone(SEOUL).date(),
                            key=SecretStr("synthetic-not-a-key"),
                        ),
                    )
                    is not None
                ):
                    pass
            finally:
                client.close()
            observations = store.observations(
                at=at, now=at, policy=policy, permit=permit
            )
            analyses.append(analyze_information(observations, analysis_policy, at=at))
        code = ArtifactRef(
            artifact_id="synthetic-factor-implementation",
            version="1",
            digest="sha256:"
            + hashlib.sha256(
                Path("src/ats/backtest/portfolio.py").read_bytes()
            ).hexdigest(),
        )

        def artifact_bytes(payload: bytes) -> str:
            digest = hashlib.sha256(payload).hexdigest()
            directory = root / "market" / "sha256"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / digest
            if path.exists() and path.read_bytes() != payload:
                raise ValueError("synthetic market artifact changed")
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(payload)
            return "sha256:" + digest

        freeze = datetime.combine(
            case.dates[-1] + timedelta(days=1), time(6), tzinfo=SEOUL
        )
        universe = UniverseMembershipArtifact.model_validate(
            {
                "manifest_id": "synthetic-workflow-universe",
                "as_of": freeze,
                "members": [
                    {
                        "instrument_id": "krx-test",
                        "asset_class": "EQUITY",
                        "membership_basis": "KOSPI_200",
                        "observed_at": start,
                        "effective_from": start,
                        "evidence": code,
                    }
                ],
            }
        )
        records: list[PointInTimeRecord] = []
        for index, session in enumerate(case.dates):
            price = DailyPrice.model_validate(
                {
                    "instrument_id": "krx-test",
                    "session": session,
                    "session_close": datetime.combine(
                        session, time(15, 30), tzinfo=SEOUL
                    ),
                    "open": str(case.open[index]),
                    "high": str(max(case.open[index], case.close[index])),
                    "low": str(min(case.open[index], case.close[index])),
                    "close": str(case.close[index]),
                    "volume": case.volume[index],
                }
            )
            records.append(
                PointInTimeRecord.model_validate(
                    {
                        "source_id": "synthetic-market",
                        "source_item_id": f"session-{session:%Y%m%d}",
                        "revision": "v1-price",
                        "observed_at": datetime.combine(
                            session + timedelta(days=1), time(6), tzinfo=SEOUL
                        ),
                        "effective_at": price.session_close,
                        "instrument_id": price.instrument_id,
                        "rights_class": "APPROVED_PUBLIC",
                        "credibility_tier": "PRIMARY",
                        "content_hash": price.content_digest(),
                        "raw_payload_digest": artifact_bytes(
                            price.model_dump_json().encode()
                        ),
                    }
                )
            )
        snapshot = DataSnapshot(
            snapshot_id="synthetic-workflow-market",
            observed_through=freeze,
            created_at=freeze,
            universe_membership=UniverseMembershipManifest(
                manifest_id=universe.manifest_id,
                as_of=freeze,
                digest=artifact_bytes(universe.model_dump_json().encode()),
            ),
            records=tuple(records),
        )
        (root / "market-snapshot.json").write_text(
            snapshot.model_dump_json(indent=2), encoding="utf-8"
        )
        parent = StrategySpec.model_validate(
            {
                **template.model_dump(),
                "container_image_digest": engine_image,
                "dataset_snapshot": {
                    "snapshot_id": snapshot.snapshot_id,
                    "observed_through": freeze,
                    "digest": snapshot.content_digest(),
                },
                "universe": {
                    **template.universe.model_dump(),
                    "membership_snapshot_id": universe.manifest_id,
                },
                "version": {
                    "strategy_id": uuid4(),
                    "version_id": uuid4(),
                    "semantic_version": "1.0.0",
                    "created_at": start,
                },
                "code_artifact": code,
                "model": ArtifactRef(
                    artifact_id="ridge-expanding-past-only",
                    version="1",
                    digest="sha256:"
                    + hashlib.sha256(
                        Path("research/rsi/propose.py").read_bytes()
                    ).hexdigest(),
                )
                if signal_model == "RIDGE"
                else None,
                "features": [
                    ArtifactRef(
                        artifact_id=f"information-{index}",
                        version="1",
                        digest=evidence_digest(item),
                    )
                    for index, item in enumerate(analyses)
                ],
                "signal": {
                    "family": "TREND",
                    "implementation": code,
                    "parameters": [
                        {"name": "signal.lookback_days", "value": 2},
                        {"name": "signal.direction", "value": 1},
                        {"name": "signal.official_gate", "value": 0},
                    ],
                },
                "mutation_policy": {
                    "allowed_parameters": [
                        {
                            "name": "signal.lookback_days",
                            "minimum": 2,
                            "maximum": 5,
                            "step": 1,
                        },
                        {
                            "name": "signal.direction",
                            "minimum": -1,
                            "maximum": 1,
                            "step": 2,
                        },
                        {
                            "name": "signal.official_gate",
                            "minimum": 0,
                            "maximum": 1,
                            "step": 1,
                        },
                    ]
                },
            }
        )
        campaign = ResearchCampaign(
            campaign_id="synthetic-factor-rsi",
            parent=parent,
            case=case,
            budget=ResearchBudget(max_trials=6, max_engine_calls=12, max_seconds=2700),
            proposer_image=proposer_image,
            proposer_code="sha256:"
            + hashlib.sha256(Path("research/rsi/propose.py").read_bytes()).hexdigest(),
            qlib_lock="sha256:" + hashlib.sha256(qlib_lock.read_bytes()).hexdigest(),
            lean_lock="sha256:" + hashlib.sha256(lean_lock.read_bytes()).hexdigest(),
            created_at=datetime.now(UTC),
            composition_search=True,
            signal_model=signal_model,
            information=tuple(analyses),
        )
        with configuration.open("x", encoding="utf-8") as stream:
            stream.write(campaign.model_dump_json(indent=2))
        operator.register_review(campaign.campaign_id, evidence_digest(campaign))
        actor = OperatorIdentity(subject="synthetic-human", role="OPERATOR", human=True)
        for revision, action in enumerate(("HALT_RESEARCH", "RESUME_RESEARCH")):
            command = OperatorCommand.model_validate(
                {
                    "request_id": uuid4(),
                    "action": action,
                    "target": campaign.campaign_id,
                    "target_digest": evidence_digest(campaign),
                    "expected_revision": revision,
                    "reason": "Synthetic test authorization; not operational approval",
                }
            )
            operator.command(
                command,
                actor,
                {
                    "jti": str(uuid4()),
                    "action": action,
                    "target_digest": command.target_digest,
                    "command_digest": evidence_digest(command),
                },
            )
    snapshot = DataSnapshot.model_validate_json(
        (root / "market-snapshot.json").read_bytes()
    )
    if snapshot.content_digest() != campaign.parent.dataset_snapshot.digest:
        raise ValueError("research market snapshot changed")
    resolver = LocalArtifactResolver(root / "market")
    visible = resolver.select_verified_records_as_of(
        snapshot, at=snapshot.observed_through
    )
    prices = sorted(
        (normalize_daily_price(resolver, record) for record in visible),
        key=lambda price: price.session,
    )
    if (
        tuple(price.session for price in prices) != campaign.case.dates
        or tuple(float(price.open) for price in prices) != campaign.case.open
        or tuple(float(price.close) for price in prices) != campaign.case.close
    ):
        raise ValueError("research case differs from retained market observations")
    research = ResearchStore(root / "research.sqlite3")
    research.register(campaign)
    while len(research.trials(campaign.campaign_id)) < cycles:
        trial = run_research_cycle(
            research,
            campaign,
            qlib_lock=qlib_lock,
            lean_lock=lean_lock,
            research_guard=operator.require_research_enabled,
        )
        if trial.status == "AWAITING_REVIEW":
            operator.register_review(
                "candidate-" + trial.candidate.version.version_id.hex,
                trial.candidate.content_digest(),
            )
    trials = research.trials(campaign.campaign_id)
    result: dict[str, object] = {
        "mode": "SYNTHETIC_INFORMATION_RSI",
        "cycles": len(trials),
        "collection_runs": len(InformationStore(root / "information.sqlite3").status()),
        "learned_cycles": sum(
            item.proposer_receipt.get("fit_rows", 0) >= 2 for item in trials
        ),
        "parameters": [item.parameters for item in trials],
        "statuses": [item.status for item in trials],
        "real_broker_requests": 0,
        "promoted": False,
        "complete_ats": False,
    }
    (root / "report.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--engine-image", required=True)
    parser.add_argument("--proposer-image", required=True)
    parser.add_argument("--cycles", type=int, default=4)
    parser.add_argument(
        "--signal-model", choices=["FACTORS", "RIDGE"], default="FACTORS"
    )
    parser.add_argument("--case", type=Path, default=Path("research/engines/case.json"))
    args = parser.parse_args()
    print(
        json.dumps(
            run_workflow(
                Path(args.root),
                engine_image=args.engine_image,
                proposer_image=args.proposer_image,
                cycles=args.cycles,
                signal_model=args.signal_model,
                case_file=args.case,
            ),
            indent=2,
        )
    )
