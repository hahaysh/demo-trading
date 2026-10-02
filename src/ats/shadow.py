"""Non-transmittable actual-account overlay diagnostics and durable sessions."""

import hashlib
import json
import multiprocessing
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from ats.data.storage import LocalPayloadStore
from ats.domain.governance import evidence_digest
from ats.domain.policy import PolicyMetadata, PolicyStatus
from ats.domain.strategy import FrozenModel, Identifier, Sha256Digest
from ats.kis_readonly import ROUTES, TOKEN_PATH, Money, ProductionAccountObservation


class ShadowProposal(FrozenModel):
    proposal_id: UUID
    account_id: Identifier
    candidate_digest: Sha256Digest
    observation_digest: Sha256Digest
    created_at: AwareDatetime
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9]{6}$")]
    side: Literal["BUY", "SELL", "HOLD"]
    quantity: Annotated[int, Field(strict=True, ge=0)]
    limit_price: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    reason: Annotated[str, Field(min_length=3, max_length=1000)]
    transmittable: Literal[False] = False
    research_only: Literal[True] = True


class ShadowContext(FrozenModel):
    candidate_digest: Sha256Digest
    account_id: Identifier
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9]{6}$")]
    observation_digest: Sha256Digest
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    evidence_digest: Sha256Digest
    owned_symbols: tuple[str, ...] = ()
    eligible_symbols: tuple[str, ...] = ()
    asset_class: Literal["EQUITY", "ETF"] | None = None
    leveraged_or_inverse: bool | None = None
    classification_known_at: AwareDatetime | None = None
    market_window_start: AwareDatetime | None = None
    market_window_end: AwareDatetime | None = None
    quote_at: AwareDatetime | None = None
    quote_price: Money | None = None
    credit_disabled_verified: bool | None = None
    reservations_verified: bool = False
    reserved_cash: Money | None = None
    reserved_buy_notional: dict[str, Money] = {}
    reserved_sell_quantity: dict[str, Annotated[int, Field(strict=True, ge=0)]] = {}
    cash_capacity_already_net: bool | None = None
    cash_flow_baseline_verified: bool = False
    daily_loss_fraction: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = (
        None
    )
    drawdown_fraction: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = (
        None
    )
    fee_reserve_bps: Annotated[int, Field(strict=True, ge=0, le=1000)] | None = None
    halted: bool = False


CheckStatus = Literal["PASS", "BLOCKED", "UNKNOWN"]


class ShadowAssessment(FrozenModel):
    proposal_digest: Sha256Digest
    observation_digest: Sha256Digest
    context_digest: Sha256Digest
    observed_at: AwareDatetime
    checks: dict[str, CheckStatus]
    outcome: Literal["CHECKS_PASSED_ONLY", "BLOCKED", "UNKNOWN"]
    execution_authorized: Literal[False] = False
    broker_orders_sent: Literal[0] = 0
    actual_paper_sessions: Literal[0] = 0


