"""A frozen held-out validation stage; scores never enter proposal memory."""

import hashlib
import json
import sqlite3
import statistics
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from ats.backtest.engines import EngineCase
from ats.backtest.portfolio import (
    FactorComposition,
    OosEvidence,
    OosGatePolicy,
    OosProtocol,
    assess_oos_gate,
    compile_factors,
    evaluate_oos,
    purged_folds,
)
from ats.data.information_analysis import InformationAnalysis
from ats.data.storage import PayloadManifest
from ats.domain.execution import OrderIntent, RiskDecision
from ats.domain.governance import evidence_digest
from ats.domain.policy import RiskPolicy
from ats.domain.strategy import ArtifactRef, FrozenModel, Sha256Digest
from ats.risk.assessor import RiskState, assess_limit_intent
from ats.rsi import (
    ResearchCampaign,
    ResearchError,
    ResearchStore,
    ResearchTrial,
    learned_targets,
    model_inference,
    verify_research_information,
)


class FrozenValidation(FrozenModel):
    campaign: ResearchCampaign
    candidate: ResearchTrial
    market: EngineCase
    protocol: OosProtocol
    validator_digest: Sha256Digest
    gate_policy: OosGatePolicy | None = None


def validator_digest() -> str:
    root = Path(__file__).parent
    paths = (
        "research_validation.py",
        "rsi.py",
        "backtest/portfolio.py",
        "backtest/engines.py",
        "data/storage.py",
        "data/information_jobs.py",
        "data/information_analysis.py",
    )
    payload = json.dumps(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in paths
        },
        sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class ValidationStore(ResearchStore):
    def transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._connection(create=True)


class ShadowRiskEvidence(FrozenModel):
    kind: Literal["NonExecutingShadowRisk"] = "NonExecutingShadowRisk"
    intent_digest: Sha256Digest
    decision: RiskDecision
    broker_requests: Literal[0] = 0
    actual_paper_sessions: Literal[0] = 0


def shadow_risk(
    intent: OrderIntent,
    policy: RiskPolicy,
    state: RiskState,
    *,
    at: datetime,
    assessor: ArtifactRef,
) -> ShadowRiskEvidence:
    decision = assess_limit_intent(intent, policy, state, at=at, assessor=assessor)
    return ShadowRiskEvidence(intent_digest=evidence_digest(intent), decision=decision)


