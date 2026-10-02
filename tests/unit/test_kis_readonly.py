from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.kis_readonly import (
    ReadOnlyAccessPermit,
    ReadOnlyCredentials,
    ReadOnlyError,
    ReadOnlyKisClient,
    WindowsCredentialProvider,
)

NOW = datetime(2026, 10, 2, 0, tzinfo=UTC)


def test_persistent_collector_rejects_concurrency_and_restart_token(
    tmp_path: Path,
) -> None:
    from ats.readonly_runtime import ReadOnlyRuntime

    permit, _, _ = configuration()
    runtime = ReadOnlyRuntime(tmp_path / "control.sqlite3")
    with runtime.collector(permit, clock=lambda: NOW) as owner:
        with (
            pytest.raises(ReadOnlyError, match="owned"),
            ReadOnlyRuntime(runtime.database).collector(permit, clock=lambda: NOW),
        ):
            pytest.fail("second collector entered")
        runtime.before_request(permit, owner, token=True, at=NOW)
        with pytest.raises(ReadOnlyError, match="termination"):
            runtime.recover_owner(permit, authorize=lambda: None)
    renewed = permit.model_copy(update={"max_calls": 61})
    with (
        ReadOnlyRuntime(runtime.database).collector(
            renewed, clock=lambda: NOW
        ) as owner,
        pytest.raises(ReadOnlyError, match="hold"),
    ):
        runtime.before_request(
            renewed, owner, token=True, at=NOW + timedelta(minutes=2)
        )
    assert b"synthetic-secret" not in runtime.database.read_bytes()


def test_provisioning_requires_authorization_before_prompt_or_write() -> None:
    from ats.readonly_owner import provision_credentials

    permit, _, credentials = configuration()
    answers = iter(("synthetic-app", "synthetic-secret", "12345678", "01"))
    writes: list[tuple[str, bytes]] = []

    def deny() -> None:
        raise ReadOnlyError("owner denied")

    with pytest.raises(ReadOnlyError, match="denied"):
        provision_credentials(
            permit,
            authorize=deny,
            prompt=lambda label: pytest.fail("secret prompt before authorization"),
            write=lambda target, payload: pytest.fail("write before authorization"),
        )
    provision_credentials(
        permit,
        authorize=lambda: None,
        prompt=lambda label: next(answers),
        write=lambda target, payload: writes.append((target, payload)),
    )
    assert len(writes) == 1 and writes[0][0] == "ATS/KIS/fixture-slot"
    assert ReadOnlyCredentials.model_validate_json(writes[0][1]) == credentials


def test_owner_scope_signature_and_replay_are_independent(tmp_path: Path) -> None:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from ats.operator import OperatorAuth
    from ats.readonly_owner import OwnerAuthority

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    auth = OperatorAuth.model_validate(
        {
            "issuer": "synthetic-local-issuer",
            "audience": "synthetic-local-owner",
            "verification_key": public,
            "algorithm": "RS256",
            "synthetic_only": False,
            "identities": [
                {"subject": "synthetic-human", "role": "OPERATOR", "human": True}
            ],
        }
    )
    current = datetime.now(UTC)
    target_digest = "sha256:" + "a" * 64
    claims = {
        "sub": "synthetic-human",
        "iss": auth.issuer,
        "aud": auth.audience,
        "iat": current,
        "nbf": current,
        "exp": current + timedelta(minutes=2),
        "jti": "synthetic-once",
        "actor_type": "human",
        "action": "READ_ONCE",
        "target_digest": target_digest,
    }
    token = SecretStr(jwt.encode(claims, key, algorithm="RS256"))
    authority = OwnerAuthority(tmp_path / "authority.sqlite3")
    with pytest.raises(ReadOnlyError, match="scope"):
        authority.consume(
            auth,
            token,
            action="PROVISION_CREDENTIALS",
            target_digest=target_digest,
        )
    authority.consume(auth, token, action="READ_ONCE", target_digest=target_digest)
    with pytest.raises(ReadOnlyError, match="consumed"):
        authority.consume(auth, token, action="READ_ONCE", target_digest=target_digest)
    assert token.get_secret_value().encode() not in authority.database.read_bytes()