def assess_shadow(
    proposal: ShadowProposal,
    observation: ProductionAccountObservation,
    context: ShadowContext,
    *,
    at: datetime,
    allocated_cash: Decimal = Decimal(0),
    allocated_symbols: dict[str, Decimal] | None = None,
    allocated_sells: dict[str, int] | None = None,
) -> ShadowAssessment:
    proposal = ShadowProposal.model_validate(proposal.model_dump())
    observation = ProductionAccountObservation.model_validate(observation.model_dump())
    context = ShadowContext.model_validate(context.model_dump())
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("aware shadow assessment time required")
    if (
        proposal.account_id != observation.account_id
        or context.account_id != observation.account_id
        or proposal.candidate_digest != context.candidate_digest
        or proposal.observation_digest != evidence_digest(observation)
        or context.observation_digest != proposal.observation_digest
        or context.symbol != proposal.symbol
    ):
        raise ValueError("shadow candidate/account/observation binding mismatch")
    if allocated_cash < 0 or not allocated_cash.is_finite():
        raise ValueError("invalid overlay reservation")

    def result(condition: bool) -> CheckStatus:
        return "PASS" if condition else "BLOCKED"

    checks: dict[str, CheckStatus] = {
        "account_freshness": result(
            observation.started_at <= observation.observed_at <= at
            and (at - observation.observed_at).total_seconds() < 60
            and (observation.observed_at - observation.started_at).total_seconds() <= 30
        ),
        "context_freshness": result(
            context.observed_at <= at < context.valid_until
            and proposal.created_at <= at
        ),
        "stop": result(not context.halted),
        "eligibility": result(
            proposal.symbol in context.eligible_symbols
            and context.leveraged_or_inverse is False
            and context.classification_known_at <= proposal.created_at
        )
        if context.eligible_symbols
        and context.asset_class is not None
        and context.leveraged_or_inverse is not None
        and context.classification_known_at is not None
        else "UNKNOWN",
        "market_window": result(
            context.market_window_start <= at < context.market_window_end
        )
        if context.market_window_start is not None
        and context.market_window_end is not None
        else "UNKNOWN",
        "quote_freshness": result(0 <= (at - context.quote_at).total_seconds() < 60)
        if context.quote_at
        else "UNKNOWN",
        "cash_only": result(
            context.credit_disabled_verified and observation.credit_amount == 0
        )
        if observation.credit_amount is not None
        and context.credit_disabled_verified is not None
        else "UNKNOWN",
        "daily_loss": result(context.daily_loss_fraction < Decimal("0.01"))
        if context.cash_flow_baseline_verified
        and context.daily_loss_fraction is not None
        else "UNKNOWN",
        "drawdown": result(context.drawdown_fraction < Decimal("0.15"))
        if context.cash_flow_baseline_verified and context.drawdown_fraction is not None
        else "UNKNOWN",
        "reservations": "PASS" if context.reservations_verified else "UNKNOWN",
        "non_executing_candidate": "PASS",
    }
    if observation.credit_amount is not None and observation.credit_amount > 0:
        checks["cash_only"] = "BLOCKED"
    notional = proposal.limit_price * proposal.quantity
    fee = (
        notional * context.fee_reserve_bps / 10000
        if context.fee_reserve_bps is not None
        else None
    )
    holding = next(
        (item for item in observation.holdings if item.symbol == proposal.symbol), None
    )
    if proposal.side == "BUY":
        bound = (
            observation.capacity_symbol == proposal.symbol
            and observation.capacity_price == proposal.limit_price
        )
        cash_values = (observation.cash_orderable, observation.no_margin_buy_amount)
        if (
            bound
            and all(value is not None for value in cash_values)
            and context.reservations_verified
            and context.cash_capacity_already_net is not None
            and context.reserved_cash is not None
            and fee is not None
        ):
            assert (
                observation.cash_orderable is not None
                and observation.no_margin_buy_amount is not None
            )
            available = min(
                observation.cash_orderable, observation.no_margin_buy_amount
            )
            if not context.cash_capacity_already_net:
                available -= context.reserved_cash
            checks["cash_available"] = result(
                notional + fee <= available - allocated_cash
            )
        else:
            checks["cash_available"] = "UNKNOWN"
        checks["capacity_quantity"] = (
            result(proposal.quantity <= observation.no_margin_buy_quantity)
            if bound and observation.no_margin_buy_quantity is not None
            else "UNKNOWN"
        )
    elif proposal.side == "SELL":
        checks["holding_ownership"] = result(proposal.symbol in context.owned_symbols)
        checks["sellable_quantity"] = (
            result(
                holding is not None
                and proposal.quantity
                <= min(holding.quantity, holding.sellable_quantity)
                - context.reserved_sell_quantity.get(proposal.symbol, 0)
                - (allocated_sells or {}).get(proposal.symbol, 0)
            )
            if context.reservations_verified
            else "UNKNOWN"
        )
    checks["quantity"] = result(
        proposal.quantity == 0 if proposal.side == "HOLD" else proposal.quantity > 0
    )
    if (
        context.reservations_verified
        and context.quote_price is not None
        and fee is not None
    ):
        value = holding.quantity * context.quote_price if holding else Decimal(0)
        value += (
            notional
            if proposal.side == "BUY"
            else -proposal.quantity * context.quote_price
            if proposal.side == "SELL"
            else 0
        )
        value += context.reserved_buy_notional.get(proposal.symbol, Decimal(0)) + (
            allocated_symbols or {}
        ).get(proposal.symbol, Decimal(0))
        equity = observation.total_equity - fee
        checks["symbol_weight"] = result(
            equity > 0 and 0 <= value <= equity * Decimal("0.1")
        )
        gross = (
            observation.securities_value
            + sum(context.reserved_buy_notional.values())
            + sum((allocated_symbols or {}).values())
            + (
                notional
                if proposal.side == "BUY"
                else -proposal.quantity * context.quote_price
                if proposal.side == "SELL"
                else 0
            )
        )
        checks["gross_exposure"] = result(equity > 0 and 0 <= gross <= equity)
    else:
        checks["symbol_weight"] = checks["gross_exposure"] = "UNKNOWN"
    outcome = (
        "BLOCKED"
        if "BLOCKED" in checks.values()
        else "UNKNOWN"
        if "UNKNOWN" in checks.values()
        else "CHECKS_PASSED_ONLY"
    )
    return ShadowAssessment(
        proposal_digest=evidence_digest(proposal),
        observation_digest=evidence_digest(observation),
        context_digest=evidence_digest(context),
        observed_at=at,
        checks=checks,
        outcome=outcome,
    )