def validate_candidate(
    store: ValidationStore,
    request: FrozenValidation,
    *,
    qlib_lock: Path,
    lean_lock: Path,
    guard: Callable[[str], None],
    information_resolver: Callable[[PayloadManifest], InformationAnalysis]
    | None = None,
) -> OosEvidence:
    request = FrozenValidation.model_validate(request.model_dump())
    if request.validator_digest != validator_digest():
        raise ResearchError("validation code changed; do not relabel previous evidence")
    campaign, trial, case, protocol = (
        request.campaign,
        request.candidate,
        request.market,
        request.protocol,
    )
    guard(campaign.campaign_id)
    verify_research_information(campaign, information_resolver)
    if (
        "sha256:" + hashlib.sha256(qlib_lock.read_bytes()).hexdigest()
        != campaign.qlib_lock
        or "sha256:" + hashlib.sha256(lean_lock.read_bytes()).hexdigest()
        != campaign.lean_lock
    ):
        raise ResearchError("validation dependency locks changed")
    store.register(campaign)
    trials = store.trials(campaign.campaign_id)
    if (
        trial not in trials
        or trial.status not in ("REJECTED", "AWAITING_REVIEW")
        or trial.campaign_digest != evidence_digest(campaign)
        or protocol.total_trials < len(trials)
    ):
        raise ResearchError(
            "validation candidate or search count differs from research memory"
        )
    count = len(campaign.case.dates)
    if (
        protocol.minimum_train < count
        or len(case.dates) <= count
        or case.dates[count] != campaign.case.next_session
        or any(
            getattr(case, field)[:count] != getattr(campaign.case, field)
            for field in ("dates", "open", "close", "volume")
        )
        or any(
            getattr(case, field) != getattr(campaign.case, field)
            for field in (
                "quantity",
                "initial_cash",
                "buy_fee",
                "sell_fee",
                "slippage",
                "price_tick",
            )
        )
        or (case.opening_capacity is None) != (campaign.case.opening_capacity is None)
        or case.opening_capacity is not None
        and case.opening_capacity[:count] != campaign.case.opening_capacity
    ):
        raise ResearchError(
            "holdout must strictly extend the unchanged research market prefix"
        )
    folds = purged_folds(
        observations=len(case.dates),
        minimum_train=protocol.minimum_train,
        test_size=protocol.test_size,
        label_horizon=protocol.label_horizon,
        embargo=protocol.embargo,
    )
    calls = len(folds) * len(protocol.cost_multipliers) * 2
    if calls > 24:
        raise ResearchError("validation engine budget exceeded")
    digest = evidence_digest(request)
    token = str(uuid4())
    started = datetime.now(UTC)
    with store.transaction() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS research_validation (campaign TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, lease TEXT NOT NULL, lease_until TEXT NOT NULL, calls INTEGER NOT NULL, attempts INTEGER NOT NULL, result TEXT)"
        )
        previous = connection.execute(
            "SELECT * FROM research_validation WHERE campaign=?",
            (campaign.campaign_id,),
        ).fetchone()
        running = connection.execute(
            "SELECT lease,lease_until FROM campaigns WHERE id=?",
            (campaign.campaign_id,),
        ).fetchone()
        if (
            running["lease"] is not None
            and datetime.fromisoformat(running["lease_until"]) > started
        ):
            raise ResearchError("cannot freeze holdout while research is running")
        actual_trials = connection.execute(
            "SELECT COUNT(*) FROM research_trials WHERE campaign=?",
            (campaign.campaign_id,),
        ).fetchone()[0]
        if actual_trials > protocol.total_trials:
            raise ResearchError("search count changed before holdout freeze")
        if previous is not None:
            if previous["digest"] != digest:
                raise ResearchError(
                    "holdout already frozen for a different candidate/input; no adaptive reuse"
                )
            if previous["state"] == "SUCCEEDED":
                return OosEvidence.model_validate_json(previous["result"])
            if (
                previous["state"] == "RUNNING"
                and datetime.fromisoformat(previous["lease_until"]) > started
            ):
                raise ResearchError("validation is already running")
            if previous["attempts"] >= 2 or previous["calls"] + calls > 24:
                raise ResearchError("validation retry budget exhausted")
        attempts = previous["attempts"] + 1 if previous else 1
        reserved = previous["calls"] + calls if previous else calls
        connection.execute(
            "INSERT INTO research_validation VALUES (?,?,'RUNNING',?,?,?,?,NULL) ON CONFLICT(campaign) DO UPDATE SET state='RUNNING',lease=excluded.lease,lease_until=excluded.lease_until,calls=excluded.calls,attempts=excluded.attempts",
            (
                campaign.campaign_id,
                digest,
                token,
                (started + timedelta(seconds=1800)).isoformat(),
                reserved,
                attempts,
            ),
        )
    clock = time.monotonic()

    def checked() -> None:
        guard(campaign.campaign_id)
        verify_research_information(campaign, information_resolver)
        if time.monotonic() - clock >= 1800:
            raise ResearchError("validation time budget exceeded")

    try:
        checked()
        parameters = {
            item.name: item.value for item in trial.candidate.signal.parameters
        }
        composition = FactorComposition.model_validate(
            {
                "lookback": int(parameters["signal.lookback_days"]),
                "direction": int(parameters.get("signal.direction", 1)),
                "require_official": bool(parameters.get("signal.official_gate", 0)),
                "price_adjustment": "TOTAL_RETURN"
                if parameters.get("signal.total_return_adjustment", 0) == 1
                else "RAW",
            }
        )
        case = EngineCase.model_validate(
            {**case.model_dump(), "lookback": composition.lookback}
        )
        stream = compile_factors(
            case,
            composition,
            instrument=campaign.instrument_id,
            information=campaign.information,
        )
        model_receipt = None
        if campaign.signal_model != "FACTORS":
            cutoffs = [0] * len(case.dates)
            for fold in folds:
                for index in range(fold.test_start, fold.test_end):
                    cutoffs[index] = fold.train_end
            stream, model_receipt = learned_targets(
                campaign,
                stream,
                training_cutoffs=tuple(cutoffs),
                price_adjustment=composition.price_adjustment,
                model_family="ELASTIC_NET"
                if campaign.signal_model == "LINEAR_SEARCH"
                and parameters.get("signal.model_family") == 1
                else "RIDGE",
            )
        result = evaluate_oos(
            case,
            composition,
            protocol,
            selection_cutoff=campaign.parent.dataset_snapshot.observed_through,
            information=campaign.information,
            instrument=campaign.instrument_id,
            candidate_digest=trial.candidate.content_digest(),
            snapshot_digest=evidence_digest(case),
            image_digest=campaign.parent.container_image_digest,
            qlib_lock=qlib_lock,
            lean_lock=lean_lock,
            guard=checked,
            prepared_stream=stream,
            model_receipt=model_receipt,
        )
        checked()
        baseline_offset = protocol.cost_multipliers.index(1) * 2
        width = len(protocol.cost_multipliers) * 2
        returns: list[float] = []
        segments: list[list[float]] = []
        for evidence in result.fold_results[baseline_offset::width]:
            curve = (case.initial_cash, *evidence.result.equity)
            segment = [
                current / previous - 1
                for previous, current in zip(curve, curve[1:], strict=False)
            ]
            returns.extend(segment)
            segments.append(segment)
        sharpes: list[float] = []
        for item in trials:
            if not item.engine_evidence:
                continue
            curve = (campaign.case.initial_cash, *item.engine_evidence[0].result.equity)
            sample = [
                current / previous - 1
                for previous, current in zip(curve, curve[1:], strict=False)
            ]
            if len(sample) > 1 and statistics.stdev(sample) > 0:
                sharpes.append(statistics.mean(sample) / statistics.stdev(sample))
        if protocol.total_trials >= 2 and result.fold_results:
            diagnostic = model_inference(
                campaign,
                {
                    "task": "dsr",
                    "returns": returns,
                    "trial_sharpes": sharpes,
                    "total_trials": protocol.total_trials,
                    "minimum_observations": 60,
                },
            )
            if diagnostic.get("model") != "DSR/SciPy-per-period" or diagnostic.get(
                "observations"
            ) != len(returns):
                raise ResearchError(
                    "statistical diagnostic is not bound to held-out observations"
                )
            result = OosEvidence.model_validate(
                {**result.model_dump(), "dsr_diagnostic": diagnostic}
            )
            checked()
            dependence = model_inference(
                campaign,
                {
                    "task": "block_bootstrap",
                    "segments": segments,
                    "total_trials": protocol.total_trials,
                    "block_lengths": list(protocol.block_lengths),
                },
            )
            if dependence.get(
                "model"
            ) != "NumPy/circular-block-bootstrap" or dependence.get(
                "observations"
            ) != len(returns):
                raise ResearchError(
                    "dependence diagnostic is not bound to held-out observations"
                )
            result = OosEvidence.model_validate(
                {**result.model_dump(), "dependence_diagnostic": dependence}
            )
        checked()
        if request.gate_policy is not None:
            gate = assess_oos_gate(
                result, protocol, request.gate_policy, at=datetime.now(UTC)
            )
            result = OosEvidence.model_validate(
                {**result.model_dump(), "review_gate": gate.model_dump(mode="json")}
            )
        with store.transaction() as connection:
            cursor = connection.execute(
                "UPDATE research_validation SET state='SUCCEEDED',result=? WHERE campaign=? AND lease=? AND state='RUNNING'",
                (result.model_dump_json(), campaign.campaign_id, token),
            )
            if cursor.rowcount != 1:
                raise ResearchError("stale validation worker cannot publish")
        return result
    except Exception:
        with store.transaction() as connection:
            connection.execute(
                "UPDATE research_validation SET state='FAILED' WHERE campaign=? AND lease=?",
                (campaign.campaign_id, token),
            )
        raise