def test_bundle_keeps_quote_event_time_and_reservation_coverage_unknown() -> None:
    permit, policy, credentials = configuration()
    permit = permit.model_copy(
        update={
            "operations": ("BALANCE", "CAPACITY", "QUOTE", "OPEN_ORDERS", "HISTORY"),
            "symbols": ("005930",),
            "history_start": NOW.date(),
        }
    )
    order = {
        "ord_gno_brno": "001",
        "odno": "123",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02",
        "ord_qty": "2",
        "tot_ccld_qty": "1",
        "psbl_qty": "1",
        "rmn_qty": "1",
        "ord_unpr": "100",
        "ord_dt": "20261002",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            return token_response()
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
            output["output"] = [order]
        else:
            output["output1"] = [order]
        return httpx.Response(200, json=output)

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW,
    )
    try:
        bundle = client.observation_bundle("005930", NOW.date())
        assert bundle.quote is not None and bundle.quote.exchange_traded_at is None
        assert (
            bundle.reservation_crosscheck == "MATCH"
            and not bundle.external_reservations_verified
        )
        assert len(bundle.orders) == 2 and len(bundle.receipts) == 5
        assert "12345678" not in bundle.model_dump_json()
    finally:
        client.close()


def configuration() -> tuple[
    ReadOnlyAccessPermit, SourceAllowlist, ReadOnlyCredentials
]:
    metadata = {
        "policy_id": "synthetic-readonly",
        "version": "1",
        "status": "APPROVED",
        "approved_by": "fixture-only",
        "approved_at": NOW - timedelta(days=1),
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
                    "rights": {"classification": "LICENSED", "retention_days": 1},
                    "rate_limit_per_minute": 60,
                    "notes": "Synthetic only",
                }
            ],
        }
    )
    permit = ReadOnlyAccessPermit.model_validate(
        {
            "metadata": metadata,
            "account_id": "fixture-account",
            "credential_ref": "fixture-slot",
            "binding_id": UUID(int=1),
            "source_policy_digest": evidence_digest(policy),
            "starts_at": NOW - timedelta(minutes=1),
            "expires_at": NOW + timedelta(hours=1),
            "enabled": True,
            "synthetic_only": True,
            "rate_per_minute": 60,
        }
    )
    credentials = ReadOnlyCredentials(
        account_id=permit.account_id,
        credential_ref=permit.credential_ref,
        binding_id=permit.binding_id,
        app_key=SecretStr("synthetic-app"),
        app_secret=SecretStr("synthetic-secret"),
        account_number=SecretStr("12345678"),
        product_code="01",
    )
    return permit, policy, credentials


def test_authority_and_binding_precede_credentials_and_http() -> None:
    permit, policy, credentials = configuration()
    loaded: list[str] = []
    calls: list[httpx.Request] = []

    def load(reference: str) -> ReadOnlyCredentials:
        loaded.append(reference)
        return credentials

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        load,
        account_id="other-account",
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(500)
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(ReadOnlyError):
        client.balance_pages()
    permit = permit.model_copy(update={"operations": ("order-cash",)})
    with pytest.raises(ReadOnlyError):
        client.balance_pages()
    assert not loaded and not calls
    client.close()


def test_protected_credential_provider_only_uses_scoped_mock_backend() -> None:
    _, _, credentials = configuration()
    accesses: list[str] = []

    def backend(target: str) -> bytes:
        accesses.append(target)
        data = credentials.model_dump(mode="json")
        data.update(
            app_key="synthetic-app",
            app_secret="synthetic-secret",
            account_number="12345678",
        )
        import json

        return json.dumps(data).encode()

    provider = WindowsCredentialProvider(backend=backend)
    assert provider("fixture-slot") == credentials
    assert accesses == ["ATS/KIS/fixture-slot"]
    with pytest.raises(ReadOnlyError):
        provider("../../other-secret")
    assert len(accesses) == 1
    with pytest.raises(ReadOnlyError) as failure:
        WindowsCredentialProvider(
            backend=lambda target: b'{"app_secret":"sensitive-fixture"}'
        )("fixture-slot")
    assert "sensitive-fixture" not in str(failure.value)


def test_balance_uses_fixed_production_get_and_memory_token() -> None:
    permit, policy, credentials = configuration()
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert (
            request.url.host == "openapi.koreainvestment.com"
            and request.url.port == 9443
        )
        if request.method == "POST":
            assert request.url.path == "/oauth2/tokenP"
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-token",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                    "access_token_token_expired": "2026-10-03 09:00:00",
                },
            )
        assert request.headers["tr_id"] == "TTTC8434R"
        assert request.url.params["CANO"] == "12345678"
        return httpx.Response(200, json={"rt_cd": "0", "output1": [], "output2": []})

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW,
    )
    _, receipts = client.balance_pages()
    client.balance_pages()
    assert [request.method for request in calls] == ["POST", "GET", "GET"]
    assert receipts[0].mode == "SYNTHETIC"
    assert "synthetic-secret" not in str(client.audit)
    client.stop()
    with pytest.raises(ReadOnlyError):
        client.balance_pages()
    assert len(calls) == 3
    client.close()


