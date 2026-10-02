import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from ats.domain.execution import OrderIntent, RiskCheck, RiskDecision
from ats.domain.governance import evidence_digest
from ats.domain.policy import RiskPolicy
from ats.domain.strategy import ArtifactRef
from ats.kis_paper import (
    KisPaperClient,
    PaperCredentials,
    PaperExecutionService,
    PaperRecovery,
    PaperTransportError,
)
from ats.paper import PaperBrokerUpdate, PaperLedgerError, PaperOrderLedger
from ats.research_validation import shadow_risk
from ats.risk.assessor import RiskState, assess_limit_intent

CREATED = datetime(2026, 9, 30, 7, tzinfo=UTC)
CHECKED = CREATED + timedelta(minutes=1)
EXPIRY = CREATED + timedelta(minutes=5)
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
ARTIFACT = ArtifactRef(artifact_id="test-evidence", version="1.0.0", digest=DIGEST)


def test_paper_ledger_claim_is_durable_and_never_automatically_retried(
    tmp_path: Path,
) -> None:
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    prepared = ledger.prepare(
        intent, policy, state, at=state.observed_at, assessor=ARTIFACT
    )
    assert (
        ledger.prepare(intent, policy, state, at=state.observed_at, assessor=ARTIFACT)
        == prepared
    )
    claimed = ledger.claim_submission(
        intent.account_id,
        intent.client_order_id,
        policy,
        state,
        at=state.observed_at,
        assessor=ARTIFACT,
    )
    assert claimed.status.value == "SUBMITTING"
    restarted = PaperOrderLedger(ledger.database)
    with pytest.raises(PaperLedgerError, match="already claimed"):
        restarted.claim_submission(
            intent.account_id,
            intent.client_order_id,
            policy,
            state,
            at=state.observed_at,
            assessor=ARTIFACT,
        )
    restarted.mark_unknown(
        intent.account_id, intent.client_order_id, at=state.observed_at
    )
    assert (
        restarted.get(intent.account_id, intent.client_order_id).status.value
        == "UNKNOWN"
    )
    other = OrderIntent.model_validate(
        {**intent.model_dump(), "intent_id": UUID(int=9), "client_order_id": "other"}
    )
    with pytest.raises(PaperLedgerError, match="halted"):
        restarted.prepare(other, policy, state, at=state.observed_at, assessor=ARTIFACT)


def test_paper_ledger_reserves_pending_exposure_atomically(tmp_path: Path) -> None:
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    policy, state = _policy(), _risk_state()
    first = _intent(policy)
    ledger.prepare(first, policy, state, at=state.observed_at, assessor=ARTIFACT)
    second = OrderIntent.model_validate(
        {**first.model_dump(), "intent_id": UUID(int=9), "client_order_id": "other"}
    )
    with pytest.raises(ValueError, match="denies"):
        PaperOrderLedger(ledger.database).prepare(
            second, policy, state, at=state.observed_at, assessor=ARTIFACT
        )


def test_concurrent_paper_reservations_do_not_both_pass(tmp_path: Path) -> None:
    database = tmp_path / "paper.sqlite3"
    policy, state = _policy(), _risk_state()

    def prepare(number: int) -> bool:
        intent = OrderIntent.model_validate(
            {
                **_intent(policy).model_dump(),
                "intent_id": UUID(int=number),
                "client_order_id": f"concurrent-{number}",
            }
        )
        try:
            PaperOrderLedger(database).prepare(
                intent, policy, state, at=state.observed_at, assessor=ARTIFACT
            )
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(prepare, (1, 2)))
    assert sorted(outcomes) == [False, True]


def test_unfinished_submission_blocks_new_work_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "paper.sqlite3"
    ledger = PaperOrderLedger(database)
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    ledger.prepare(intent, policy, state, at=state.observed_at, assessor=ARTIFACT)
    ledger.claim_submission(
        intent.account_id,
        intent.client_order_id,
        policy,
        state,
        at=state.observed_at,
        assessor=ARTIFACT,
    )
    other = OrderIntent.model_validate(
        {
            **intent.model_dump(),
            "intent_id": UUID(int=9),
            "client_order_id": "new-after-crash",
        }
    )
    with pytest.raises(PaperLedgerError, match="pending submission"):
        PaperOrderLedger(database).prepare(
            other, policy, state, at=state.observed_at, assessor=ARTIFACT
        )


