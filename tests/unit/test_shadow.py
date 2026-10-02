from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from pathlib import Path

import pytest

from ats.domain.execution import OrderIntent
from ats.domain.governance import evidence_digest
from ats.kis_readonly import ProductionAccountObservation, ReadReceipt
from ats.shadow import ShadowContext, ShadowProposal, ShadowSessionSpec, ShadowStore, assess_overlay, assess_shadow, run_shadow_round, supervise_shadow

NOW=datetime(2026,10,2,tzinfo=UTC)
DIGEST="sha256:"+"a"*64


def inputs() -> tuple[ProductionAccountObservation,ShadowProposal,ShadowContext]:
    receipt=ReadReceipt(account_id="fixture-account",operation="BALANCE",permit_digest=DIGEST,started_at=NOW,observed_at=NOW,response_digest=DIGEST,mode="SYNTHETIC")
    observation=ProductionAccountObservation(account_id="fixture-account",mode="SYNTHETIC",started_at=NOW,observed_at=NOW,receipts=(receipt,),holdings=(),deposit_balance=Decimal(2000),total_equity=Decimal(10000),securities_value=Decimal(0),cash_orderable=Decimal(900),no_margin_buy_amount=Decimal(800),no_margin_buy_quantity=8,capacity_symbol="005930",capacity_price=Decimal(100),credit_amount=Decimal(0))
    proposal=ShadowProposal(proposal_id=UUID(int=1),account_id=observation.account_id,candidate_digest=DIGEST,observation_digest=evidence_digest(observation),created_at=NOW,symbol="005930",side="BUY",quantity=5,limit_price=Decimal(100),reason="Synthetic shadow proposal")
    context=ShadowContext(candidate_digest=DIGEST,account_id=observation.account_id,symbol="005930",observation_digest=evidence_digest(observation),observed_at=NOW,valid_until=NOW+timedelta(seconds=60),evidence_digest=DIGEST)
    return observation,proposal,context


def test_unknown_facts_are_not_fabricated_and_proposal_is_not_an_order() -> None:
    observation,proposal,context=inputs()
    assessment=assess_shadow(proposal,observation,context,at=NOW)
    assert assessment.outcome=="UNKNOWN" and assessment.checks["daily_loss"]=="UNKNOWN"
    assert not assessment.execution_authorized and assessment.broker_orders_sent==0 and assessment.actual_paper_sessions==0
    with pytest.raises(ValueError): OrderIntent.model_validate(proposal.model_dump())
    with pytest.raises(ValueError): ShadowProposal.model_validate({**proposal.model_dump(),"transmittable":True})


def verified_context(context: ShadowContext) -> ShadowContext:
    return context.model_copy(update={"eligible_symbols":("005930",),"market_window_start":NOW-timedelta(seconds=1),"market_window_end":NOW+timedelta(minutes=1),"quote_at":NOW,"quote_price":Decimal(100),"credit_disabled_verified":True,"reservations_verified":True,"reserved_cash":Decimal(0),"cash_capacity_already_net":True,"cash_flow_baseline_verified":True,"daily_loss_fraction":Decimal(0),"drawdown_fraction":Decimal(0),"fee_reserve_bps":10})


def test_overlay_cannot_double_allocate_cash_or_change_real_holdings() -> None:
    observation,proposal,context=inputs()
    context=verified_context(context)
    before=observation.model_dump_json()
    second=proposal.model_copy(update={"proposal_id":UUID(int=2)})
    results=assess_overlay((proposal,second),observation,(context,context),at=NOW)
    assert results[0].outcome=="CHECKS_PASSED_ONLY"
    assert results[1].checks["cash_available"]=="BLOCKED"
    assert results[1].checks["symbol_weight"]=="BLOCKED"
    assert observation.model_dump_json()==before
    assert all(not item.execution_authorized for item in results)