def token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "synthetic-token",
            "token_type": "Bearer",
            "expires_in": 86400,
            "access_token_token_expired": "2026-10-03 09:00:00",
        },
    )


@pytest.mark.parametrize("status", [401, 403, 302, 500, 429])
def test_failures_are_bounded_and_redacted(status: int) -> None:
    permit, policy, credentials = configuration()
    permit = permit.model_copy(update={"max_retries": 2})
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return (
            token_response()
            if request.method == "POST"
            else httpx.Response(
                status,
                json={"msg1": "synthetic-secret 12345678"},
                headers={"location": "https://evil.example.test"},
            )
        )

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW,
    )
    with pytest.raises(ReadOnlyError) as failure:
        client.balance_pages()
    assert "synthetic-secret" not in str(failure.value) and "12345678" not in str(
        failure.value
    )
    assert calls.count("POST") == 1 and calls.count("GET") == (
        3 if status in (429, 500) else 1
    )
    client.close()


@pytest.mark.parametrize(
    "problem",
    ["loop", "missing", "budget", "duplicate-json", "account-echo", "oversized"],
)
def test_incomplete_or_untrusted_responses_never_publish(problem: str) -> None:
    permit, policy, credentials = configuration()
    if problem == "budget":
        permit = permit.model_copy(update={"max_calls": 1})

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return token_response()
        if problem == "duplicate-json":
            return httpx.Response(
                200,
                content=b'{"rt_cd":"0","rt_cd":"1"}',
                headers={"content-type": "application/json"},
            )
        if problem == "account-echo":
            return httpx.Response(200, json={"rt_cd": "0", "CANO": "99999999"})
        if problem == "oversized":
            return httpx.Response(
                200,
                content=b"x" * (permit.max_bytes + 1),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                **(
                    {"ctx_area_fk100": "same", "ctx_area_nk100": "same"}
                    if problem == "loop"
                    else {}
                ),
            },
            headers={"tr_cont": "M"},
        )

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW,
    )
    with pytest.raises(ReadOnlyError):
        client.balance_pages()
    client.close()


def test_account_values_remain_distinct_and_unknowns_are_not_zero() -> None:
    permit, policy, credentials = configuration()
    permit = permit.model_copy(
        update={"operations": ("BALANCE", "CAPACITY"), "symbols": ("005930",)}
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return token_response()
        if request.headers["tr_id"] == "TTTC8908R":
            assert request.url.params["ORD_DVSN"] == "01"
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "ord_psbl_cash": "900",
                        "nrcvb_buy_amt": "800",
                        "nrcvb_buy_qty": "8",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [
                    {
                        "pdno": "005930",
                        "hldg_qty": "2",
                        "ord_psbl_qty": "1",
                        "evlu_amt": "200",
                    }
                ],
                "output2": [
                    {
                        "dnca_tot_amt": "1000",
                        "tot_evlu_amt": "1250",
                        "scts_evlu_amt": "200",
                    }
                ],
            },
        )

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW,
    )
    observation = client.account_observation(symbol="005930", price=Decimal(100))
    assert (
        observation.deposit_balance == 1000
        and observation.cash_orderable == 900
        and observation.no_margin_buy_amount == 800
    )
    assert (
        observation.total_equity == 1250
        and observation.unsettled_receivable is None
        and observation.credit_amount is None
    )
    assert observation.external_reservations_verified is False
    assert "12345678" not in observation.model_dump_json()
    client.close()


def test_concurrent_reads_share_one_token_and_revocation_precedes_next_get() -> None:
    permit, policy, credentials = configuration()
    methods: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return (
            token_response()
            if request.method == "POST"
            else httpx.Response(200, json={"rt_cd": "0"})
        )

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW,
    )
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(client.balance_pages) for _ in range(4)]
        for future in futures:
            future.result()
    assert methods.count("POST") == 1 and methods.count("GET") == 4
    permit = permit.model_copy(update={"enabled": False})
    with pytest.raises(ReadOnlyError):
        client.balance_pages()
    assert len(methods) == 5
    client.close()


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"expires_at": NOW},
        {"synthetic_only": False},
        {"operations": ("CAPACITY",)},
        {"rate_per_minute": 61},
    ],
)
def test_invalid_permit_never_loads_credentials(change: dict[str, object]) -> None:
    permit, policy, credentials = configuration()
    permit = permit.model_copy(update=change)
    loaded: list[str] = []

    def load(reference: str) -> ReadOnlyCredentials:
        loaded.append(reference)
        return credentials

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        load,
        account_id=permit.account_id,
        transport=httpx.MockTransport(lambda request: pytest.fail("network reached")),
        clock=lambda: NOW,
    )
    with pytest.raises(ReadOnlyError):
        client.balance_pages()
    assert not loaded
    client.close()