def assess_overlay(
    proposals: tuple[ShadowProposal, ...],
    observation: ProductionAccountObservation,
    contexts: tuple[ShadowContext, ...],
    *,
    at: datetime,
) -> tuple[ShadowAssessment, ...]:
    if (
        not 1 <= len(proposals) <= 100
        or len(proposals) != len(contexts)
        or len({item.proposal_id for item in proposals}) != len(proposals)
    ):
        raise ValueError("bounded unique overlay proposals and contexts required")
    if (
        len({item.candidate_digest for item in proposals}) != 1
        or len({context.account_id for context in contexts}) != 1
    ):
        raise ValueError("overlay must use one account and pinned candidate")
    allocated_cash = Decimal(0)
    allocated_symbols: dict[str, Decimal] = {}
    allocated_sells: dict[str, int] = {}
    results: list[ShadowAssessment] = []
    for proposal, context in zip(proposals, contexts, strict=True):
        assessment = assess_shadow(
            proposal,
            observation,
            context,
            at=at,
            allocated_cash=allocated_cash,
            allocated_symbols=allocated_symbols,
            allocated_sells=allocated_sells,
        )
        results.append(assessment)
        if assessment.outcome != "CHECKS_PASSED_ONLY":
            continue
        if proposal.side == "BUY":
            notional = proposal.quantity * proposal.limit_price
            allocated_cash += notional * (
                1 + Decimal(context.fee_reserve_bps or 0) / 10000
            )
            allocated_symbols[proposal.symbol] = (
                allocated_symbols.get(proposal.symbol, Decimal(0)) + notional
            )
        elif proposal.side == "SELL":
            allocated_sells[proposal.symbol] = (
                allocated_sells.get(proposal.symbol, 0) + proposal.quantity
            )
    return tuple(results)


class ShadowSessionSpec(FrozenModel):
    session_id: Identifier
    account_id: Identifier
    candidate_digest: Sha256Digest
    read_permit_digest: Sha256Digest
    mode: Literal["SYNTHETIC", "PRODUCTION_READ_ONLY"]
    starts_at: AwareDatetime
    expires_at: AwareDatetime
    retention: PolicyMetadata
    retention_until: AwareDatetime
    allow_persistence: bool = False
    overlay: Literal["ACTUAL_ACCOUNT_OVERLAY"] = "ACTUAL_ACCOUNT_OVERLAY"


class ShadowSessionReport(FrozenModel):
    session_digest: Sha256Digest
    observation: ProductionAccountObservation
    proposals: tuple[ShadowProposal, ...]
    contexts: tuple[ShadowContext, ...]
    assessments: tuple[ShadowAssessment, ...]
    created_at: AwareDatetime
    transport_audit_digest: Sha256Digest
    transport_audit: tuple[dict[str, str | int], ...]
    broker_orders_sent: Literal[0] = 0
    actual_paper_sessions: Literal[0] = 0
    execution_authorized: Literal[False] = False


