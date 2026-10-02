"""Fixed five-session observation schedule; never dispatches broker orders."""

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Self

import httpx
from pydantic import AwareDatetime, Field, model_validator

from ats.data.collection import CalendarWindow
from ats.domain.governance import evidence_digest
from ats.domain.policy import PolicyMetadata, PolicyStatus, SourceAllowlist
from ats.domain.prices import SEOUL
from ats.domain.strategy import FrozenModel, Sha256Digest, StrategySpec
from ats.kis_readonly import (
    ProductionAccountObservation,
    ReadOnlyAccessPermit,
    ReadOnlyCredentials,
    ReadOnlyKisClient,
)
from ats.readonly_runtime import ReadOnlyRuntime
from ats.shadow import (
    ShadowContext,
    ShadowProposal,
    ShadowSessionReport,
    ShadowSessionSpec,
    ShadowStore,
    run_shadow_round,
)
from ats.shadow_calculation import calculate_proposals


class ObservationWindow(FrozenModel):
    session: date
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        if not self.starts_at < self.ends_at or any(
            value.astimezone(SEOUL).date() != self.session
            for value in (self.starts_at, self.ends_at)
        ):
            raise ValueError("observation window must stay within its Korean session")
        return self


class FiveDayPlan(FrozenModel):
    session: ShadowSessionSpec
    approval: PolicyMetadata
    candidate_artifact_digest: Sha256Digest
    calendar: CalendarWindow
    windows: tuple[ObservationWindow, ...] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def fixed_schedule(self) -> Self:
        days = [window.session for window in self.windows]
        if days != sorted(set(days)) or days != [
            item.session for item in self.calendar.sessions
        ]:
            raise ValueError(
                "exactly five explicit verified calendar sessions required"
            )
        if self.calendar.known_at > self.windows[0].starts_at:
            raise ValueError("calendar was unavailable before observation")
        if (
            self.session.starts_at != self.windows[0].starts_at
            or self.session.expires_at != self.windows[-1].ends_at
        ):
            raise ValueError("session authority must end at the fifth window")
        if self.session.retention_until <= self.session.expires_at:
            raise ValueError("final report retention must extend beyond observation")
        return self

    def verify_artifacts(self, candidate: bytes, calendar_evidence: bytes) -> None:
        if (
            "sha256:" + hashlib.sha256(candidate).hexdigest()
            != self.candidate_artifact_digest
        ):
            raise ValueError("candidate artifact changed")
        strategy = StrategySpec.model_validate_json(candidate)
        if strategy.content_digest() != self.session.candidate_digest:
            raise ValueError("candidate semantic digest mismatch")
        if (
            "sha256:" + hashlib.sha256(calendar_evidence).hexdigest()
            != self.calendar.evidence.digest
        ):
            raise ValueError("calendar source evidence changed")


