from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from ats.domain.execution import OrderIntent
from ats.domain.governance import evidence_digest
from ats.domain.strategy import StrategySpec
from ats.kis_readonly import ProductionAccountObservation, ReadReceipt
from ats.shadow import (
    ShadowContext,
    ShadowProposal,
    ShadowSessionReport,
    ShadowSessionSpec,
    ShadowStore,
    assess_overlay,
    assess_shadow,
    run_shadow_round,
    supervise_shadow,
)

NOW = datetime(2026, 10, 2, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def inputs() -> tuple[ProductionAccountObservation, ShadowProposal, ShadowContext]:
    receipt = ReadReceipt(
        account_id="fixture-account",
        operation="BALANCE",
        permit_digest=DIGEST,
        started_at=NOW,
        observed_at=NOW,
        response_digest=DIGEST,
        mode="SYNTHETIC",
    )
    observation = ProductionAccountObservation(
        account_id="fixture-account",
        mode="SYNTHETIC",
        started_at=NOW,
        observed_at=NOW,
        receipts=(receipt,),
        holdings=(),
        deposit_balance=Decimal(2000),
        total_equity=Decimal(10000),
        securities_value=Decimal(0),
        cash_orderable=Decimal(900),
        no_margin_buy_amount=Decimal(800),
        no_margin_buy_quantity=8,
        capacity_symbol="005930",
        capacity_price=Decimal(100),
        credit_amount=Decimal(0),
    )
    proposal = ShadowProposal(
        proposal_id=UUID(int=1),
        account_id=observation.account_id,
        candidate_digest=DIGEST,
        observation_digest=evidence_digest(observation),
        created_at=NOW,
        symbol="005930",
        side="BUY",
        quantity=5,
        limit_price=Decimal(100),
        reason="Synthetic shadow proposal",
    )
    context = ShadowContext(
        candidate_digest=DIGEST,
        account_id=observation.account_id,
        symbol="005930",
        observation_digest=evidence_digest(observation),
        observed_at=NOW,
        valid_until=NOW + timedelta(seconds=60),
        evidence_digest=DIGEST,
    )
    return observation, proposal, context


def test_unknown_facts_are_not_fabricated_and_proposal_is_not_an_order() -> None:
    observation, proposal, context = inputs()
    assessment = assess_shadow(proposal, observation, context, at=NOW)
    assert (
        assessment.outcome == "UNKNOWN" and assessment.checks["daily_loss"] == "UNKNOWN"
    )
    assert (
        not assessment.execution_authorized
        and assessment.broker_orders_sent == 0
        and assessment.actual_paper_sessions == 0
    )
    with pytest.raises(ValueError):
        OrderIntent.model_validate(proposal.model_dump())
    with pytest.raises(ValueError):
        ShadowProposal.model_validate({**proposal.model_dump(), "transmittable": True})


def verified_context(context: ShadowContext) -> ShadowContext:
    return context.model_copy(
        update={
            "eligible_symbols": ("005930",),
            "asset_class": "EQUITY",
            "leveraged_or_inverse": False,
            "classification_known_at": NOW,
            "market_window_start": NOW - timedelta(seconds=1),
            "market_window_end": NOW + timedelta(minutes=1),
            "quote_at": NOW,
            "quote_price": Decimal(100),
            "credit_disabled_verified": True,
            "reservations_verified": True,
            "reserved_cash": Decimal(0),
            "cash_capacity_already_net": True,
            "cash_flow_baseline_verified": True,
            "daily_loss_fraction": Decimal(0),
            "drawdown_fraction": Decimal(0),
            "fee_reserve_bps": 10,
        }
    )


def test_overlay_cannot_double_allocate_cash_or_change_real_holdings() -> None:
    observation, proposal, context = inputs()
    context = verified_context(context)
    before = observation.model_dump_json()
    second = proposal.model_copy(update={"proposal_id": UUID(int=2)})
    results = assess_overlay(
        (proposal, second), observation, (context, context), at=NOW
    )
    assert results[0].outcome == "CHECKS_PASSED_ONLY"
    assert results[1].checks["cash_available"] == "BLOCKED"
    assert results[1].checks["symbol_weight"] == "BLOCKED"
    assert observation.model_dump_json() == before
    assert all(not item.execution_authorized for item in results)


@pytest.mark.parametrize(
    "change",
    [
        {"halted": True},
        {"leveraged_or_inverse": True},
        {"classification_known_at": NOW + timedelta(seconds=1)},
        {"quote_at": NOW - timedelta(minutes=5)},
        {"daily_loss_fraction": Decimal("0.01")},
        {"drawdown_fraction": Decimal("0.15")},
    ],
)
def test_shadow_retains_halts_and_freshness(change: dict[str, object]) -> None:
    observation, proposal, context = inputs()
    result = assess_shadow(
        proposal,
        observation,
        verified_context(context).model_copy(update=change),
        at=NOW,
    )
    assert result.outcome == "BLOCKED" and not result.execution_authorized


def specification() -> ShadowSessionSpec:
    return ShadowSessionSpec.model_validate(
        {
            "session_id": "fixture-shadow",
            "account_id": "fixture-account",
            "candidate_digest": DIGEST,
            "read_permit_digest": DIGEST,
            "mode": "SYNTHETIC",
            "starts_at": NOW - timedelta(minutes=1),
            "expires_at": NOW + timedelta(hours=1),
            "retention": {
                "policy_id": "fixture-retention",
                "version": "1",
                "status": "APPROVED",
                "approved_by": "fixture-human",
                "approved_at": NOW - timedelta(minutes=1),
            },
            "retention_until": NOW + timedelta(days=1),
            "allow_persistence": True,
        }
    )


def test_shadow_round_is_idempotent_immutable_and_expires(tmp_path: Path) -> None:
    observation, proposal, context = inputs()
    spec = specification()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    reads: list[int] = []

    def collect() -> tuple[ProductionAccountObservation, list[dict[str, str | int]]]:
        reads.append(1)
        return observation, [
            {
                "method": "GET",
                "path": "/uapi/domestic-stock/v1/trading/inquire-balance",
                "call": 1,
            }
        ]

    report = run_shadow_round(
        store,
        spec,
        "round-001",
        collect=collect,
        propose=lambda observation: ((proposal,), (context,)),
        guard=lambda target: None,
        clock=lambda: NOW,
    )
    assert report is not None and report.assessments[0].outcome == "UNKNOWN"
    assert (
        run_shadow_round(
            ShadowStore(store.database),
            spec,
            "round-001",
            collect=collect,
            propose=lambda observation: ((proposal,), (context,)),
            guard=lambda target: None,
            clock=lambda: NOW,
        )
        is None
    )
    assert reads == [1]
    with pytest.raises(ValueError, match="immutable"):
        store.register(
            spec.model_copy(update={"candidate_digest": "sha256:" + "b" * 64}), at=NOW
        )
    assert store.reports(at=NOW)[0]["state"] == "SUCCEEDED"
    with pytest.raises(ValueError, match="binding"):
        store.reports(
            at=NOW, spec=spec.model_copy(update={"mode": "PRODUCTION_READ_ONLY"})
        )
    for changed in (
        report.model_copy(
            update={
                "assessments": (
                    report.assessments[0].model_copy(
                        update={"outcome": "CHECKS_PASSED_ONLY"}
                    ),
                )
            }
        ),
        report.model_copy(
            update={
                "transport_audit": (
                    {
                        "method": "POST",
                        "path": "/uapi/domestic-stock/v1/trading/order-cash",
                        "call": 1,
                    },
                )
            }
        ),
    ):
        with pytest.raises(ValueError):
            store.finish(
                spec,
                "round-001",
                "invalid-lease",
                changed,
                at=NOW,
                guard=lambda target: None,
            )
    token = store.claim(spec, "round-expired", at=NOW, guard=lambda target: None)
    assert token is not None
    with pytest.raises(ValueError, match="stale"):
        store.finish(
            spec,
            "round-expired",
            token,
            report,
            at=NOW + timedelta(seconds=120),
            guard=lambda target: None,
        )
    assert store.purge(at=spec.retention_until) == 2
    assert all(row["report"] is None for row in store.reports(at=spec.retention_until))


def test_shadow_lease_failure_requires_explicit_recovery(tmp_path: Path) -> None:
    spec = specification()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    token = store.claim(spec, "round-001", at=NOW, guard=lambda target: None)
    assert token is not None
    with pytest.raises(ValueError, match="lease"):
        ShadowStore(store.database).claim(
            spec, "round-002", at=NOW, guard=lambda target: None
        )
    store.fail(spec.session_id, "round-001", token)
    with pytest.raises(ValueError, match="halted"):
        store.claim(spec, "round-002", at=NOW, guard=lambda target: None)

    def denied(target: str) -> None:
        raise ValueError("operator denied")

    with pytest.raises(ValueError, match="denied"):
        store.recover(spec, at=NOW, guard=denied, authorize=lambda: None)
    store.recover(spec, at=NOW, guard=lambda target: None, authorize=lambda: None)
    assert store.claim(spec, "round-002", at=NOW, guard=lambda target: None) is not None


def blocked_worker() -> None:
    import threading

    threading.Event().wait()


def synthetic_shadow_candidate() -> StrategySpec:
    artifact = {"artifact_id": "synthetic", "version": "1.0.0", "digest": DIGEST}
    strategy = StrategySpec.model_validate(
        {
            "name": "Synthetic fixed candidate",
            "version": {
                "strategy_id": UUID(int=100),
                "version_id": UUID(int=1),
                "semantic_version": "1.0.0",
                "created_at": NOW - timedelta(days=1),
            },
            "research_hypothesis": "Synthetic testing only; not a selected operating candidate",
            "universe": {
                "membership_snapshot_id": "fixture-universe",
                "etf_allowlist_id": "fixture-etfs",
            },
            "features": [artifact],
            "signal": {
                "family": "MOMENTUM",
                "implementation": artifact,
                "parameters": [{"name": "signal.lookback_days", "value": 20}],
            },
            "portfolio": {
                "method": "EQUAL_WEIGHT",
                "max_positions": 10,
                "cash_buffer_bps": 100,
                "turnover_budget": 0.5,
            },
            "risk_policy": {
                "policy_id": "paper-risk-policy",
                "version": "1.0.0",
                "digest": DIGEST,
            },
            "mutation_policy": {
                "allowed_parameters": [
                    {
                        "name": "signal.lookback_days",
                        "minimum": 5,
                        "maximum": 60,
                        "step": 5,
                    }
                ],
                "max_parameter_changes": 1,
            },
            "dataset_snapshot": {
                "snapshot_id": "fixture-prices",
                "observed_through": NOW - timedelta(days=1),
                "digest": DIGEST,
            },
            "code_artifact": artifact,
            "container_image_digest": DIGEST,
            "provenance_hash": DIGEST,
        }
    )
    return strategy


def test_five_day_plan_has_fixed_end_missing_days_and_revocation(
    tmp_path: Path,
) -> None:
    import hashlib

    from ats.shadow_campaign import FiveDayPlan, FiveDayStore

    strategy = synthetic_shadow_candidate()
    artifact = {"artifact_id": "synthetic", "version": "1.0.0", "digest": DIGEST}
    candidate = strategy.model_dump_json().encode()
    calendar_evidence = b"synthetic calendar, not real exchange evidence"
    windows = [
        {
            "session": (NOW + timedelta(days=offset)).date(),
            "starts_at": NOW + timedelta(days=offset),
            "ends_at": NOW + timedelta(days=offset, minutes=5),
        }
        for offset in (0, 3, 4, 5, 6)
    ]
    session = specification().model_copy(
        update={
            "starts_at": windows[0]["starts_at"],
            "expires_at": windows[-1]["ends_at"],
            "retention_until": NOW + timedelta(days=10),
            "candidate_digest": strategy.content_digest(),
        }
    )
    plan = FiveDayPlan.model_validate(
        {
            "session": session,
            "approval": session.retention,
            "candidate_artifact_digest": "sha256:"
            + hashlib.sha256(candidate).hexdigest(),
            "calendar": {
                "start": windows[0]["session"],
                "end": windows[-1]["session"],
                "known_at": NOW - timedelta(days=1),
                "evidence": {
                    **artifact,
                    "digest": "sha256:" + hashlib.sha256(calendar_evidence).hexdigest(),
                },
                "sessions": [
                    {"session": item["session"], "close": item["ends_at"]}
                    for item in windows
                ],
            },
            "windows": windows,
        }
    )
    store = FiveDayStore(tmp_path / "five-days.sqlite3")
    with pytest.raises(ValueError, match="candidate"):
        store.register_plan(
            plan, candidate=b"changed", calendar_evidence=calendar_evidence, at=NOW
        )
    store.register_plan(
        plan, candidate=candidate, calendar_evidence=calendar_evidence, at=NOW
    )
    store.guard_window(plan, NOW.date(), at=NOW, operator_guard=lambda target: None)
    with pytest.raises(ValueError, match="window"):
        store.guard_window(
            plan,
            NOW.date(),
            at=NOW + timedelta(minutes=6),
            operator_guard=lambda target: None,
        )
    with pytest.raises(ValueError):
        FiveDayPlan.model_validate({**plan.model_dump(), "windows": plan.windows[:4]})
    with pytest.raises(ValueError, match="not ended"):
        store.final_report(plan, at=NOW)
    final = store.final_report(plan, at=plan.session.expires_at)
    assert final["status"] == "ENDED" and final["verdict"] == "NOT_MET"
    assert len(final["days"]) == 5 and all(
        day["state"] == "MISSING" for day in final["days"]
    )
    assert (
        store.final_report(plan, at=plan.session.expires_at + timedelta(seconds=1))
        == final
    )
    with pytest.raises(ValueError):
        store.guard_window(
            plan,
            plan.windows[-1].session,
            at=plan.session.expires_at,
            operator_guard=lambda target: None,
        )
    store.withdraw_plan(plan, authorize=lambda: None)
    assert (
        store.final_report(plan, at=plan.session.expires_at)["reason"]
        == "WITHDRAWN_OR_EXPIRED"
    )


def test_shadow_candidate_in_real_local_sandbox() -> None:
    import hashlib
    import os

    from ats.shadow_calculation import calculate_proposals

    if os.environ.get("ATS_SHADOW_CONTAINER_TEST") != "1":
        pytest.skip("explicit local container probe only")
    program = b"""import sys,json,hashlib,os,uuid
raw=sys.stdin.buffer.read(1048577)
data=json.loads(raw)
assert os.getuid()==65532
assert "ATS_TEST_BROKER_SECRET" not in os.environ
assert len(open("/proc/net/route").read().strip().splitlines())==1
proposal={"proposal_id":str(uuid.uuid4()),"account_id":data["observation"]["account_id"],"candidate_digest":data["candidate_digest"],"observation_digest":data["observation_digest"],"created_at":data["at"],"symbol":"005930","side":"HOLD","quantity":0,"limit_price":"100","reason":"Synthetic isolation probe; not a selected candidate"}
print(json.dumps({"protocol":"ats.shadow.v1","input_digest":"sha256:"+hashlib.sha256(raw).hexdigest(),"proposals":[proposal]}))
"""
    candidate = synthetic_shadow_candidate()
    candidate = candidate.model_copy(
        update={
            "code_artifact": candidate.code_artifact.model_copy(
                update={"digest": "sha256:" + hashlib.sha256(program).hexdigest()}
            ),
            "container_image_digest": "sha256:8db54b7850906291f8a3a0fbf01993ee5369316a8de34cbd88ffa7a0378922f1",
        }
    )
    observation, _, _ = inputs()
    proposals = calculate_proposals(candidate, program, observation, at=NOW)
    assert (
        len(proposals) == 1
        and not proposals[0].transmittable
        and proposals[0].quantity == 0
    )
    with pytest.raises(ValueError, match="hash"):
        calculate_proposals(candidate, b"changed", observation, at=NOW)


@pytest.mark.parametrize("with_failures", [True, False])
def test_five_day_end_to_end_preserves_failed_interrupted_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_failures: bool
) -> None:
    import hashlib

    from ats.domain.policy import SourceAllowlist
    from ats.kis_readonly import ReadOnlyAccessPermit, ReadOnlyCredentials
    from ats.readonly_runtime import ReadOnlyRuntime
    from ats.shadow_campaign import FiveDayPlan, FiveDayStore, run_observation_day
    from ats.shadow_demo import initialize_fixture

    initialize_fixture(tmp_path)
    policy = SourceAllowlist.model_validate_json(
        (tmp_path / "source-policy.json").read_bytes()
    )
    policy = policy.model_copy(
        update={
            "sources": tuple(
                entry.model_copy(
                    update={
                        "rights": entry.rights.model_copy(update={"retention_days": 30})
                    }
                )
                for entry in policy.sources
            )
        }
    )
    original = ReadOnlyAccessPermit.model_validate_json(
        (tmp_path / "read-permit.json").read_bytes()
    )
    base = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    windows = [
        {
            "session": (base + timedelta(days=offset)).date(),
            "starts_at": base + timedelta(days=offset),
            "ends_at": base + timedelta(days=offset, minutes=5),
        }
        for offset in (0, 3, 4, 5, 6)
    ]
    permit = original.model_copy(
        update={
            "starts_at": windows[0]["starts_at"],
            "expires_at": windows[-1]["ends_at"],
            "operations": ("QUOTE", "BALANCE", "CAPACITY", "OPEN_ORDERS", "HISTORY"),
            "retain_observations": True,
            "max_token_requests": 5,
            "source_policy_digest": evidence_digest(policy),
            "history_start": base.date(),
        }
    )
    program = b"synthetic pinned program"
    strategy = synthetic_shadow_candidate()
    strategy = strategy.model_copy(
        update={
            "code_artifact": strategy.code_artifact.model_copy(
                update={"digest": "sha256:" + hashlib.sha256(program).hexdigest()}
            )
        }
    )
    candidate = strategy.model_dump_json().encode()
    calendar = b"synthetic five-day calendar evidence"
    session = specification().model_copy(
        update={
            "session_id": "five-day-integration",
            "starts_at": windows[0]["starts_at"],
            "expires_at": windows[-1]["ends_at"],
            "retention_until": base + timedelta(days=10),
            "candidate_digest": strategy.content_digest(),
            "read_permit_digest": evidence_digest(permit),
        }
    )
    plan = FiveDayPlan.model_validate(
        {
            "session": session,
            "approval": session.retention,
            "candidate_artifact_digest": "sha256:"
            + hashlib.sha256(candidate).hexdigest(),
            "windows": windows,
            "calendar": {
                "start": windows[0]["session"],
                "end": windows[-1]["session"],
                "known_at": NOW,
                "evidence": {
                    "artifact_id": "synthetic-calendar",
                    "version": "1",
                    "digest": "sha256:" + hashlib.sha256(calendar).hexdigest(),
                },
                "sessions": [
                    {"session": item["session"], "close": item["ends_at"]}
                    for item in windows
                ],
            },
        }
    )
    store = FiveDayStore(tmp_path / "five-day.sqlite3")
    store.register_plan(plan, candidate=candidate, calendar_evidence=calendar, at=base)
    now = [base]
    calls: list[str] = []
    broken = [False]

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "access_token": "public-fixture-token",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                    "access_token_token_expired": (
                        now[0] + timedelta(days=1, hours=9)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
        if broken[0]:
            return httpx.Response(200, json={"rt_cd": "0", "output": {}})
        path = request.url.path
        output: dict[str, object] = {"rt_cd": "0"}
        if path.endswith("inquire-price"):
            output["output"] = {
                "stck_shrn_iscd": "005930",
                "stck_prpr": "100",
                "temp_stop_yn": "N",
                "iscd_stat_cls_code": "00",
            }
        elif path.endswith("inquire-balance"):
            output.update(
                output1=[],
                output2=[
                    {
                        "dnca_tot_amt": "1000",
                        "tot_evlu_amt": "1000",
                        "scts_evlu_amt": "0",
                    }
                ],
            )
        elif path.endswith("inquire-psbl-order"):
            output["output"] = {
                "ord_psbl_cash": "900",
                "nrcvb_buy_amt": "800",
                "nrcvb_buy_qty": "8",
            }
        elif path.endswith("inquire-psbl-rvsecncl"):
            output["output"] = []
        else:
            output["output1"] = []
        return httpx.Response(200, json=output)

    def calculate(
        candidate: StrategySpec,
        program: bytes,
        observation: ProductionAccountObservation,
        *,
        at: datetime,
    ) -> tuple[ShadowProposal, ...]:
        return (
            ShadowProposal(
                proposal_id=UUID(int=1),
                account_id=observation.account_id,
                candidate_digest=candidate.content_digest(),
                observation_digest=evidence_digest(observation),
                created_at=at,
                symbol="005930",
                side="HOLD",
                quantity=0,
                limit_price=Decimal(100),
                reason="Synthetic integrated observation",
            ),
        )

    monkeypatch.setattr("ats.shadow_campaign.calculate_proposals", calculate)
    credentials = ReadOnlyCredentials.model_validate(
        {
            "account_id": permit.account_id,
            "credential_ref": permit.credential_ref,
            "binding_id": permit.binding_id,
            "app_key": "synthetic-app",
            "app_secret": "synthetic-secret",
            "account_number": "12345678",
            "product_code": "01",
        }
    )

    def run(day_index: int) -> ShadowSessionReport | None:
        return run_observation_day(
            plan,
            day=plan.windows[day_index].session,
            candidate=candidate,
            program=program,
            calendar_evidence=calendar,
            permit=permit,
            source_policy=policy,
            store=store,
            runtime=ReadOnlyRuntime(tmp_path / "control.sqlite3"),
            credentials=lambda reference: credentials,
            operator_guard=lambda target: None,
            clock=lambda: now[0],
            symbol="005930",
            transport=httpx.MockTransport(respond),
        )

    assert run(0) is not None
    before = len(calls)
    assert run(0) is None and len(calls) == before
    if with_failures:
        now[0] = plan.windows[1].starts_at
        broken[0] = True
        with pytest.raises(ValueError):
            run(1)
        before = len(calls)
        now[0] = plan.windows[2].starts_at
        with pytest.raises(ValueError):
            run(2)
        assert len(calls) == before
        store.recover(
            plan.session, at=now[0], guard=lambda target: None, authorize=lambda: None
        )
        assert store.claim(
            plan.session,
            plan.windows[2].session.isoformat(),
            at=now[0],
            guard=lambda target: None,
        )
        now[0] = plan.windows[3].starts_at
        with pytest.raises(ValueError):
            run(3)
        store.recover(
            plan.session, at=now[0], guard=lambda target: None, authorize=lambda: None
        )
        broken[0] = False
        assert run(3) is not None
    else:
        for day_index in range(1, 5):
            now[0] = plan.windows[day_index].starts_at
            assert run(day_index) is not None
    final = store.final_report(plan, at=plan.session.expires_at)
    assert final["verdict"] == ("NOT_MET" if with_failures else "OBSERVATIONS_COMPLETE")
    assert [entry["state"] for entry in final["days"]] == (
        ["SUCCEEDED", "FAILED", "FAILED", "SUCCEEDED", "MISSING"]
        if with_failures
        else ["SUCCEEDED"] * 5
    )
    assert final["broker_orders_sent"] == 0 and not final["execution_authorized"]
    assert b"synthetic-secret" not in store.database.read_bytes()
    with pytest.raises(ValueError, match="only data stores"):
        store.backup(tmp_path / "forbidden-backup.sqlite3")
    from ats.readonly_smoke import ReadOnceRequest, run_read_once

    now[0] = base
    smoke_permit = permit.model_copy(
        update={"retain_observations": False, "max_token_requests": 1}
    )
    smoke = ReadOnceRequest(
        permit=smoke_permit,
        source_policy=policy,
        candidate_digest=strategy.content_digest(),
        candidate_artifact_digest=plan.candidate_artifact_digest,
        symbol="005930",
        session=base.date(),
    )
    memory_runtime = ReadOnlyRuntime(tmp_path / "memory-control.sqlite3")
    result = run_read_once(
        smoke,
        candidate,
        authorize=lambda: None,
        runtime=memory_runtime,
        credentials=lambda reference: credentials,
        clock=lambda: now[0],
        transport=httpx.MockTransport(respond),
    )
    assert not result["payload_persisted"] and "observation" not in result
    assert (
        b"12345678" not in memory_runtime.database.read_bytes()
        and b"synthetic-secret" not in memory_runtime.database.read_bytes()
    )


def blocked_owner_result() -> dict[str, object]:
    blocked_worker()
    return {}


def test_owner_supervisor_has_hard_deadline() -> None:
    from ats.readonly_smoke import supervise_owner_result

    with pytest.raises(ValueError, match="timed out"):
        supervise_owner_result(blocked_owner_result, timeout_seconds=0.2)


def test_supervisor_terminates_stuck_worker() -> None:
    with pytest.raises(ValueError, match="hard deadline"):
        supervise_shadow(blocked_worker, timeout_seconds=0.2)


def test_synthetic_round_uses_readonly_client_and_resumes_without_duplication(
    tmp_path: Path,
) -> None:
    from ats.shadow_demo import initialize_fixture, run_fixture_round

    spec = initialize_fixture(tmp_path)
    run_fixture_round(tmp_path)
    run_fixture_round(tmp_path)
    reports = ShadowStore(tmp_path / "shadow.sqlite3").reports(at=datetime.now(UTC))
    assert len(reports) == 1 and reports[0]["state"] == "SUCCEEDED"
    assert spec.mode == "SYNTHETIC"


def test_shadow_app_separates_fixture_auth_from_production(tmp_path: Path) -> None:
    from ats.operator import OperatorAuth, OperatorStore
    from ats.shadow_demo import (
        KEY,
        create_shadow_app,
        initialize_fixture,
        run_fixture_round,
        synthetic_shadow_app,
    )

    spec = initialize_fixture(tmp_path)
    run_fixture_round(tmp_path)
    client = cast(httpx.Client, TestClient(synthetic_shadow_app(tmp_path)))
    assert client.get("/api/status").status_code == 401
    token = client.get("/fixture/session?viewer=true").json()["token"]
    response = client.get("/api/status", headers={"Authorization": "Bearer " + token})
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    assert response.json()["role"] == "VIEWER"
    report = response.json()["workflow"]["shadow"][0]["report"]
    assert (
        report["assessments"][0]["outcome"] == "UNKNOWN"
        and not report["execution_authorized"]
    )
    assert "12345678" not in response.text and "synthetic-secret" not in response.text
    auth = OperatorAuth.model_validate(
        {
            "issuer": "fixture",
            "audience": "fixture",
            "verification_key": KEY,
            "algorithm": "HS256",
            "synthetic_only": True,
            "identities": [],
        }
    )
    operator = OperatorStore(tmp_path / "operator.sqlite3")
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    with pytest.raises(ValueError, match="non-fixture"):
        create_shadow_app(
            operator,
            store,
            spec.model_copy(update={"mode": "PRODUCTION_READ_ONLY"}),
            auth,
        )
    app = create_shadow_app(operator, store, spec, auth)
    clean = cast(httpx.Client, TestClient(app))
    assert clean.get("/fixture/session").status_code == 404
    assert clean.post("/fixture/sign", json={}).status_code == 404
    lease = store.claim(
        spec,
        "failed-round",
        at=datetime.now(UTC),
        guard=operator.require_research_enabled,
    )
    assert lease is not None
    store.fail(spec.session_id, "failed-round", lease)
    for index, action in enumerate(("HALT_RESEARCH", "RESUME_RESEARCH"), start=2):
        state = operator.snapshot()["reviews"][0]
        command = {
            "request_id": str(UUID(int=index)),
            "action": action,
            "target": spec.session_id,
            "target_digest": state["digest"],
            "expected_revision": state["revision"],
            "reason": "Synthetic authenticated recovery",
        }
        endpoint = (
            "/api/commands" if action == "HALT_RESEARCH" else "/api/shadow/recover"
        )
        assert (
            client.post(
                endpoint, json=command, headers={"Authorization": "Bearer " + token}
            ).status_code
            == 403
        )
        signed = client.post("/fixture/sign", json=command).json()["token"]
        headers = {"Authorization": "Bearer " + signed}
        assert client.post(endpoint, json=command, headers=headers).status_code == 200
        assert client.post(endpoint, json=command, headers=headers).status_code == 409
    assert (
        store.claim(
            spec,
            "after-recovery",
            at=datetime.now(UTC),
            guard=operator.require_research_enabled,
        )
        is not None
    )