if __name__ == "__main__":
    import argparse

    from ats.operator import OperatorStore
    from ats.rsi_workflow import information_resolver_for

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--market", type=Path, required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument(
        "--lean-lock", type=Path, default=Path("research/engines/packages.lock.json")
    )
    args = parser.parse_args()
    campaign = ResearchCampaign.model_validate_json(
        (args.root / "campaign.json").read_bytes()
    )
    store = ValidationStore(args.root / "research.sqlite3")
    trials = store.trials(campaign.campaign_id)
    selected = next(item for item in trials if item.sequence == args.sequence)
    request = FrozenValidation(
        campaign=campaign,
        candidate=selected,
        validator_digest=validator_digest(),
        gate_policy=OosGatePolicy.model_validate(
            {"metadata": {"policy_id": "development-oos-gate", "version": "1"}}
        ),
        market=EngineCase.model_validate_json(args.market.read_bytes()),
        protocol=OosProtocol(
            minimum_train=len(campaign.case.dates),
            test_size=3,
            label_horizon=1,
            embargo=1,
            total_trials=len(trials),
        ),
    )
    operator = OperatorStore(args.root / "operator.sqlite3")
    result = validate_candidate(
        store,
        request,
        qlib_lock=Path("research/qlib/requirements.lock"),
        lean_lock=args.lean_lock,
        guard=operator.require_research_enabled,
        information_resolver=information_resolver_for(args.root),
    )
    path = args.root / "holdout.json"
    if path.exists() and OosEvidence.model_validate_json(path.read_bytes()) != result:
        raise ResearchError("holdout artifact changed")
    if not path.exists():
        with path.open("x", encoding="utf-8") as stream:
            stream.write(result.model_dump_json(indent=2))
    print(
        result.model_dump_json(
            include={
                "candidate_digest",
                "case_digest",
                "corrected_lower_bound",
                "deployment_ready",
            }
        )
    )