@pytest.mark.parametrize("change",[{"halted":True},{"quote_at":NOW-timedelta(minutes=5)},{"daily_loss_fraction":Decimal("0.01")},{"drawdown_fraction":Decimal("0.15")}])
def test_shadow_retains_halts_and_freshness(change: dict[str,object]) -> None:
    observation,proposal,context=inputs()
    result=assess_shadow(proposal,observation,verified_context(context).model_copy(update=change),at=NOW)
    assert result.outcome=="BLOCKED" and not result.execution_authorized


def specification() -> ShadowSessionSpec:
    return ShadowSessionSpec.model_validate({"session_id":"fixture-shadow","account_id":"fixture-account","candidate_digest":DIGEST,"read_permit_digest":DIGEST,"mode":"SYNTHETIC","starts_at":NOW-timedelta(minutes=1),"expires_at":NOW+timedelta(hours=1),"retention":{"policy_id":"fixture-retention","version":"1","status":"APPROVED","approved_by":"fixture-human","approved_at":NOW-timedelta(minutes=1)},"retention_until":NOW+timedelta(days=1),"allow_persistence":True})


def test_shadow_round_is_idempotent_immutable_and_expires(tmp_path:Path) -> None:
    observation,proposal,context=inputs()
    spec=specification()
    store=ShadowStore(tmp_path/"shadow.sqlite3")
    reads:list[int]=[]
    def collect() -> tuple[ProductionAccountObservation,list[dict[str,str|int]]]:
        reads.append(1)
        return observation,[{"method":"GET","path":"/uapi/domestic-stock/v1/trading/inquire-balance","call":1}]
    report=run_shadow_round(store,spec,"round-001",collect=collect,propose=lambda observation:((proposal,),(context,)),guard=lambda target:None,clock=lambda:NOW)
    assert report is not None and report.assessments[0].outcome=="UNKNOWN"
    assert run_shadow_round(ShadowStore(store.database),spec,"round-001",collect=collect,propose=lambda observation:((proposal,),(context,)),guard=lambda target:None,clock=lambda:NOW) is None
    assert reads==[1]
    with pytest.raises(ValueError,match="immutable"):
        store.register(spec.model_copy(update={"candidate_digest":"sha256:"+"b"*64}),at=NOW)
    assert store.reports(at=NOW)[0]["state"]=="SUCCEEDED"
    assert store.purge(at=spec.retention_until)==1
    assert store.reports(at=spec.retention_until)[0]["report"] is None


def test_shadow_lease_failure_requires_explicit_recovery(tmp_path:Path) -> None:
    spec=specification()
    store=ShadowStore(tmp_path/"shadow.sqlite3")
    token=store.claim(spec,"round-001",at=NOW,guard=lambda target:None)
    assert token is not None
    with pytest.raises(ValueError,match="lease"):
        ShadowStore(store.database).claim(spec,"round-002",at=NOW,guard=lambda target:None)
    store.fail(spec.session_id,"round-001",token)
    with pytest.raises(ValueError,match="halted"):
        store.claim(spec,"round-002",at=NOW,guard=lambda target:None)
    def denied(target:str)->None: raise ValueError("operator denied")
    with pytest.raises(ValueError,match="denied"):
        store.recover(spec,at=NOW,guard=denied)
    store.recover(spec,at=NOW,guard=lambda target:None)
    assert store.claim(spec,"round-002",at=NOW,guard=lambda target:None) is not None


def blocked_worker() -> None:
    import threading
    threading.Event().wait()


def test_supervisor_terminates_stuck_worker() -> None:
    with pytest.raises(ValueError,match="hard deadline"):
        supervise_shadow(blocked_worker,timeout_seconds=0.2)


def test_synthetic_round_uses_readonly_client_and_resumes_without_duplication(tmp_path:Path)->None:
    from ats.shadow_demo import initialize_fixture, run_fixture_round
    spec=initialize_fixture(tmp_path)
    run_fixture_round(tmp_path)
    run_fixture_round(tmp_path)
    reports=ShadowStore(tmp_path/"shadow.sqlite3").reports(at=datetime.now(UTC))
    assert len(reports)==1 and reports[0]["state"]=="SUCCEEDED"
    assert spec.mode=="SYNTHETIC"