def test_paper_ledger_partial_fill_and_cancel_preserve_freshness_gate(
    tmp_path: Path,
) -> None:
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    ledger.prepare(intent, policy, state, at=state.observed_at, assessor=ARTIFACT)
    ledger.claim_submission(
        intent.account_id,
        intent.client_order_id,
        policy,
        state,
        at=state.observed_at,
        assessor=ARTIFACT,
    )
    partial = PaperBrokerUpdate(
        account_id=intent.account_id,
        client_order_id=intent.client_order_id,
        status="PARTIALLY_FILLED",
        filled_quantity=3,
        broker_order_id="fixture-broker-id",
        observed_at=state.observed_at,
        evidence=ARTIFACT,
    )
    record = ledger.reconcile(partial)
    assert record.remaining_quantity == 7
    assert ledger.reconcile(partial) == record
    canceled = partial.model_copy(
        update={
            "status": "CANCELED",
            "evidence": ARTIFACT.model_copy(update={"digest": OTHER_DIGEST}),
        }
    )
    assert ledger.reconcile(canceled).remaining_quantity == 0
    other = OrderIntent.model_validate(
        {**intent.model_dump(), "intent_id": UUID(int=9), "client_order_id": "other"}
    )
    with pytest.raises(PaperLedgerError, match="fresh reconciled"):
        ledger.prepare(other, policy, state, at=state.observed_at, assessor=ARTIFACT)


def test_paper_ledger_rejects_conflicting_ids_and_unclaimed_reconciliation(
    tmp_path: Path,
) -> None:
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    ledger.prepare(intent, policy, state, at=state.observed_at, assessor=ARTIFACT)
    altered = OrderIntent.model_validate({**intent.model_dump(), "quantity": 1})
    with pytest.raises(PaperLedgerError, match="conflicts"):
        ledger.prepare(altered, policy, state, at=state.observed_at, assessor=ARTIFACT)
    with pytest.raises(PaperLedgerError, match="invalid"):
        ledger.reconcile(
            PaperBrokerUpdate(
                account_id=intent.account_id,
                client_order_id=intent.client_order_id,
                status="FILLED",
                filled_quantity=intent.quantity,
                broker_order_id="fixture-broker-id",
                observed_at=state.observed_at,
                evidence=ARTIFACT,
            )
        )


@pytest.mark.parametrize(
    "submitted,unknown", [(False, False), (True, False), (True, True)]
)
def test_only_unsubmitted_expired_orders_release_reservations(
    tmp_path: Path, submitted: bool, unknown: bool
) -> None:
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    ledger.prepare(intent, policy, state, at=state.observed_at, assessor=ARTIFACT)
    if submitted:
        ledger.claim_submission(
            intent.account_id,
            intent.client_order_id,
            policy,
            state,
            at=state.observed_at,
            assessor=ARTIFACT,
        )
    if unknown:
        ledger.mark_unknown(
            intent.account_id, intent.client_order_id, at=state.observed_at
        )
    restarted = PaperOrderLedger(ledger.database)
    assert (
        restarted.expire_prepared(
            intent.account_id, at=intent.expires_at - timedelta(microseconds=1)
        )
        == 0
    )
    assert restarted.expire_prepared(intent.account_id, at=intent.expires_at) == (
        0 if submitted else 1
    )
    record = restarted.get(intent.account_id, intent.client_order_id)
    assert record.remaining_quantity == (intent.quantity if submitted else 0)
    if not submitted:
        assert record.status.value == "EXPIRED"
    assert restarted.expire_prepared(intent.account_id, at=intent.expires_at) == 0


def test_submission_claim_rechecks_fresh_risk_and_preserves_unclaimed_state_on_denial(
    tmp_path: Path,
) -> None:
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    ledger.prepare(intent, policy, state, at=state.observed_at, assessor=ARTIFACT)
    for at, checked in (
        (state.valid_until, state),
        (state.observed_at, state.model_copy(update={"kill_switch_active": True})),
    ):
        with pytest.raises(ValueError, match="expired|denies"):
            ledger.claim_submission(
                intent.account_id,
                intent.client_order_id,
                policy,
                checked,
                at=at,
                assessor=ARTIFACT,
            )
        assert (
            ledger.get(intent.account_id, intent.client_order_id).status.value
            == "PREPARED"
        )


@pytest.mark.parametrize(
    "status,filled", [("FILLED", 10), ("REJECTED", 0), ("CANCELED", 0)]
)
def test_terminal_reconciliation_is_idempotent_and_rejects_changed_evidence(
    tmp_path: Path, status: str, filled: int
) -> None:
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    ledger.prepare(intent, policy, state, at=state.observed_at, assessor=ARTIFACT)
    ledger.claim_submission(
        intent.account_id,
        intent.client_order_id,
        policy,
        state,
        at=state.observed_at,
        assessor=ARTIFACT,
    )
    update = PaperBrokerUpdate.model_validate(
        {
            "account_id": intent.account_id,
            "client_order_id": intent.client_order_id,
            "status": status,
            "filled_quantity": filled,
            "broker_order_id": "synthetic-terminal",
            "observed_at": state.observed_at,
            "evidence": ARTIFACT,
        }
    )
    record = ledger.reconcile(update)
    assert record.remaining_quantity == 0
    assert ledger.reconcile(update) == record
    with pytest.raises(PaperLedgerError):
        ledger.reconcile(
            update.model_copy(
                update={"observed_at": state.observed_at + timedelta(seconds=1)}
            )
        )