def test_wrong_credential_binding_never_sends_token() -> None:
    permit, policy, credentials = configuration()
    credentials = credentials.model_copy(update={"binding_id": UUID(int=99)})
    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(lambda request: pytest.fail("network reached")),
        clock=lambda: NOW,
    )
    with pytest.raises(ReadOnlyError, match="binding"):
        client.balance_pages()
    assert not client.audit
    client.close()


def test_external_stop_during_response_prevents_next_page() -> None:
    permit, policy, credentials = configuration()
    stopped = [False]
    calls: list[str] = []

    def guard() -> None:
        if stopped[0]:
            raise ValueError("operator stop")

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return token_response()
        stopped[0] = True
        return httpx.Response(
            200,
            json={"rt_cd": "0", "ctx_area_fk100": "next", "ctx_area_nk100": "next"},
            headers={"tr_cont": "M"},
        )

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW,
        guard=guard,
    )
    with pytest.raises(ReadOnlyError):
        client.balance_pages()
    assert calls == ["POST", "GET"]
    with pytest.raises(ReadOnlyError):
        client.balance_pages()
    assert calls == ["POST", "GET"]
    client.close()


def test_absolute_token_expiry_and_total_deadline_are_enforced() -> None:
    permit, policy, credentials = configuration()
    elapsed = [0.0]
    counts = {"POST": 0, "GET": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        counts[request.method] += 1
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-token",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                    "access_token_token_expired": "2026-10-02 09:02:00"
                    if counts["POST"] == 1
                    else "2026-10-03 09:00:00",
                },
            )
        return httpx.Response(200, json={"rt_cd": "0"})

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW + timedelta(seconds=elapsed[0]),
        monotonic=lambda: elapsed[0],
    )
    client.balance_pages()
    elapsed[0] = 61
    client.balance_pages()
    assert counts == {"POST": 2, "GET": 2}
    elapsed[0] = 120
    with pytest.raises(ReadOnlyError):
        client.balance_pages()
    assert counts == {"POST": 2, "GET": 2}
    client.close()


def test_all_typed_read_routes_share_auth_without_account_leak_in_quote() -> None:
    permit, policy, credentials = configuration()
    permit = permit.model_copy(
        update={
            "operations": (
                "BALANCE",
                "CAPACITY",
                "HISTORY",
                "OPEN_ORDERS",
                "QUOTE",
                "DAILY",
            ),
            "symbols": ("005930",),
            "history_start": NOW.date() - timedelta(days=10),
        }
    )
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return token_response()
        if "/quotations/" in request.url.path:
            assert (
                "CANO" not in request.url.params
                and "ACNT_PRDT_CD" not in request.url.params
            )
        return httpx.Response(200, json={"rt_cd": "0"})

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW,
    )
    client.quote("005930")
    client.capacity("005930", Decimal(100))
    client.history(NOW.date(), NOW.date())
    client.open_orders()
    client.daily_prices(
        "005930", NOW.date() - timedelta(days=1), NOW.date() - timedelta(days=1)
    )
    assert len(paths) == 6 and paths.count("/oauth2/tokenP") == 1
    with pytest.raises(ReadOnlyError):
        client.quote("000660")
    with pytest.raises(ReadOnlyError):
        client.history(NOW.date() - timedelta(days=90), NOW.date())
    assert len(paths) == 6
    client.close()


def test_changing_balance_summary_rejects_whole_observation() -> None:
    permit, policy, credentials = configuration()
    permit = permit.model_copy(
        update={"operations": ("BALANCE", "CAPACITY"), "symbols": ("005930",)}
    )
    pages = [0]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return token_response()
        if request.headers["tr_id"] == "TTTC8908R":
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "ord_psbl_cash": "1000",
                        "nrcvb_buy_amt": "1000",
                        "nrcvb_buy_qty": "10",
                    },
                },
            )
        pages[0] += 1
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [],
                "output2": [
                    {
                        "dnca_tot_amt": str(1000 + pages[0]),
                        "tot_evlu_amt": "1000",
                        "scts_evlu_amt": "0",
                    }
                ],
                "ctx_area_fk100": "page-two",
                "ctx_area_nk100": "page-two",
            },
            headers={"tr_cont": "M" if pages[0] == 1 else ""},
        )

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        lambda reference: credentials,
        account_id=permit.account_id,
        transport=httpx.MockTransport(respond),
        clock=lambda: NOW,
    )
    with pytest.raises(ReadOnlyError, match="inconsistent"):
        client.account_observation(symbol="005930", price=Decimal(100))
    client.close()