class FiveDayStore(ShadowStore):
    def sweep_retention(self, plan: FiveDayPlan, *, at: datetime) -> None:
        if at >= plan.session.retention_until:
            self.purge(at=at)
            with self._connection() as connection:
                connection.execute(
                    "UPDATE shadow_plans SET final=NULL WHERE session=?",
                    (plan.session.session_id,),
                )

    def register_plan(
        self,
        plan: FiveDayPlan,
        *,
        candidate: bytes,
        calendar_evidence: bytes,
        at: datetime,
    ) -> None:
        plan = FiveDayPlan.model_validate(plan.model_dump())
        if (
            at.tzinfo is None
            or plan.approval.status is not PolicyStatus.APPROVED
            or plan.approval.approved_at is None
            or not plan.approval.approved_at <= at <= plan.session.starts_at
        ):
            raise ValueError("plan must be approved and registered before observation")
        plan.verify_artifacts(candidate, calendar_evidence)
        self.register(plan.session, at=plan.session.starts_at)
        with self._connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS shadow_plans (session TEXT PRIMARY KEY,digest TEXT NOT NULL,record TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,final TEXT)"
            )
            row = connection.execute(
                "SELECT digest FROM shadow_plans WHERE session=?",
                (plan.session.session_id,),
            ).fetchone()
            if row is not None and row["digest"] != evidence_digest(plan):
                raise ValueError("five-day plan is immutable")
            connection.execute(
                "INSERT OR IGNORE INTO shadow_plans(session,digest,record) VALUES (?,?,?)",
                (
                    plan.session.session_id,
                    evidence_digest(plan),
                    plan.model_dump_json(),
                ),
            )

    def guard_window(
        self,
        plan: FiveDayPlan,
        day: date,
        *,
        at: datetime,
        operator_guard: Callable[[str], None],
    ) -> None:
        operator_guard(plan.session.session_id)
        self.check_session(plan.session, at)
        self.validate_binding(plan.session)
        window = next((item for item in plan.windows if item.session == day), None)
        if window is None or not window.starts_at <= at < window.ends_at:
            raise ValueError("outside fixed observation window; no extension")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT digest,revoked,final FROM shadow_plans WHERE session=?",
                (plan.session.session_id,),
            ).fetchone()
            if (
                row is None
                or row["digest"] != evidence_digest(plan)
                or row["revoked"]
                or row["final"]
            ):
                raise ValueError("observation plan revoked, ended or changed")
            if connection.execute(
                "SELECT halted FROM shadow_sessions WHERE id=?",
                (plan.session.session_id,),
            ).fetchone()[0]:
                raise ValueError("observation halted")

    def halt_plan(self, plan: FiveDayPlan, *, authorize: Callable[[], None]) -> None:
        authorize()
        self.validate_binding(plan.session)
        with self._connection() as connection:
            connection.execute(
                "UPDATE shadow_sessions SET halted=1 WHERE id=?",
                (plan.session.session_id,),
            )

    def withdraw_plan(
        self, plan: FiveDayPlan, *, authorize: Callable[[], None]
    ) -> None:
        authorize()
        self.validate_binding(plan.session)
        with self._connection() as connection:
            connection.execute(
                "UPDATE shadow_plans SET revoked=1,final=NULL WHERE session=? AND digest=?",
                (plan.session.session_id, evidence_digest(plan)),
            )
            connection.execute(
                "UPDATE shadow_sessions SET halted=1 WHERE id=?",
                (plan.session.session_id,),
            )
            connection.execute(
                "UPDATE shadow_runs SET report=NULL,state='WITHDRAWN',lease=NULL WHERE session=?",
                (plan.session.session_id,),
            )

    def final_report(self, plan: FiveDayPlan, *, at: datetime) -> dict[str, Any]:
        if at.tzinfo is None or at < plan.session.expires_at:
            raise ValueError("observation period has not ended")
        self.validate_binding(plan.session)
        self.sweep_retention(plan, at=at)
        records = {
            row["round_id"]: row for row in self.reports(at=at, spec=plan.session)
        }
        with self._connection() as connection:
            row = connection.execute(
                "SELECT digest,revoked,final FROM shadow_plans WHERE session=?",
                (plan.session.session_id,),
            ).fetchone()
            if row is None or row["digest"] != evidence_digest(plan):
                raise ValueError("plan binding mismatch")
            if row["revoked"] or at >= plan.session.retention_until:
                connection.execute(
                    "UPDATE shadow_plans SET final=NULL WHERE session=?",
                    (plan.session.session_id,),
                )
                return {
                    "status": "ENDED",
                    "verdict": "NOT_MET",
                    "reason": "WITHDRAWN_OR_EXPIRED",
                    "broker_orders_sent": 0,
                    "execution_authorized": False,
                    "actual_paper_sessions": 0,
                }
            if row["final"]:
                import json

                return json.loads(row["final"])
            days: list[dict[str, Any]] = []
            for window in plan.windows:
                record = records.get(window.session.isoformat())
                state = record["state"] if record else "MISSING"
                if state == "RUNNING":
                    state = "FAILED_INTERRUPTED"
                days.append(
                    {
                        "session": window.session.isoformat(),
                        "window": window.model_dump(mode="json"),
                        "state": state,
                        "evidence": record,
                    }
                )
            report = {
                "plan_digest": evidence_digest(plan),
                "candidate_digest": plan.session.candidate_digest,
                "account_id": plan.session.account_id,
                "mode": plan.session.mode,
                "status": "ENDED",
                "verdict": "OBSERVATIONS_COMPLETE"
                if all(day["state"] == "SUCCEEDED" for day in days)
                else "NOT_MET",
                "days": days,
                "created_at": at.astimezone(UTC).isoformat(),
                "broker_orders_sent": 0,
                "execution_authorized": False,
                "actual_paper_sessions": 0,
                "profitability_certified": False,
            }
            import json

            connection.execute(
                "UPDATE shadow_plans SET final=? WHERE session=?",
                (json.dumps(report, sort_keys=True), plan.session.session_id),
            )
            connection.execute(
                "UPDATE shadow_sessions SET halted=1 WHERE id=?",
                (plan.session.session_id,),
            )
            return report