@pytest.mark.parametrize("cancel_timeout", [False, True])
def test_paper_http_submission_partial_cancel_and_reconciliation(
    tmp_path: Path, cancel_timeout: bool
) -> None:
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    requests: list[httpx.Request] = []
    broker = {"filled": 0, "canceled": False}

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "openapivts.koreainvestment.com"
        assert request.url.port == 29443
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "tokenP":
            return httpx.Response(
                200, json={"access_token": "synthetic-token", "expires_in": 3600}
            )
        assert request.headers["authorization"] == "Bearer synthetic-token"
        if route == "inquire-balance":
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output1": [],
                    "output2": [
                        {
                            "dnca_tot_amt": "10000000",
                            "tot_evlu_amt": "10000000",
                            "scts_evlu_amt": "0",
                        }
                    ],
                },
            )
        if route == "inquire-psbl-order":
            assert request.url.params["ORD_DVSN"] == "01"
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "ord_psbl_cash": "10000000",
                        "nrcvb_buy_amt": "10000000",
                        "nrcvb_buy_qty": "100",
                    },
                },
            )
        if route == "order-cash":
            assert request.headers["tr_id"] == "VTTC0012U"
            assert json.loads(request.content)["ORD_DVSN"] == "00"
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {"ODNO": "00001", "KRX_FWDG_ORD_ORGNO": "01234"},
                },
            )
        if route == "inquire-daily-ccld":
            if not any(item.url.path.endswith("order-cash") for item in requests):
                return httpx.Response(200, json={"rt_cd": "0", "output1": []})
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output1": [
                        {
                            "odno": "00001",
                            "pdno": "005930",
                            "ord_qty": "10",
                            "tot_ccld_qty": str(broker["filled"]),
                            "ord_dt": "20261001",
                            "sll_buy_dvsn_cd": "02",
                            "ord_unpr": "70000",
                            "cncl_yn": "Y" if broker["canceled"] else "N",
                            "rmn_qty": str(10 - int(broker["filled"])),
                            "ord_gno_brno": "01234",
                        }
                    ],
                },
            )
        if route == "order-rvsecncl":
            body = json.loads(request.content)
            assert body["ORGN_ODNO"] == "00001" and body["ORD_QTY"] == "7"
            assert body["RVSE_CNCL_DVSN_CD"] == "02"
            if cancel_timeout:
                raise httpx.ReadTimeout("synthetic cancel timeout", request=request)
            broker["canceled"] = True
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {"ODNO": "00002", "KRX_FWDG_ORD_ORGNO": "01234"},
                },
            )
        raise AssertionError(route)

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
        credentials,
        transport=httpx.MockTransport(transport),
        clock=lambda: state.observed_at,
    )
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    service = PaperExecutionService(client, ledger, ARTIFACT)
    assert service.submit(intent, policy, state).status.value == "ACCEPTED"
    with pytest.raises(PaperLedgerError):
        service.submit(intent, policy, state)
    broker["filled"] = 3
    assert (
        service.reconcile(intent.account_id, intent.client_order_id).remaining_quantity
        == 7
    )
    if cancel_timeout:
        with pytest.raises(PaperTransportError):
            service.cancel(intent.account_id, intent.client_order_id)
        with pytest.raises(PaperLedgerError, match="already claimed"):
            service.cancel(intent.account_id, intent.client_order_id)
        assert (
            len(
                [
                    request
                    for request in requests
                    if request.url.path.endswith("order-rvsecncl")
                ]
            )
            == 1
        )
        broker["canceled"] = True
    else:
        service.cancel(intent.account_id, intent.client_order_id)
    assert ledger.get(intent.account_id, intent.client_order_id).remaining_quantity == 7
    assert (
        service.reconcile(intent.account_id, intent.client_order_id).status.value
        == "CANCELED"
    )
    assert ledger.get(intent.account_id, intent.client_order_id).remaining_quantity == 0
    assert (
        len(
            [request for request in requests if request.url.path.endswith("order-cash")]
        )
        == 1
    )
    client.close()
    with pytest.raises(PaperTransportError, match="disarmed"):
        KisPaperClient(credentials)


