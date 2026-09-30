"""Bounded offline parameter experiments; never change a champion pointer."""

from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from ats.backtest.native import (
    NativeBacktestReport,
    SimulationAssumptions,
    run_native_backtest,
)
from ats.data.artifacts import LocalArtifactResolver
from ats.data.replay import InputReplayRequest
from ats.domain.governance import evidence_digest
from ats.domain.strategy import (
    FrozenModel,
    StrategySpec,
    StrategyVersion,
    create_challenger,
)


class CandidateExperiment(FrozenModel):
    strategy: StrategySpec
    report: NativeBacktestReport


def compare_candidates(
    resolver: LocalArtifactResolver,
    request: InputReplayRequest,
    parent: StrategySpec,
    *,
    instrument_id: str,
    session_opens: tuple[datetime, ...],
    assumptions: SimulationAssumptions,
    lookbacks: tuple[int, ...],
    max_trials: int,
) -> tuple[CandidateExperiment, ...]:
    if type(max_trials) is not int or not 1 <= max_trials <= 20:
        raise ValueError("explicit experiment budget must be between 1 and 20")
    if (
        not lookbacks
        or len(lookbacks) > max_trials
        or len(set(lookbacks)) != len(lookbacks)
    ):
        raise ValueError(
            "candidate lookbacks must be nonempty, unique and within budget"
        )
    candidates: list[StrategySpec] = []
    for index, lookback in enumerate(lookbacks, start=1):
        if type(lookback) is not int:
            raise ValueError("candidate lookback must be an integer")
        version = StrategyVersion(
            strategy_id=parent.version.strategy_id,
            version_id=uuid5(
                NAMESPACE_URL, f"{parent.content_digest()}:lookback:{lookback}"
            ),
            parent_version_id=parent.version.version_id,
            semantic_version=f"0.1.{index}",
            created_at=request.snapshot.created_at,
        )
        candidates.append(
            create_challenger(
                parent,
                version,
                {"signal.lookback_days": lookback},
                hypothesis=f"Offline lookback {lookback} sensitivity experiment; not promotion evidence.",
                provenance_hash=evidence_digest(parent),
            )
        )
    return tuple(
        CandidateExperiment(
            strategy=candidate,
            report=run_native_backtest(
                resolver,
                request,
                candidate,
                instrument_id=instrument_id,
                session_opens=session_opens,
                assumptions=assumptions,
            ),
        )
        for candidate in candidates
    )