class ShadowStore(LocalPayloadStore):
    def validate_binding(self, spec: ShadowSessionSpec) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT digest FROM shadow_sessions WHERE id=?", (spec.session_id,)
            ).fetchone()
        if row is None or row["digest"] != evidence_digest(spec):
            raise ValueError("shadow session binding mismatch")

    def register(self, spec: ShadowSessionSpec, *, at: datetime) -> None:
        spec = ShadowSessionSpec.model_validate(spec.model_dump())
        self.check_session(spec, at)
        with self._connection(create=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS shadow_sessions (id TEXT PRIMARY KEY,digest TEXT NOT NULL,record TEXT NOT NULL,halted INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS shadow_runs (session TEXT NOT NULL,round_id TEXT NOT NULL,state TEXT NOT NULL,lease TEXT,lease_until TEXT,report TEXT,expires_at TEXT NOT NULL,PRIMARY KEY(session,round_id))"
            )
            previous = connection.execute(
                "SELECT digest FROM shadow_sessions WHERE id=?", (spec.session_id,)
            ).fetchone()
            if previous is not None and previous["digest"] != evidence_digest(spec):
                raise ValueError("shadow session is immutable")
            connection.execute(
                "INSERT OR IGNORE INTO shadow_sessions VALUES (?,?,?,0)",
                (spec.session_id, evidence_digest(spec), spec.model_dump_json()),
            )

    @staticmethod
    def check_session(spec: ShadowSessionSpec, at: datetime) -> None:
        if (
            at.tzinfo is None
            or at.utcoffset() is None
            or not spec.starts_at <= at < spec.expires_at
            or not spec.allow_persistence
            or spec.retention.status is not PolicyStatus.APPROVED
            or spec.retention.approved_at is None
            or spec.retention.approved_at > at
            or at >= spec.retention_until
        ):
            raise ValueError("shadow observation or retention permit rejected")

    def claim(
        self,
        spec: ShadowSessionSpec,
        round_id: str,
        *,
        at: datetime,
        guard: Callable[[str], None],
    ) -> str | None:
        guard(spec.session_id)
        self.register(spec, at=at)
        token = str(uuid4())
        with self._connection() as connection:
            if connection.execute(
                "SELECT halted FROM shadow_sessions WHERE id=?", (spec.session_id,)
            ).fetchone()[0]:
                raise ValueError(
                    "shadow session halted; authenticated recovery required"
                )
            existing = connection.execute(
                "SELECT state FROM shadow_runs WHERE session=? AND round_id=?",
                (spec.session_id, round_id),
            ).fetchone()
            if existing is not None:
                if existing["state"] == "SUCCEEDED":
                    return None
                raise ValueError("shadow round already exists; no automatic replay")
            if connection.execute(
                "SELECT 1 FROM shadow_runs WHERE session=? AND state='RUNNING'",
                (spec.session_id,),
            ).fetchone():
                raise ValueError("shadow session has an active or unrecovered lease")
            until = min(spec.expires_at, at + timedelta(seconds=120))
            connection.execute(
                "INSERT INTO shadow_runs VALUES (?,?,'RUNNING',?,?,NULL,?)",
                (
                    spec.session_id,
                    round_id,
                    token,
                    until.astimezone(UTC).isoformat(),
                    spec.retention_until.astimezone(UTC).isoformat(),
                ),
            )
        return token

    def finish(
        self,
        spec: ShadowSessionSpec,
        round_id: str,
        token: str,
        report: ShadowSessionReport,
        *,
        at: datetime,
        guard: Callable[[str], None],
    ) -> None:
        guard(spec.session_id)
        self.check_session(spec, at)
        report = ShadowSessionReport.model_validate(report.model_dump())
        if (
            report.session_digest != evidence_digest(spec)
            or report.observation.account_id != spec.account_id
            or report.observation.mode != spec.mode
            or report.created_at > at
            or not spec.starts_at
            <= report.observation.started_at
            <= report.observation.observed_at
            <= report.created_at
            or len(report.proposals) != len(report.assessments)
            or not report.proposals
        ):
            raise ValueError("shadow report session binding mismatch")
        for proposal, assessment in zip(
            report.proposals, report.assessments, strict=True
        ):
            if (
                proposal.candidate_digest != spec.candidate_digest
                or proposal.account_id != spec.account_id
                or assessment.proposal_digest != evidence_digest(proposal)
                or assessment.observation_digest != evidence_digest(report.observation)
            ):
                raise ValueError("shadow report candidate/evidence mismatch")
        if any(
            receipt.permit_digest != spec.read_permit_digest
            or receipt.account_id != spec.account_id
            or receipt.mode != spec.mode
            or not report.observation.started_at
            <= receipt.started_at
            <= receipt.observed_at
            <= report.observation.observed_at
            for receipt in report.observation.receipts
        ):
            raise ValueError("shadow report read permit mismatch")
        if not report.observation.receipts:
            raise ValueError("shadow observation receipts required")
        if (
            assess_overlay(
                report.proposals,
                report.observation,
                report.contexts,
                at=report.created_at,
            )
            != report.assessments
        ):
            raise ValueError("shadow assessment differs from independent recomputation")
        audit_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(report.transport_audit, sort_keys=True).encode()
            ).hexdigest()
        )
        allowed = {
            ("POST", TOKEN_PATH),
            *(("GET", route[0]) for route in ROUTES.values()),
        }
        if audit_digest != report.transport_audit_digest or any(
            (item.get("method"), item.get("path")) not in allowed
            or set(item) != {"method", "path", "call"}
            for item in report.transport_audit
        ):
            raise ValueError(
                "shadow transport audit contains forbidden or changed operations"
            )
        if [item["call"] for item in report.transport_audit] != list(
            range(1, len(report.transport_audit) + 1)
        ):
            raise ValueError("shadow transport audit sequence is incomplete")
        for operation in {receipt.operation for receipt in report.observation.receipts}:
            if sum(
                item["method"] == "GET" and item["path"] == ROUTES[operation][0]
                for item in report.transport_audit
            ) < sum(
                receipt.operation == operation
                for receipt in report.observation.receipts
            ):
                raise ValueError("shadow read receipts lack transport evidence")
        with self._connection() as connection:
            registered = connection.execute(
                "SELECT digest,halted FROM shadow_sessions WHERE id=?",
                (spec.session_id,),
            ).fetchone()
            if registered is None or registered["digest"] != evidence_digest(spec):
                raise ValueError("shadow session binding mismatch")
            if registered["halted"]:
                raise ValueError("shadow session halted")
            row = connection.execute(
                "SELECT lease,lease_until,state FROM shadow_runs WHERE session=? AND round_id=?",
                (spec.session_id, round_id),
            ).fetchone()
            if (
                row is None
                or row["state"] != "RUNNING"
                or row["lease"] != token
                or at >= datetime.fromisoformat(row["lease_until"])
            ):
                raise ValueError("stale shadow worker cannot publish")
            connection.execute(
                "UPDATE shadow_runs SET state='SUCCEEDED',report=?,lease=NULL WHERE session=? AND round_id=?",
                (report.model_dump_json(), spec.session_id, round_id),
            )

    def fail(self, session_id: str, round_id: str, token: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE shadow_runs SET state='FAILED',lease=NULL WHERE session=? AND round_id=? AND lease=? AND state='RUNNING'",
                (session_id, round_id, token),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE shadow_sessions SET halted=1 WHERE id=?", (session_id,)
                )

    def recover(
        self,
        spec: ShadowSessionSpec,
        *,
        at: datetime,
        guard: Callable[[str], None],
        authorize: Callable[[], None],
    ) -> None:
        self.check_session(spec, at)
        self.validate_binding(spec)
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM shadow_runs WHERE session=? AND state='RUNNING' AND lease_until>?",
                (spec.session_id, at.astimezone(UTC).isoformat()),
            ).fetchone():
                raise ValueError("active shadow worker cannot be recovered")
            authorize()
            guard(spec.session_id)
            connection.execute(
                "UPDATE shadow_runs SET state='FAILED',lease=NULL WHERE session=? AND state='RUNNING'",
                (spec.session_id,),
            )
            connection.execute(
                "UPDATE shadow_sessions SET halted=0 WHERE id=? AND digest=?",
                (spec.session_id, evidence_digest(spec)),
            )

    def reports(
        self, *, at: datetime, spec: ShadowSessionSpec | None = None
    ) -> tuple[dict[str, object], ...]:
        if spec is not None:
            self.validate_binding(spec)
        with self._connection(create=True) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='shadow_runs'"
            ).fetchone():
                return ()
            rows = connection.execute(
                "SELECT session,round_id,state,report,expires_at FROM shadow_runs ORDER BY session,round_id"
            ).fetchall()
        return tuple(
            {
                "session": row["session"],
                "round_id": row["round_id"],
                "retention_until": row["expires_at"],
                "state": "EXPIRED"
                if at >= datetime.fromisoformat(row["expires_at"])
                else row["state"],
                "report": ShadowSessionReport.model_validate_json(
                    row["report"]
                ).model_dump(mode="json")
                if row["report"] and at < datetime.fromisoformat(row["expires_at"])
                else None,
            }
            for row in rows
            if spec is None or row["session"] == spec.session_id
        )

    def purge(self, *, at: datetime) -> int:
        with self._connection() as connection:
            return connection.execute(
                "UPDATE shadow_runs SET report=NULL,state='EXPIRED',lease=NULL WHERE expires_at<=? AND state!='EXPIRED'",
                (at.astimezone(UTC).isoformat(),),
            ).rowcount