@pytest.mark.parametrize("failure", ["timeout", "unauthorized", "bad-ack", "rejected"])
def test_paper_http_failed_submission_never_replays_after_restart(
    tmp_path: Path, failure: str
) -> None:
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    sends: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "tokenP":
            return httpx.Response(
                200, json={"access_token": "synthetic-token", "expires_in": 3600}
            )
        if route == "inquire-balance":
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output1": [],
                    "output2": [
                        {
                            "dnca_tot_amt": "10000000",
                            "tot_evlu_amt": "10000000",
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
                        "ord_psbl_cash": "10000000",
                        "nrcvb_buy_amt": "10000000",
                        "nrcvb_buy_qty": "100",
                    },
                },
            )
        if route == "inquire-daily-ccld":
            return httpx.Response(200, json={"rt_cd": "0", "output1": []})
        assert route == "order-cash"
        sends.append(route)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-secret must not leak", request=request)
        if failure == "unauthorized":
            return httpx.Response(401)
        if failure == "rejected":
            return httpx.Response(200, json={"rt_cd": "1", "msg1": "synthetic refusal"})
        return httpx.Response(200, json={"rt_cd": "0", "output": {}})

    client = KisPaperClient(
        _paper_credentials(),
        transport=httpx.MockTransport(transport),
        clock=lambda: state.observed_at,
    )
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    service = PaperExecutionService(client, ledger, ARTIFACT)
    if failure == "rejected":
        assert service.submit(intent, policy, state).status.value == "REJECTED"
    else:
        with pytest.raises(PaperTransportError) as error:
            service.submit(intent, policy, state)
        assert "synthetic-secret" not in str(error.value)
        assert (
            ledger.get(intent.account_id, intent.client_order_id).status.value
            == "UNKNOWN"
        )
    restarted = PaperExecutionService(
        client, PaperOrderLedger(ledger.database), ARTIFACT
    )
    with pytest.raises(PaperLedgerError):
        restarted.submit(intent, policy, state)
    assert sends == ["order-cash"]
    client.close()


@pytest.mark.parametrize("cycle", [False, True])
def test_paper_pagination_is_complete_or_fails_closed(cycle: bool) -> None:
    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tokenP"):
            return httpx.Response(
                200, json={"access_token": "synthetic-token", "expires_in": 3600}
            )
        calls.append(request)
        more = cycle or len(calls) == 1
        if len(calls) > 1:
            assert request.headers["tr_cont"] == "N"
            assert request.url.params["CTX_AREA_NK100"] == "next"
        return httpx.Response(
            200,
            headers={"tr_cont": "F" if more else "D"},
            json={
                "rt_cd": "0",
                "ctx_area_fk100": "key",
                "ctx_area_nk100": "next",
                "output1": [],
                "output2": [
                    {
                        "dnca_tot_amt": "10000000",
                        "tot_evlu_amt": "10000000",
                        "scts_evlu_amt": "0",
                    }
                ],
            },
        )

    client = KisPaperClient(
        _paper_credentials(),
        transport=httpx.MockTransport(transport),
        clock=lambda: _risk_state().observed_at,
    )
    if cycle:
        with pytest.raises(PaperTransportError, match="cyclic"):
            client.account()
    else:
        assert client.account().cash == Decimal("10000000")
    assert len(calls) == 2
    client.close()


def test_paper_token_uses_earlier_expiry_and_refreshes_without_replaying_orders() -> (
    None
):
    now = [_risk_state().observed_at]
    tokens: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tokenP"):
            tokens.append(request)
            from ats.domain.prices import SEOUL

            expiry = (
                (now[0] + timedelta(seconds=120))
                .astimezone(SEOUL)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-token",
                    "expires_in": 3600,
                    "access_token_token_expired": expiry,
                },
            )
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [],
                "output2": [
                    {
                        "dnca_tot_amt": "10000000",
                        "tot_evlu_amt": "10000000",
                        "scts_evlu_amt": "0",
                    }
                ],
            },
        )

    client = KisPaperClient(
        _paper_credentials(),
        transport=httpx.MockTransport(transport),
        clock=lambda: now[0],
    )
    client.account()
    now[0] += timedelta(seconds=60)
    client.account()
    assert len(tokens) == 2
    client.close()


def _paper_credentials() -> PaperCredentials:
    return PaperCredentials.model_validate(
        {
            "app_key": "synthetic-key",
            "app_secret": "synthetic-secret",
            "account_number": "12345678",
            "product_code": "01",
            "account_id": "paper-account",
        }
    )