def run_observation_day(
    plan: FiveDayPlan,
    *,
    day: date,
    candidate: bytes,
    program: bytes,
    calendar_evidence: bytes,
    permit: ReadOnlyAccessPermit,
    source_policy: SourceAllowlist,
    store: FiveDayStore,
    runtime: ReadOnlyRuntime,
    credentials: Callable[[str], ReadOnlyCredentials],
    operator_guard: Callable[[str], None],
    clock: Callable[[], datetime],
    symbol: str,
    transport: httpx.MockTransport | None = None,
) -> ShadowSessionReport | None:
    plan = FiveDayPlan.model_validate(plan.model_dump())
    plan.verify_artifacts(candidate, calendar_evidence)
    strategy = StrategySpec.model_validate_json(candidate)
    if "sha256:" + hashlib.sha256(program).hexdigest() != strategy.code_artifact.digest:
        raise ValueError("candidate code changed before collection")
    if (
        evidence_digest(permit) != plan.session.read_permit_digest
        or permit.account_id != plan.session.account_id
        or permit.synthetic_only != (plan.session.mode == "SYNTHETIC")
        or not permit.retain_observations
    ):
        raise ValueError("observation authority differs from fixed plan")

    def guard(target: str) -> None:
        if target != plan.session.session_id:
            raise ValueError("wrong observation session")
        source = next(
            (
                entry
                for entry in source_policy.sources
                if entry.source_id == permit.source_id
            ),
            None,
        )
        if (
            source is None
            or source.rights.retention_days is None
            or plan.session.retention_until
            > clock() + timedelta(days=source.rights.retention_days)
        ):
            raise ValueError("observation retention exceeds source rights")
        store.guard_window(plan, day, at=clock(), operator_guard=operator_guard)

    def collect() -> tuple[ProductionAccountObservation, list[dict[str, str | int]]]:
        with runtime.collector(permit, clock=clock) as owner:

            def read_guard() -> None:
                guard(plan.session.session_id)
                runtime.check_owner(permit, owner)

            client = ReadOnlyKisClient(
                lambda: permit,
                lambda: source_policy,
                credentials,
                account_id=permit.account_id,
                transport=transport,
                allow_network=transport is None,
                clock=clock,
                guard=read_guard,
                before_request=lambda active, token: runtime.before_request(
                    active, owner, token=token, at=clock()
                ),
            )
            try:
                observation = client.observation_bundle(symbol, day)
                return observation, list(client.audit)
            finally:
                client.close()

    def propose(
        observation: ProductionAccountObservation,
    ) -> tuple[tuple[ShadowProposal, ...], tuple[ShadowContext, ...]]:
        guard(plan.session.session_id)
        at = clock()
        proposals = calculate_proposals(strategy, program, observation, at=at)
        window = next(item for item in plan.windows if item.session == day)
        contexts = tuple(
            ShadowContext(
                candidate_digest=strategy.content_digest(),
                account_id=observation.account_id,
                symbol=proposal.symbol,
                observation_digest=evidence_digest(observation),
                observed_at=at,
                valid_until=min(
                    window.ends_at, observation.observed_at + timedelta(seconds=60)
                ),
                evidence_digest=evidence_digest(plan),
                market_window_start=window.starts_at,
                market_window_end=window.ends_at,
                quote_at=observation.quote.exchange_traded_at
                if observation.quote
                else None,
                quote_price=observation.quote.price if observation.quote else None,
                halted=observation.quote.halted is True if observation.quote else True,
            )
            for proposal in proposals
        )
        return proposals, contexts

    return run_shadow_round(
        store,
        plan.session,
        day.isoformat(),
        collect=collect,
        propose=propose,
        guard=guard,
        clock=clock,
    )