def run_shadow_round(
    store: ShadowStore,
    spec: ShadowSessionSpec,
    round_id: str,
    *,
    collect: Callable[
        [], tuple[ProductionAccountObservation, list[dict[str, str | int]]]
    ],
    propose: Callable[
        [ProductionAccountObservation],
        tuple[tuple[ShadowProposal, ...], tuple[ShadowContext, ...]],
    ],
    guard: Callable[[str], None],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ShadowSessionReport | None:
    token = store.claim(spec, round_id, at=clock(), guard=guard)
    if token is None:
        return None
    started = time.monotonic()
    try:
        guard(spec.session_id)
        observation, audit = collect()
        guard(spec.session_id)
        store.check_session(spec, clock())
        proposals, contexts = propose(observation)
        assessed_at = clock()
        assessments = assess_overlay(proposals, observation, contexts, at=assessed_at)
        if time.monotonic() - started >= 120:
            raise ValueError("shadow round deadline exceeded")
        report = ShadowSessionReport(
            session_digest=evidence_digest(spec),
            observation=observation,
            proposals=proposals,
            contexts=contexts,
            assessments=assessments,
            created_at=assessed_at,
            transport_audit=tuple(audit),
            transport_audit_digest="sha256:"
            + hashlib.sha256(json.dumps(audit, sort_keys=True).encode()).hexdigest(),
        )
        store.finish(spec, round_id, token, report, at=clock(), guard=guard)
        return report
    except Exception:
        store.fail(spec.session_id, round_id, token)
        raise ValueError(
            "shadow round failed; inspect protected diagnostics and obtain recovery approval"
        ) from None


def _supervised_entry(worker: Callable[[], None]) -> None:
    try:
        worker()
    except Exception:
        raise SystemExit(1) from None


def supervise_shadow(
    worker: Callable[[], None], *, timeout_seconds: float = 120
) -> None:
    if not 0 < timeout_seconds <= 120:
        raise ValueError("bounded supervisor deadline required")
    process = multiprocessing.get_context("spawn").Process(
        target=_supervised_entry, args=(worker,)
    )
    process.start()
    try:
        process.join(timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
            raise ValueError(
                "shadow supervisor hard deadline exceeded; recovery required"
            )
        if process.exitcode != 0:
            raise ValueError("shadow worker failed; recovery required")
    finally:
        process.close()