def test_challenger_shadow_cannot_bypass_champion_or_create_paper_sessions() -> None:
    policy, state = _policy(), _risk_state()
    candidate = _intent(policy).model_copy(
        update={"strategy_version_id": UUID(int=99), "strategy_digest": OTHER_DIGEST}
    )
    result = shadow_risk(
        candidate, policy, state, at=state.observed_at, assessor=ARTIFACT
    )
    assert result.decision.outcome.value == "DENY"
    assert any(
        item.check.value == "CHAMPION" and item.status.value == "FAILED"
        for item in result.decision.checks
    )
    assert result.broker_requests == result.actual_paper_sessions == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("kill_switch_active", True),
        ("daily_loss_fraction", Decimal("0.01")),
        ("drawdown_fraction", Decimal("0.15")),
    ],
)
def test_portfolio_risk_stop_survives_fresh_state_and_restart(
    tmp_path: Path, field: str, value: object
) -> None:
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    ledger = PaperOrderLedger(tmp_path / "risk-halt.sqlite3")
    with pytest.raises(PaperLedgerError, match="persistent risk halt"):
        ledger.prepare(
            intent,
            policy,
            state.model_copy(update={field: value}),
            at=state.observed_at,
            assessor=ARTIFACT,
        )
    with pytest.raises(PaperLedgerError, match="halted"):
        PaperOrderLedger(ledger.database).prepare(
            intent, policy, state, at=state.observed_at, assessor=ARTIFACT
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            302, headers={"location": "https://openapi.koreainvestment.com:9443"}
        ),
        httpx.Response(200, content=b'{"rt_cd":"0","rt_cd":"1"}'),
        httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
        httpx.Response(200, json={"rt_cd": "0", "output1": ["not-an-object"]}),
    ],
)
def test_paper_transport_rejects_redirect_duplicate_oversize_and_malformed_rows(
    response: httpx.Response,
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tokenP"):
            return httpx.Response(
                200, json={"access_token": "synthetic-token", "expires_in": 3600}
            )
        return response

    client = KisPaperClient(
        _paper_credentials(), transport=httpx.MockTransport(transport)
    )
    with pytest.raises(PaperTransportError):
        client.account()
    client.close()


def test_unmanaged_open_order_blocks_paper_submission(tmp_path: Path) -> None:
    state, policy = _risk_state(), _policy()
    intent = _intent(policy)
    routes: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        route = request.url.path.rsplit("/", 1)[-1]
        routes.append(route)
        if route == "tokenP":
            return httpx.Response(
                200, json={"access_token": "synthetic-token", "expires_in": 3600}
            )
        assert route == "inquire-daily-ccld"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [
                    {
                        "odno": "unmanaged",
                        "rmn_qty": "1",
                        "tot_ccld_qty": "0",
                        "ord_qty": "1",
                        "cncl_yn": "N",
                    }
                ],
            },
        )

    client = KisPaperClient(
        _paper_credentials(),
        transport=httpx.MockTransport(transport),
        clock=lambda: state.observed_at,
    )
    service = PaperExecutionService(
        client, PaperOrderLedger(tmp_path / "paper.sqlite3"), ARTIFACT
    )
    with pytest.raises(PaperTransportError, match="unmanaged"):
        service.submit(intent, policy, state)
    assert routes == ["tokenP", "inquire-daily-ccld"]
    with pytest.raises(PaperLedgerError, match="halted"):
        service.ledger.claim_submission(
            intent.account_id,
            intent.client_order_id,
            policy,
            state,
            at=state.observed_at,
            assessor=ARTIFACT,
        )
    client.close()


def test_risk_denial_precedes_any_paper_http_and_transmission_claim_is_single_use(
    tmp_path: Path,
) -> None:
    state, policy = _risk_state(), _policy()
    intent = _intent(policy)

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("risk denial must precede network")

    client = KisPaperClient(
        _paper_credentials(),
        transport=httpx.MockTransport(forbidden),
        clock=lambda: state.observed_at,
    )
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    with pytest.raises(ValueError, match="denies"):
        PaperExecutionService(client, ledger, ARTIFACT).submit(
            intent, policy, state.model_copy(update={"kill_switch_active": True})
        )
    with pytest.raises(PaperLedgerError, match="halted"):
        PaperOrderLedger(ledger.database).prepare(
            intent, policy, state, at=state.observed_at, assessor=ARTIFACT
        )
    ledger = PaperOrderLedger(tmp_path / "unhalted.sqlite3")
    ledger.prepare(intent, policy, state, at=state.observed_at, assessor=ARTIFACT)
    ledger.claim_submission(
        intent.account_id,
        intent.client_order_id,
        policy,
        state,
        at=state.observed_at,
        assessor=ARTIFACT,
    )

    def consume(number: int) -> bool:
        try:
            PaperOrderLedger(ledger.database).start_transmission(
                intent.account_id, intent.client_order_id, policy, at=state.observed_at
            )
            return True
        except PaperLedgerError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(consume, (1, 2))) == [False, True]
    client.close()


