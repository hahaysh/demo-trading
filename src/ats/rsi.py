"""Persistent, budgeted learned challenger search; no automatic champion mutation."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, FiniteFloat, TypeAdapter

from ats.backtest.engines import (
    EngineCase,
    EngineEvidence,
    TargetEngineCase,
    run_engine,
)
from ats.backtest.portfolio import FactorComposition, compile_factors
from ats.data.information_analysis import InformationAnalysis
from ats.data.storage import LocalPayloadStore
from ats.domain.governance import evidence_digest
from ats.domain.strategy import (
    ArtifactRef,
    FrozenModel,
    Identifier,
    Sha256Digest,
    StrategySpec,
    StrategyVersion,
    create_challenger,
)


class ResearchBudget(FrozenModel):
    max_trials: Annotated[int, Field(strict=True, ge=2, le=100)]
    max_engine_calls: Annotated[int, Field(strict=True, ge=4, le=200)]
    max_seconds: Annotated[int, Field(strict=True, ge=450, le=3600)]
    minimum_trades: Annotated[int, Field(strict=True, ge=2)] = 2
    minimum_improvement: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0001
    agent_editable: Literal[False] = False


class ResearchCampaign(FrozenModel):
    campaign_id: Identifier
    parent: StrategySpec
    case: EngineCase
    budget: ResearchBudget
    proposer_image: Sha256Digest
    proposer_code: Sha256Digest
    qlib_lock: Sha256Digest
    lean_lock: Sha256Digest
    created_at: AwareDatetime
    phase: Literal["DEVELOPMENT_ONLY"] = "DEVELOPMENT_ONLY"
    information: tuple[InformationAnalysis, ...] = ()
    instrument_id: Identifier = "krx-test"
    composition_search: bool = False
    signal_model: Literal["FACTORS", "RIDGE"] = "FACTORS"


class ResearchTrial(FrozenModel):
    campaign_digest: Sha256Digest
    sequence: int
    candidate: StrategySpec
    parameters: tuple[float, ...]
    hypothesis: str
    memory_digest: Sha256Digest
    proposer_receipt: dict[str, Any]
    created_at: AwareDatetime
    status: Literal["RUNNING", "FAILED", "REJECTED", "AWAITING_REVIEW"]
    score: float | None = None
    rejection: str | None = None
    engine_evidence: tuple[EngineEvidence, ...] = ()
    promoted: Literal[False] = False


class ResearchError(ValueError):
    pass


class LearnedSignalEvidence(FrozenModel):
    factors: ArtifactRef
    model_receipt: dict[str, Any]


class ResearchStore(LocalPayloadStore):
    def register(self, campaign: ResearchCampaign) -> None:
        campaign = ResearchCampaign.model_validate(campaign.model_dump())
        with self._connection(create=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, definition TEXT NOT NULL, champion TEXT NOT NULL, lease TEXT, lease_until TEXT, engine_calls INTEGER NOT NULL, elapsed REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS research_trials (campaign TEXT, sequence INTEGER, parameters TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(campaign,sequence), UNIQUE(campaign,parameters))"
            )
            previous = connection.execute(
                "SELECT definition FROM campaigns WHERE id=?", (campaign.campaign_id,)
            ).fetchone()
            if (
                previous is not None
                and previous["definition"] != campaign.model_dump_json()
            ):
                raise ResearchError("campaign is immutable; create a new version")
            connection.execute(
                "INSERT OR IGNORE INTO campaigns VALUES (?, ?, ?, NULL, NULL, 0, 0)",
                (
                    campaign.campaign_id,
                    campaign.model_dump_json(),
                    campaign.parent.content_digest(),
                ),
            )

    def trials(self, campaign_id: str) -> tuple[ResearchTrial, ...]:
        with self._connection() as connection:
            return tuple(
                ResearchTrial.model_validate_json(row["record"])
                for row in connection.execute(
                    "SELECT record FROM research_trials WHERE campaign=? ORDER BY sequence",
                    (campaign_id,),
                )
            )

    def claim(self, campaign: ResearchCampaign, *, at: datetime) -> UUID:
        self.register(campaign)
        token = uuid4()
        with self._connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='research_validation'"
                ).fetchone()
                and connection.execute(
                    "SELECT 1 FROM research_validation WHERE campaign=?",
                    (campaign.campaign_id,),
                ).fetchone()
            ):
                raise ResearchError(
                    "campaign is frozen for holdout; further search is forbidden"
                )
            row = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (campaign.campaign_id,)
            ).fetchone()
            if row["champion"] != campaign.parent.content_digest():
                raise ResearchError("research cannot change champion identity")
            if row["lease_until"] and datetime.fromisoformat(row["lease_until"]) > at:
                raise ResearchError("research cycle already leased")
            records = connection.execute(
                "SELECT record FROM research_trials WHERE campaign=? ORDER BY sequence",
                (campaign.campaign_id,),
            ).fetchall()
            if (
                len(records) >= campaign.budget.max_trials
                or row["engine_calls"] + 2 > campaign.budget.max_engine_calls
                or row["elapsed"] + 450 > campaign.budget.max_seconds
            ):
                raise ResearchError("research experiment/engine/time budget exhausted")
            for record in records:
                trial = ResearchTrial.model_validate_json(record["record"])
                if trial.status == "RUNNING":
                    failed = trial.model_copy(
                        update={
                            "status": "FAILED",
                            "rejection": "worker interrupted; no automatic promotion",
                        }
                    )
                    connection.execute(
                        "UPDATE research_trials SET record=? WHERE campaign=? AND sequence=?",
                        (
                            failed.model_dump_json(),
                            campaign.campaign_id,
                            trial.sequence,
                        ),
                    )
            connection.execute(
                "UPDATE campaigns SET lease=?,lease_until=?,engine_calls=engine_calls+2,elapsed=elapsed+450 WHERE id=?",
                (
                    str(token),
                    (at + timedelta(seconds=450)).isoformat(),
                    campaign.campaign_id,
                ),
            )
        return token

    def save(
        self,
        campaign: ResearchCampaign,
        token: UUID,
        trial: ResearchTrial,
        *,
        at: datetime,
        elapsed: float | None = None,
    ) -> None:
        trial = ResearchTrial.model_validate(trial.model_dump())
        if trial.campaign_digest != evidence_digest(campaign):
            raise ResearchError("trial/campaign binding mismatch")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT lease,lease_until FROM campaigns WHERE id=?",
                (campaign.campaign_id,),
            ).fetchone()
            if (
                row["lease"] != str(token)
                or not row["lease_until"]
                or at >= datetime.fromisoformat(row["lease_until"])
            ):
                raise ResearchError("stale research worker cannot write")
            previous = connection.execute(
                "SELECT record FROM research_trials WHERE campaign=? AND sequence=?",
                (campaign.campaign_id, trial.sequence),
            ).fetchone()
            if previous is not None:
                prior = ResearchTrial.model_validate_json(previous["record"])
                if (
                    prior.status != "RUNNING"
                    or prior.candidate != trial.candidate
                    or prior.parameters != trial.parameters
                ):
                    raise ResearchError(
                        "terminal or different trial cannot be overwritten"
                    )
            connection.execute(
                "INSERT INTO research_trials VALUES (?, ?, ?, ?) ON CONFLICT(campaign,sequence) DO UPDATE SET record=excluded.record",
                (
                    campaign.campaign_id,
                    trial.sequence,
                    json.dumps(trial.parameters),
                    trial.model_dump_json(),
                ),
            )
            if elapsed is not None:
                if not 0 <= elapsed <= 450:
                    raise ResearchError("invalid research elapsed time")
                connection.execute(
                    "UPDATE campaigns SET lease=NULL,lease_until=NULL,elapsed=elapsed-450+? WHERE id=?",
                    (elapsed, campaign.campaign_id),
                )


def model_inference(
    campaign: ResearchCampaign, request: dict[str, Any]
) -> dict[str, Any]:
    payload = json.dumps(request, sort_keys=True, allow_nan=False).encode()
    if len(payload) > 1024 * 1024:
        raise ResearchError("proposal input exceeds budget")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", campaign.proposer_image) is None:
        raise ResearchError("model image must be pinned")
    docker = [
        "docker",
        "--host",
        "npipe:////./pipe/dockerDesktopLinuxEngine"
        if os.name == "nt"
        else "unix:///var/run/docker.sock",
    ]
    container = "ats-proposal-" + uuid4().hex
    command = [
        *docker,
        "run",
        "--rm",
        "--pull",
        "never",
        "-i",
        "--name",
        container,
        "--network",
        "none",
        "--read-only",
        "--user",
        "65532:65532",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m",
        campaign.proposer_image,
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "HOME", "USERPROFILE", "TEMP", "TMP"}
    }
    with tempfile.TemporaryFile() as log:
        try:
            completed = subprocess.run(
                command,
                input=payload,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=75,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                [*docker, "rm", "-f", container],
                capture_output=True,
                timeout=15,
                env=environment,
                check=False,
            )
            raise ResearchError("model proposal timed out") from None
        log.seek(0)
        raw = log.read(1024 * 1024 + 1)
    if completed.returncode or len(raw) > 1024 * 1024:
        raise ResearchError("model worker failed or exceeded output budget")
    result = TypeAdapter(dict[str, Any]).validate_json(raw)
    if (
        result.get("input_sha256") != hashlib.sha256(payload).hexdigest()
        or "sha256:" + str(result.get("code_sha256")) != campaign.proposer_code
        or "sha256:" + str(result.get("lock_sha256")) != campaign.qlib_lock
        or result.get("promoted") is not False
    ):
        raise ResearchError("model output is not bound to requested memory/code/lock")
    return result


def learned_proposal(
    campaign: ResearchCampaign,
    history: list[dict[str, Any]],
    choices: list[list[float]],
) -> dict[str, Any]:
    result = model_inference(campaign, {"history": history, "choices": choices})
    if result.get("selected") not in choices or result.get("fit_rows") != len(history):
        raise ResearchError(
            "learned proposal does not match authorized candidates/history"
        )
    return result


def learned_targets(
    campaign: ResearchCampaign,
    case: TargetEngineCase,
    *,
    training_cutoffs: tuple[int, ...] | None = None,
) -> tuple[TargetEngineCase, dict[str, Any]]:
    features = [
        [0.0, 0.0]
        if index == 0
        else [
            case.close[index - 1] / case.open[index - 1] - 1,
            case.close[index - 1] / case.close[max(0, index - case.lookback)] - 1,
        ]
        for index in range(len(case.dates))
    ]
    labels = [
        close / opening - 1
        for close, opening in zip(case.close, case.open, strict=True)
    ]
    cutoffs = (
        training_cutoffs
        if training_cutoffs is not None
        else tuple(range(len(case.dates)))
    )
    if len(cutoffs) != len(case.dates) or any(
        type(cutoff) is not int or not 0 <= cutoff <= index
        for index, cutoff in enumerate(cutoffs)
    ):
        raise ResearchError("invalid causal training cutoff")
    receipt = model_inference(
        campaign,
        {
            "task": "causal_ridge",
            "features": features,
            "labels": labels,
            "training_cutoffs": cutoffs,
        },
    )
    predictions = TypeAdapter(tuple[FiniteFloat, ...]).validate_python(
        receipt.get("predictions")
    )
    expected_rows = [0 if cutoff < 5 else cutoff for cutoff in cutoffs]
    if (
        len(predictions) != len(case.dates)
        or receipt.get("training_rows") != expected_rows
        or receipt.get("model") != "Ridge/expanding-past-only"
    ):
        raise ResearchError("learned signal uses incomplete or future training rows")
    targets = tuple(
        target if prediction > 0 else 0
        for target, prediction in zip(case.targets, predictions, strict=True)
    )
    signal = ArtifactRef(
        artifact_id="ridge-signal-stream",
        version="1",
        digest=evidence_digest(
            LearnedSignalEvidence(factors=case.signal_artifact, model_receipt=receipt)
        ),
    )
    return TargetEngineCase.model_validate(
        {**case.model_dump(), "targets": targets, "signal_artifact": signal}
    ), receipt


def run_research_cycle(
    store: ResearchStore,
    campaign: ResearchCampaign,
    *,
    qlib_lock: Path,
    lean_lock: Path,
    research_guard: Callable[[str], None],
) -> ResearchTrial:
    campaign = ResearchCampaign.model_validate(campaign.model_dump())
    research_guard(campaign.campaign_id)
    if campaign.signal_model == "RIDGE" and (
        not campaign.composition_search or len(campaign.case.dates) < 7
    ):
        raise ResearchError(
            "Ridge campaigns require composition authority and at least seven sessions"
        )
    if campaign.composition_search:
        compiler_digest = (
            "sha256:"
            + hashlib.sha256(
                Path(__file__)
                .with_name("backtest")
                .joinpath("portfolio.py")
                .read_bytes()
            ).hexdigest()
        )
        if campaign.parent.code_artifact.digest != compiler_digest:
            raise ResearchError(
                "factor compiler changed; a new immutable campaign is required"
            )
        supplied = {evidence_digest(item) for item in campaign.information}
        if supplied != {artifact.digest for artifact in campaign.parent.features}:
            raise ResearchError(
                "information snapshots do not match strategy feature artifacts"
            )
    if (
        campaign.qlib_lock
        != "sha256:" + hashlib.sha256(qlib_lock.read_bytes()).hexdigest()
        or campaign.lean_lock
        != "sha256:" + hashlib.sha256(lean_lock.read_bytes()).hexdigest()
    ):
        raise ResearchError("research dependency locks changed")
    bounds = next(
        (
            bound
            for bound in campaign.parent.mutation_policy.allowed_parameters
            if bound.name == "signal.lookback_days"
        ),
        None,
    )
    if bounds is None:
        raise ResearchError("operator lookback mutation bounds required")
    values = [float(value) for value in range(2, 253) if bounds.permits(float(value))]
    token = store.claim(campaign, at=datetime.now(UTC))
    started = time.monotonic()
    trials = store.trials(campaign.campaign_id)
    history = [
        {"parameters": list(trial.parameters), "score": trial.score}
        for trial in trials
        if trial.score is not None
    ]
    candidates = [[value] for value in values]
    if campaign.composition_search:
        permitted = {
            bound.name: bound
            for bound in campaign.parent.mutation_policy.allowed_parameters
        }
        if not {"signal.direction", "signal.official_gate"}.issubset(permitted):
            raise ResearchError("operator-authorized composition bounds required")
        candidates = [
            [value, float(direction), float(gate)]
            for value in values
            for direction in (-1, 1)
            for gate in (0, 1)
            if permitted["signal.direction"].permits(direction)
            and permitted["signal.official_gate"].permits(gate)
        ]
    choices = [
        parameters
        for parameters in candidates
        if all(trial.parameters != tuple(parameters) for trial in trials)
    ]
    if not choices:
        raise ResearchError("authorized candidate space exhausted")
    proposal: dict[str, Any]
    if len(history) >= 2:
        proposal = learned_proposal(campaign, history, choices)
        parameters = tuple(float(value) for value in proposal["selected"])
    else:
        baseline_parameters = (
            (float(campaign.case.lookback), 1.0, 0.0)
            if campaign.composition_search
            else (float(campaign.case.lookback),)
        )
        parameters = (
            baseline_parameters
            if not trials
            else tuple(
                max(
                    choices,
                    key=lambda choice: sum(
                        abs(value - base)
                        for value, base in zip(choice, baseline_parameters, strict=True)
                    ),
                )
            )
        )
        proposal = {
            "model": "BOOTSTRAP_NOT_LEARNED",
            "selected": list(parameters),
            "fit_rows": 0,
        }
    selected = parameters[0]
    memory_digest = (
        "sha256:"
        + hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest()
    )
    sequence = len(trials)
    changes = {"signal.lookback_days": selected}
    if campaign.composition_search:
        if len(parameters) != 3:
            raise ResearchError(
                "composition proposal must have exactly three authorized parameters"
            )
        changes.update(
            {"signal.direction": parameters[1], "signal.official_gate": parameters[2]}
        )
    current = {
        parameter.name: parameter.value
        for parameter in campaign.parent.signal.parameters
    }
    changes = {
        name: value for name, value in changes.items() if current.get(name) != value
    }
    if not changes:
        candidate = campaign.parent
    else:
        candidate = create_challenger(
            campaign.parent,
            StrategyVersion(
                strategy_id=campaign.parent.version.strategy_id,
                version_id=uuid4(),
                parent_version_id=campaign.parent.version.version_id,
                semantic_version=f"0.2.{sequence}",
                created_at=datetime.now(UTC),
            ),
            changes,
            hypothesis=f"Feedback-guided factor composition {parameters}; development only.",
            provenance_hash=memory_digest,
        )
    trial = ResearchTrial(
        campaign_digest=evidence_digest(campaign),
        sequence=sequence,
        candidate=candidate,
        parameters=parameters,
        hypothesis=candidate.research_hypothesis,
        memory_digest=memory_digest,
        proposer_receipt=proposal,
        created_at=datetime.now(UTC),
        status="RUNNING",
    )
    store.save(campaign, token, trial, at=datetime.now(UTC))
    try:
        case = EngineCase.model_validate(
            {**campaign.case.model_dump(), "lookback": int(selected)}
        )
        if campaign.composition_search:
            if len(parameters) != 3:
                raise ResearchError("composition parameter width mismatch")
            composition = FactorComposition.model_validate(
                {
                    "lookback": int(selected),
                    "direction": int(parameters[1]),
                    "require_official": bool(parameters[2]),
                }
            )
            case = compile_factors(
                case,
                composition,
                instrument=campaign.instrument_id,
                information=campaign.information,
            )
            if campaign.signal_model == "RIDGE":
                research_guard(campaign.campaign_id)
                case, model_receipt = learned_targets(campaign, case)
                trial = ResearchTrial.model_validate(
                    {
                        **trial.model_dump(),
                        "proposer_receipt": {**proposal, "signal_model": model_receipt},
                    }
                )
        engines: tuple[tuple[Literal["QLIB", "LEAN"], Path], ...] = (
            ("QLIB", qlib_lock),
            ("LEAN", lean_lock),
        )
        evaluated: list[EngineEvidence] = []
        for engine, lock in engines:
            research_guard(campaign.campaign_id)
            evaluated.append(
                run_engine(
                    case,
                    engine=engine,
                    image_digest=campaign.parent.container_image_digest,
                    dependency_lock=lock,
                    strategy_digest=candidate.content_digest(),
                    snapshot_digest=candidate.dataset_snapshot.digest,
                )
            )
        results = tuple(evaluated)
        if (
            results[0].result.equity != results[1].result.equity
            or results[0].result.fills != results[1].result.fills
        ):
            raise ResearchError("dual-engine disagreement")
        score = results[0].result.equity[-1] / case.initial_cash - 1
        baseline = next(
            (
                trial.score
                for trial in trials
                if trial.sequence == 0 and trial.score is not None
            ),
            score,
        )
        enough = len(results[0].result.fills) >= campaign.budget.minimum_trades
        accepted = (
            sequence > 0
            and enough
            and score > baseline + campaign.budget.minimum_improvement
        )
        trial = ResearchTrial.model_validate(
            {
                **trial.model_dump(),
                "status": "AWAITING_REVIEW" if accepted else "REJECTED",
                "score": score,
                "engine_evidence": results,
                "rejection": None
                if accepted
                else "insufficient improvement/trades; champion unchanged",
            }
        )
    except Exception:
        failed = trial.model_copy(
            update={
                "status": "FAILED",
                "rejection": "evaluation failed; champion unchanged",
            }
        )
        store.save(
            campaign,
            token,
            failed,
            at=datetime.now(UTC),
            elapsed=time.monotonic() - started,
        )
        raise
    store.save(
        campaign, token, trial, at=datetime.now(UTC), elapsed=time.monotonic() - started
    )
    return trial


def main() -> None:
    from ats.demo import create_synthetic_case
    from ats.domain.strategy import ArtifactRef

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--engine-image", required=True)
    parser.add_argument("--proposer-image", required=True)
    parser.add_argument("--cycles", type=int, default=4)
    args = parser.parse_args()
    if not 2 <= args.cycles <= 4:
        raise ResearchError("synthetic parameter campaign supports two to four cycles")
    root = Path(args.output)
    qlib_lock, lean_lock = (
        Path("research/qlib/requirements.lock"),
        Path("research/engines/packages.lock.json"),
    )
    configuration = root / "campaign.json"
    if configuration.exists():
        campaign = ResearchCampaign.model_validate_json(configuration.read_bytes())
        if (
            campaign.parent.container_image_digest != args.engine_image
            or campaign.proposer_image != args.proposer_image
        ):
            raise ResearchError("resume images differ from immutable campaign")
    else:
        root.mkdir(parents=True, exist_ok=False)
        _, template, _, _ = create_synthetic_case(root / "snapshot")
        artifact = ArtifactRef(
            artifact_id="synthetic-engine-strategy",
            version="1",
            digest="sha256:"
            + hashlib.sha256(
                Path("research/engines/qlib_run.py").read_bytes()
                + Path("research/engines/LeanRunner.cs").read_bytes()
            ).hexdigest(),
        )
        parent = StrategySpec.model_validate(
            {
                **template.model_dump(),
                "container_image_digest": args.engine_image,
                "code_artifact": artifact,
                "signal": {**template.signal.model_dump(), "implementation": artifact},
            }
        )
        campaign = ResearchCampaign(
            campaign_id="synthetic-rsi",
            parent=parent,
            case=EngineCase.model_validate_json(
                Path("research/engines/case.json").read_bytes()
            ),
            budget=ResearchBudget(max_trials=4, max_engine_calls=8, max_seconds=1800),
            proposer_image=args.proposer_image,
            proposer_code="sha256:"
            + hashlib.sha256(Path("research/rsi/propose.py").read_bytes()).hexdigest(),
            qlib_lock="sha256:" + hashlib.sha256(qlib_lock.read_bytes()).hexdigest(),
            lean_lock="sha256:" + hashlib.sha256(lean_lock.read_bytes()).hexdigest(),
            created_at=datetime.now(UTC),
        )
        with configuration.open("x", encoding="utf-8") as stream:
            stream.write(campaign.model_dump_json(indent=2))
    store = ResearchStore(root / "research.sqlite3")
    store.register(campaign)

    def synthetic_guard(campaign_id: str) -> None:
        if campaign_id != "synthetic-rsi" or campaign.phase != "DEVELOPMENT_ONLY":
            raise ResearchError("CLI only permits the fixed synthetic campaign")

    while len(store.trials(campaign.campaign_id)) < args.cycles:
        trial = run_research_cycle(
            store,
            campaign,
            qlib_lock=qlib_lock,
            lean_lock=lean_lock,
            research_guard=synthetic_guard,
        )
        print(
            json.dumps(
                {
                    "cycle": trial.sequence,
                    "parameters": trial.parameters,
                    "status": trial.status,
                    "score": trial.score,
                    "model": trial.proposer_receipt["model"],
                    "memory_digest": trial.memory_digest,
                }
            )
        )
    trials = store.trials(campaign.campaign_id)
    print(
        json.dumps(
            {
                "kind": "SyntheticParameterRSI",
                "cycles": len(trials),
                "learned_cycles": sum(
                    trial.proposer_receipt.get("fit_rows", 0) >= 2 for trial in trials
                ),
                "champion_digest": campaign.parent.content_digest(),
                "promoted": False,
                "scope": "PARAMETER_STAGE_NOT_COMPLETE_RSI_ATS",
            }
        )
    )


if __name__ == "__main__":
    main()