def test_paper_unknown_recovery_requires_bound_external_authorization(
    tmp_path: Path,
) -> None:
    policy, state = _policy(), _risk_state()
    intent = _intent(policy)
    ledger = PaperOrderLedger(tmp_path / "paper.sqlite3")
    ledger.prepare(intent, policy, state, at=state.observed_at, assessor=ARTIFACT)
    ledger.claim_submission(
        intent.account_id,
        intent.client_order_id,
        policy,
        state,
        at=state.observed_at,
        assessor=ARTIFACT,
    )
    ledger.start_transmission(
        intent.account_id, intent.client_order_id, policy, at=state.observed_at
    )
    ledger.mark_unknown(intent.account_id, intent.client_order_id, at=state.observed_at)
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("tokenP"):
            return httpx.Response(
                200, json={"access_token": "synthetic-token", "expires_in": 3600}
            )
        assert request.url.path.endswith("inquire-daily-ccld")
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [
                    {
                        "odno": "00001",
                        "pdno": "005930",
                        "ord_qty": "10",
                        "tot_ccld_qty": "0",
                        "ord_dt": "20261001",
                        "sll_buy_dvsn_cd": "02",
                        "ord_unpr": "70000",
                        "cncl_yn": "N",
                        "rmn_qty": "10",
                        "ord_gno_brno": "01234",
                    }
                ],
            },
        )

    client = KisPaperClient(
        _paper_credentials(),
        transport=httpx.MockTransport(transport),
        clock=lambda: state.observed_at,
    )
    request = PaperRecovery(
        account_id=intent.account_id,
        client_order_id=intent.client_order_id,
        broker_order_id="00001",
        record_digest=evidence_digest(
            ledger.get(intent.account_id, intent.client_order_id)
        ),
        expires_at=state.observed_at + timedelta(seconds=30),
        approval=ARTIFACT,
        resume=True,
    )
    with pytest.raises(PaperLedgerError, match="authorization"):
        PaperExecutionService(client, ledger, ARTIFACT).recover(request)
    assert calls == []
    service = PaperExecutionService(
        client,
        ledger,
        ARTIFACT,
        recovery_authorizer=lambda candidate: candidate == request,
    )
    recovered = service.recover(request)
    assert (
        recovered.status.value == "ACCEPTED" and recovered.recovery_approval == ARTIFACT
    )
    with pytest.raises(PaperLedgerError, match="authorization"):
        service.recover(request)
    assert all(not path.endswith("order-cash") for path in calls)
    client.close()


def _policy() -> RiskPolicy:
    return RiskPolicy.model_validate(
        {
            "metadata": {
                "policy_id": "paper-risk",
                "version": "1.0.0",
                "status": "APPROVED",
                "approved_by": "test-operator",
                "approved_at": CREATED,
            },
            "limits": {
                "max_symbol_weight": 0.1,
                "max_gross_exposure": 1.0,
                "daily_portfolio_loss_halt": 0.01,
                "portfolio_drawdown_halt": 0.15,
                "max_price_age_seconds": 900,
            },
        }
    )


def _intent(policy: RiskPolicy) -> OrderIntent:
    return OrderIntent.model_validate(
        {
            "intent_id": UUID(int=1),
            "client_order_id": "paper-order-001",
            "account_id": "paper-account",
            "strategy_version_id": UUID(int=2),
            "strategy_digest": DIGEST,
            "champion_selection": ARTIFACT,
            "dataset_snapshot": {
                "snapshot_id": "eod-snapshot",
                "observed_through": CREATED,
                "digest": DIGEST,
            },
            "signal": ARTIFACT,
            "risk_policy": {
                "policy_id": policy.metadata.policy_id,
                "version": policy.metadata.version,
                "digest": evidence_digest(policy),
            },
            "instrument_id": "krx-005930",
            "asset_class": "EQUITY",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10,
            "limit_price": "70000.00",
            "signal_session": date(2026, 9, 30),
            "execution_session": date(2026, 10, 1),
            "created_at": CREATED,
            "expires_at": CREATED + timedelta(days=1),
        }
    )


def _decision_payload(intent: OrderIntent) -> dict[str, object]:
    return {
        "decision_id": UUID(int=3),
        "intent_id": intent.intent_id,
        "intent_digest": evidence_digest(intent),
        "policy": intent.risk_policy,
        "assessor": ARTIFACT,
        "outcome": "ALLOW",
        "reason": "Synthetic checks passed.",
        "checked_at": CHECKED,
        "expires_at": EXPIRY,
        "checks": tuple(
            {"check": check, "status": "PASSED", "report": ARTIFACT}
            for check in RiskCheck
        ),
    }


def _risk_state() -> RiskState:
    opened = datetime(2026, 10, 1, tzinfo=UTC)
    return RiskState.model_validate(
        {
            "account_id": "paper-account",
            "instrument_id": "krx-005930",
            "observed_at": opened,
            "valid_until": opened + timedelta(seconds=30),
            "equity": "10000000",
            "cash": "10000000",
            "gross_exposure": "0",
            "reserved_cash": "0",
            "reserved_buy_notional": "0",
            "held_quantity": 0,
            "reserved_sell_quantity": 0,
            "quote_price": "70000",
            "quote_at": opened,
            "daily_loss_fraction": "0",
            "drawdown_fraction": "0",
            "champion_version_id": UUID(int=2),
            "champion_digest": DIGEST,
            "champion_selection": ARTIFACT,
            "universe_members": ("krx-005930",),
            "duplicate_client_order_id": False,
            "kill_switch_active": False,
            "opening_window_start": opened,
            "opening_window_end": opened + timedelta(minutes=5),
            "fee_reserve_bps": 10,
        }
    )


def test_independent_assessor_computes_checks_and_binds_state() -> None:
    policy = _policy()
    intent = _intent(policy)
    state = _risk_state()
    decision = assess_limit_intent(
        intent, policy, state, at=state.observed_at, assessor=ARTIFACT
    )
    assert decision.outcome.value == "ALLOW"
    decision.validate_for_intent(intent, policy, at=state.observed_at)
    assert all(
        check.report.digest == evidence_digest(state) for check in decision.checks
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("kill_switch_active", True, "KILL_SWITCH"),
        ("daily_loss_fraction", "0.01", "DAILY_LOSS"),
        ("drawdown_fraction", "0.15", "DRAWDOWN"),
        ("reserved_cash", "9999999", "CASH_AVAILABLE"),
        ("reserved_buy_notional", "1000000", "SYMBOL_WEIGHT"),
        ("duplicate_client_order_id", True, "DUPLICATE_ORDER"),
        ("universe_members", (), "UNIVERSE"),
        ("champion_digest", OTHER_DIGEST, "CHAMPION"),
        ("quote_at", datetime(2026, 9, 30, tzinfo=UTC), "PRICE_FRESHNESS"),
    ],
)
def test_independent_assessor_denies_real_failure_conditions(
    field: str, value: object, reason: str
) -> None:
    state = RiskState.model_validate({**_risk_state().model_dump(), field: value})
    decision = assess_limit_intent(
        _intent(_policy()), _policy(), state, at=state.observed_at, assessor=ARTIFACT
    )
    assert decision.outcome.value == "DENY"
    assert reason in decision.reason


def test_independent_assessor_denies_oversized_buys_and_short_sales() -> None:
    state = _risk_state()
    policy = _policy()
    buy = OrderIntent.model_validate({**_intent(policy).model_dump(), "quantity": 1000})
    sell = OrderIntent.model_validate({**_intent(policy).model_dump(), "side": "SELL"})
    for intent in (buy, sell):
        assert (
            assess_limit_intent(
                intent, policy, state, at=state.observed_at, assessor=ARTIFACT
            ).outcome.value
            == "DENY"
        )


def test_independent_assessor_fails_on_expired_state_and_draft_policy() -> None:
    state = _risk_state()
    with pytest.raises(ValueError, match="expired"):
        assess_limit_intent(
            _intent(_policy()),
            _policy(),
            state,
            at=state.valid_until,
            assessor=ARTIFACT,
        )
    policy = RiskPolicy.model_validate(
        {
            **_policy().model_dump(),
            "metadata": {"policy_id": "paper-risk-policy", "version": "1.0.0"},
        }
    )
    with pytest.raises(ValueError, match="unapproved"):
        assess_limit_intent(
            _intent(policy), policy, state, at=state.observed_at, assessor=ARTIFACT
        )


def test_paper_contract_round_trip_binding_and_immutability() -> None:
    policy = _policy()
    intent = _intent(policy)
    rebuilt = OrderIntent.model_validate_json(intent.model_dump_json())
    assert rebuilt == intent
    assert evidence_digest(rebuilt) == evidence_digest(intent)
    assert rebuilt.limit_price == Decimal("70000.00")
    decision = RiskDecision.model_validate(_decision_payload(intent))
    assert RiskDecision.model_validate_json(decision.model_dump_json()) == decision
    decision.validate_for_intent(intent, policy, at=CHECKED)
    with pytest.raises(ValidationError):
        intent.__setattr__("quantity", 20)
    with pytest.raises(ValidationError):
        decision.checks[0].__setattr__("status", "FAILED")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("broker_environment", "KIS_LIVE"),
        ("funding", "MARGIN"),
        ("position_scope", "SHORT"),
        ("asset_class", "FUTURE"),
        ("currency", "USD"),
        ("quantity", 0),
        ("quantity", -1),
        ("quantity", True),
        ("quantity", 1.5),
        ("quantity", "10"),
        ("limit_price", "NaN"),
        ("limit_price", "Infinity"),
        ("limit_price", "0"),
        ("limit_price", "-1"),
        ("limit_price", None),
        ("order_type", "MARKET"),
        ("execution_phase", "INTRADAY"),
        ("execution_session", date(2026, 9, 30)),
        ("expires_at", CREATED),
        ("created_at", CREATED - timedelta(seconds=1)),
        ("created_at", datetime(2026, 9, 30)),
        ("risk_override", True),
    ],
)
def test_invalid_order_intents_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        OrderIntent.model_validate({**_intent(_policy()).model_dump(), field: value})


def test_market_sell_intent_still_requires_risk_assessment() -> None:
    intent = OrderIntent.model_validate(
        {
            **_intent(_policy()).model_dump(),
            "side": "SELL",
            "order_type": "MARKET",
            "limit_price": None,
        }
    )
    assert intent.position_scope == "LONG_ONLY"
    assert intent.limit_price is None


@pytest.mark.parametrize("check", list(RiskCheck))
@pytest.mark.parametrize("status", ["MISSING", "FAILED", "UNKNOWN"])
def test_allow_requires_complete_passing_checks(check: RiskCheck, status: str) -> None:
    payload = _decision_payload(_intent(_policy()))
    checks = [
        {"check": other, "status": "PASSED", "report": ARTIFACT}
        for other in RiskCheck
        if other != check
    ]
    if status != "MISSING":
        checks.append({"check": check, "status": status, "report": ARTIFACT})
    payload["checks"] = checks
    with pytest.raises(ValidationError, match="every risk check"):
        RiskDecision.model_validate(payload)


def test_default_deny_and_duplicate_checks_fail_closed() -> None:
    policy = _policy()
    intent = _intent(policy)
    payload = _decision_payload(intent)
    del payload["outcome"]
    payload["checks"] = ()
    decision = RiskDecision.model_validate(payload)
    with pytest.raises(ValueError, match="denies"):
        decision.validate_for_intent(intent, policy, at=CHECKED)
    check = {"check": "KILL_SWITCH", "status": "FAILED", "report": ARTIFACT}
    with pytest.raises(ValidationError, match="unique"):
        RiskDecision.model_validate({**payload, "checks": (check, check)})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", 11),
        ("side", "SELL"),
        ("limit_price", "71000"),
        ("account_id", "other-account"),
        ("client_order_id", "other-order"),
        ("instrument_id", "krx-000660"),
        ("strategy_digest", OTHER_DIGEST),
        ("intent_id", UUID(int=99)),
    ],
)
def test_decision_cannot_be_reused_for_changed_intent(
    field: str, value: object
) -> None:
    policy = _policy()
    intent = _intent(policy)
    decision = RiskDecision.model_validate(_decision_payload(intent))
    changed = OrderIntent.model_validate({**intent.model_dump(), field: value})
    with pytest.raises(ValueError, match="exact intent"):
        decision.validate_for_intent(changed, policy, at=CHECKED)


@pytest.mark.parametrize(
    "at",
    [
        CHECKED - timedelta(microseconds=1),
        EXPIRY,
        EXPIRY + timedelta(seconds=1),
        datetime(2026, 9, 30),
    ],
)
def test_decision_rejects_invalid_validation_time(at: datetime) -> None:
    policy = _policy()
    intent = _intent(policy)
    with pytest.raises(ValueError):
        RiskDecision.model_validate(_decision_payload(intent)).validate_for_intent(
            intent, policy, at=at
        )


@pytest.mark.parametrize("status", ["DRAFT", "RETIRED"])
def test_inactive_policy_cannot_support_allow(status: str) -> None:
    base = _policy()
    policy = RiskPolicy.model_validate(
        {
            **base.model_dump(),
            "metadata": {
                **base.metadata.model_dump(),
                "status": status,
                "approved_at": None,
                "approved_by": None,
            },
        }
    )
    intent = _intent(policy)
    with pytest.raises(ValueError, match="approved before"):
        RiskDecision.model_validate(_decision_payload(intent)).validate_for_intent(
            intent, policy, at=CHECKED
        )


def test_changed_policy_digest_and_disallowed_asset_fail_closed() -> None:
    policy = _policy()
    intent = _intent(policy)
    changed = RiskPolicy.model_validate(
        {
            **policy.model_dump(),
            "limits": {**policy.limits.model_dump(), "max_symbol_weight": 0.05},
        }
    )
    decision = RiskDecision.model_validate(_decision_payload(intent))
    with pytest.raises(ValueError, match="policy reference"):
        decision.validate_for_intent(intent, changed, at=CHECKED)
    restricted = RiskPolicy.model_validate(
        {
            **policy.model_dump(),
            "scope": {**policy.scope.model_dump(), "allowed_asset_classes": ["ETF"]},
        }
    )
    intent = _intent(restricted)
    with pytest.raises(ValueError, match="asset class"):
        RiskDecision.model_validate(_decision_payload(intent)).validate_for_intent(
            intent, restricted, at=CHECKED
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checked_at", CREATED - timedelta(seconds=1)),
        ("expires_at", CREATED + timedelta(days=2)),
    ],
)
def test_decision_must_fit_intent_lifetime(field: str, value: datetime) -> None:
    policy = _policy()
    intent = _intent(policy)
    payload = _decision_payload(intent)
    payload[field] = value
    with pytest.raises(ValueError):
        RiskDecision.model_validate(payload).validate_for_intent(
            intent, policy, at=CHECKED
        )
