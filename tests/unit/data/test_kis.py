import json
import traceback
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from ats.data.kis import (
    KisCredentials,
    KisDailyRequest,
    KisQuoteClient,
    KisQuoteError,
    main,
)
from ats.data.prices import normalize_kis_daily_prices
from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist, load_policy

NOW = datetime(2026, 10, 1, tzinfo=UTC)
TOKEN_PATH = "/oauth2/tokenP"
DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
CREDENTIALS = KisCredentials(SecretStr("fixture-app-key"), SecretStr("fixture-secret"))
REQUEST = KisDailyRequest(
    symbol="005930", start=date(2026, 9, 1), end=date(2026, 9, 30)
)


def _policy(**source_changes: object) -> SourceAllowlist:
    return SourceAllowlist.model_validate(
        {
            "metadata": {
                "policy_id": "test-source-policy",
                "version": "0.1.0",
                "status": "APPROVED",
                "approved_by": "synthetic-test-operator",
                "approved_at": NOW - timedelta(days=1),
            },
            "sources": [
                {
                    "source_id": "kis-market",
                    "category": "MARKET",
                    "enabled": True,
                    "legal_review": "APPROVED",
                    "rate_limit_per_minute": 30,
                    "rights": {"classification": "LICENSED", "retention_days": 1},
                    "notes": "Fabricated test approval only; no actual data rights.",
                    **source_changes,
                }
            ],
        }
    )


class Clock:
    elapsed: float = 0

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return 1000 + self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


def _token(**changes: object) -> dict[str, object]:
    return {
        "access_token": "fixture-token",
        "token_type": "Bearer",
        "expires_in": 86400,
        "access_token_token_expired": "2026-10-02 09:00:00",
        **changes,
    }


def _prices(**changes: object) -> dict[str, object]:
    return {
        "rt_cd": "0",
        "output1": {"stck_shrn_iscd": "005930"},
        "output2": [
            {
                "stck_bsop_date": "20260930",
                "stck_oprc": "100",
                "stck_hgpr": "110",
                "stck_lwpr": "90",
                "stck_clpr": "105",
                "acml_vol": "1000",
                "flng_cls_code": "",
                "prtt_rate": "0",
                "revl_issu_reas": "00",
                "mod_yn": "N",
            }
        ],
        **changes,
    }


class ProbedClient(KisQuoteClient):
    def probe_route(self, method: str, path: str) -> None:
        self._send(method, path, rate=30, headers={})


def _client(
    requests: list[httpx.Request],
    clock: Clock,
    *,
    token: dict[str, object] | None = None,
    prices: dict[str, object] | None = None,
) -> ProbedClient:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "openapi.koreainvestment.com"
        assert request.url.port == 9443
        assert request.url.scheme == "https"
        if request.url.path == TOKEN_PATH:
            assert request.method == "POST"
            return httpx.Response(200, json=_token() if token is None else token)
        assert request.url.path == DAILY_PATH
        assert request.method == "GET"
        return httpx.Response(200, json=_prices() if prices is None else prices)

    return ProbedClient(
        CREDENTIALS,
        _transport=httpx.MockTransport(handle),
        _clock=clock.now,
        _monotonic=clock.monotonic,
        _sleep=clock.advance,
    )


def test_quotes_only_requests_cache_token_and_preserve_bytes() -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    with _client(requests, clock) as client:
        receipt = client.daily_prices(REQUEST, source_policy=_policy())
        client.daily_prices(REQUEST, source_policy=_policy())
    assert [request.url.path for request in requests] == [
        TOKEN_PATH,
        DAILY_PATH,
        DAILY_PATH,
    ]
    assert json.loads(requests[0].content) == {
        "grant_type": "client_credentials",
        "appkey": "fixture-app-key",
        "appsecret": "fixture-secret",
    }
    assert requests[1].headers["tr_id"] == "FHKST03010100"
    assert requests[1].headers["authorization"] == "Bearer fixture-token"
    assert requests[1].headers["custtype"] == "P"
    assert requests[1].url.params["FID_ORG_ADJ_PRC"] == "1"
    assert requests[1].url.params["FID_COND_MRKT_DIV_CODE"] == "J"
    assert requests[1].url.params["FID_PERIOD_DIV_CODE"] == "D"
    assert all("fixture-secret" not in str(request.url) for request in requests)
    assert receipt.raw_payload == httpx.Response(200, json=_prices()).content
    assert receipt.policy_digest == evidence_digest(_policy())
    assert receipt.observed_at == NOW + timedelta(seconds=2)
    assert receipt.row_count == 1
    assert not receipt.coverage_verified
    assert receipt.raw_payload_digest.startswith("sha256:")
    assert clock.elapsed == 4
    assert "fixture-secret" not in repr(CREDENTIALS)
    assert "stck_clpr" not in repr(receipt)


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"source_id": "another-source"},
        {"category": "NEWS"},
        {"rights": {"classification": "LICENSED"}},
        {
            "rights": {
                "classification": "LICENSED",
                "retention_days": 1,
                "redistribution_allowed": True,
            }
        },
        {"rights": {"classification": "APPROVED_PUBLIC", "retention_days": 1}},
    ],
)
def test_unapproved_source_has_no_network(changes: dict[str, object]) -> None:
    requests: list[httpx.Request] = []
    with (
        _client(requests, Clock()) as client,
        pytest.raises(KisQuoteError, match="approved private-use"),
    ):
        client.daily_prices(REQUEST, source_policy=_policy(**changes))
    assert not requests


def test_repository_draft_policy_blocks_network() -> None:
    requests: list[httpx.Request] = []
    policy = load_policy(Path("config/source-allowlist.yaml"), SourceAllowlist)
    with _client(requests, Clock()) as client, pytest.raises(KisQuoteError):
        client.daily_prices(REQUEST, source_policy=policy)
    assert not requests


def test_future_approval_blocks_network() -> None:
    requests: list[httpx.Request] = []
    policy = _policy()
    policy = policy.model_copy(
        update={
            "metadata": policy.metadata.model_copy(
                update={"approved_at": NOW + timedelta(seconds=1)}
            )
        }
    )
    with _client(requests, Clock()) as client, pytest.raises(KisQuoteError):
        client.daily_prices(REQUEST, source_policy=policy)
    assert not requests


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/uapi/domestic-stock/v1/trading/order-cash"),
        ("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl"),
        ("GET", "/uapi/domestic-stock/v1/trading/inquire-balance"),
        ("POST", DAILY_PATH),
        ("GET", TOKEN_PATH),
        ("GET", "https://example.invalid" + DAILY_PATH),
        ("GET", DAILY_PATH + "/../trading/order-cash"),
        ("GET", DAILY_PATH + "?redirect=evil"),
    ],
)
def test_unapproved_method_path_never_reaches_transport(method: str, path: str) -> None:
    requests: list[httpx.Request] = []
    with (
        _client(requests, Clock()) as client,
        pytest.raises(KisQuoteError, match="not permitted"),
    ):
        client.probe_route(method, path)
    assert not requests


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 429, 500])
def test_http_errors_do_not_redirect_retry_or_disclose_body(status: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://example.invalid"},
            text="fixture-secret fixture-token",
        )

    with (
        KisQuoteClient(
            CREDENTIALS, _transport=httpx.MockTransport(handle), _clock=Clock().now
        ) as client,
        pytest.raises(KisQuoteError) as failure,
    ):
        client.daily_prices(REQUEST, source_policy=_policy())
    assert len(requests) == 1
    assert "fixture" not in str(failure.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"access_token": "bad\r\nheader"},
        {"token_type": "Basic"},
        {"expires_in": True},
        {"expires_in": 0},
        {"access_token_token_expired": "2026-10-01 08:59:59"},
        {"access_token_token_expired": "not-a-date"},
    ],
)
def test_invalid_tokens_block_quotation(changes: dict[str, object]) -> None:
    requests: list[httpx.Request] = []
    with (
        _client(requests, Clock(), token=_token(**changes)) as client,
        pytest.raises(KisQuoteError, match="Invalid KIS token"),
    ):
        client.daily_prices(REQUEST, source_policy=_policy())
    assert len(requests) == 1


def test_reused_token_absolute_expiry_is_not_extended() -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    token = _token(access_token_token_expired="2026-10-01 09:05:00")
    with _client(requests, clock, token=token) as client:
        client.daily_prices(REQUEST, source_policy=_policy())
        clock.advance(300)
        with pytest.raises(KisQuoteError, match="Invalid KIS token"):
            client.daily_prices(REQUEST, source_policy=_policy())
    assert [request.url.path for request in requests] == [
        TOKEN_PATH,
        DAILY_PATH,
        TOKEN_PATH,
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"rt_cd": "1", "msg1": "fixture-secret"},
        {"output1": {"stck_shrn_iscd": "000660"}},
        {"output2": [{"stck_bsop_date": "20261001"}]},
        {"output2": [{"stck_bsop_date": "20260930"}] * 2},
        {"output2": [{"stck_bsop_date": "20260930"}] * 101},
        {"output2": [{"unexpected": "fixture-secret"}]},
    ],
)
def test_invalid_quotes_fail_without_echoing_response(
    changes: dict[str, object],
) -> None:
    requests: list[httpx.Request] = []
    with (
        _client(requests, Clock(), prices=_prices(**changes)) as client,
        pytest.raises(KisQuoteError, match="Invalid KIS daily") as failure,
    ):
        client.daily_prices(REQUEST, source_policy=_policy())
    assert "fixture-secret" not in str(failure.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "../../order-cash"},
        {"start": "2026-10-01"},
        {"start": "2026-01-01"},
    ],
)
def test_invalid_request(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KisDailyRequest.model_validate({**REQUEST.model_dump(), **changes})


def test_today_is_not_a_completed_session() -> None:
    requests: list[httpx.Request] = []
    with (
        _client(requests, Clock()) as client,
        pytest.raises(KisQuoteError, match="past sessions"),
    ):
        client.daily_prices(
            REQUEST.model_copy(update={"end": date(2026, 10, 1)}),
            source_policy=_policy(),
        )
    assert not requests


def test_cli_defaults_to_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["--symbol", "005930", "--start", "2026-09-01", "--end", "2026-09-30"])
        == 0
    )
    assert "Network disabled" in capsys.readouterr().out


def test_cli_draft_policy_fails_before_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "--symbol",
                "005930",
                "--start",
                "2026-09-01",
                "--end",
                "2026-09-30",
                "--allow-network",
            ]
        )
        == 1
    )
    assert "KIS quote check failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "response_kind", ["oversized", "html", "broken-json", "encoded"]
)
def test_response_bounds_before_receipt(response_kind: str) -> None:
    requests: list[httpx.Request] = []
    clock = Clock()

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json=_token())
        if response_kind == "oversized":
            return httpx.Response(
                200,
                content=b" " * (2 * 1024 * 1024 + 1),
                headers={"content-type": "application/json"},
            )
        if response_kind == "html":
            return httpx.Response(
                200, text="fixture-secret", headers={"content-type": "text/html"}
            )
        if response_kind == "encoded":
            return httpx.Response(
                200,
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "content-encoding": "unsupported",
                },
            )
        return httpx.Response(
            200, content=b"fixture-secret", headers={"content-type": "application/json"}
        )

    with (
        KisQuoteClient(
            CREDENTIALS,
            _transport=httpx.MockTransport(handle),
            _clock=clock.now,
            _monotonic=clock.monotonic,
            _sleep=clock.advance,
        ) as client,
        pytest.raises(KisQuoteError) as failure,
    ):
        client.daily_prices(REQUEST, source_policy=_policy())
    assert len(requests) == 2
    assert "fixture-secret" not in str(failure.value)


def test_timeout_is_sanitized_and_not_retried() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("fixture-secret fixture-token", request=request)

    with (
        KisQuoteClient(
            CREDENTIALS, _transport=httpx.MockTransport(handle), _clock=Clock().now
        ) as client,
        pytest.raises(KisQuoteError) as failure,
    ):
        client.daily_prices(REQUEST, source_policy=_policy())
    assert len(requests) == 1
    rendered = "".join(traceback.format_exception(failure.value))
    assert "fixture-secret" not in rendered
    assert "fixture-token" not in rendered


def test_token_issuance_failure_has_cooldown() -> None:
    requests: list[httpx.Request] = []
    with _client(requests, Clock(), token={}) as client:
        with pytest.raises(KisQuoteError, match="Invalid KIS token"):
            client.daily_prices(REQUEST, source_policy=_policy())
        with pytest.raises(KisQuoteError, match="cooldown"):
            client.daily_prices(REQUEST, source_policy=_policy())
    assert len(requests) == 1


def test_updated_source_policy_blocks_even_with_cached_token() -> None:
    requests: list[httpx.Request] = []
    with _client(requests, Clock()) as client:
        client.daily_prices(REQUEST, source_policy=_policy())
        with pytest.raises(KisQuoteError):
            client.daily_prices(REQUEST, source_policy=_policy(enabled=False))
    assert len(requests) == 2


def test_revalidate_forged_policy_and_request() -> None:
    requests: list[httpx.Request] = []
    policy = _policy()
    forged = policy.model_copy(
        update={
            "sources": (
                policy.sources[0].model_copy(update={"rate_limit_per_minute": 0}),
            )
        }
    )
    with _client(requests, Clock()) as client:
        with pytest.raises(KisQuoteError, match="Invalid source policy"):
            client.daily_prices(REQUEST, source_policy=forged)
        with pytest.raises(ValidationError):
            client.daily_prices(
                REQUEST.model_copy(update={"symbol": "../order-cash"}),
                source_policy=policy,
            )
    assert not requests


def test_empty_response_makes_no_coverage_claim() -> None:
    requests: list[httpx.Request] = []
    with _client(requests, Clock(), prices=_prices(output2=[])) as client:
        receipt = client.daily_prices(REQUEST, source_policy=_policy())
    assert receipt.row_count == 0
    assert not receipt.coverage_verified


def test_closed_client_cannot_fetch() -> None:
    requests: list[httpx.Request] = []
    client = _client(requests, Clock())
    client.close()
    with pytest.raises(KisQuoteError, match="closed"):
        client.daily_prices(REQUEST, source_policy=_policy())
    assert not requests


@pytest.mark.parametrize("secret", ["", "bad\nheader", "bad secret", "x" * 4097])
def test_credentials_reject_invalid_header_values(secret: str) -> None:
    with pytest.raises(KisQuoteError, match="Invalid KIS credentials"):
        KisCredentials(SecretStr("fixture-key"), SecretStr(secret))


def test_credentials_are_loaded_only_from_quote_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIS_QUOTE_APP_KEY", "fixture-app-key")
    monkeypatch.setenv("KIS_QUOTE_APP_SECRET", "fixture-secret")
    assert KisCredentials.from_environment() == CREDENTIALS
    monkeypatch.delenv("KIS_QUOTE_APP_SECRET")
    with pytest.raises(KisQuoteError):
        KisCredentials.from_environment()


def test_environment_proxy_and_ca_settings_are_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:1")
    monkeypatch.setenv("SSL_CERT_FILE", "nonexistent-fixture-ca.pem")
    requests: list[httpx.Request] = []
    with _client(requests, Clock()) as client:
        client.daily_prices(REQUEST, source_policy=_policy())
    assert len(requests) == 2


def test_cli_does_not_read_credentials_when_offline_or_policy_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> KisCredentials:
        pytest.fail("Credentials must not be read before the source-policy gate")

    monkeypatch.setattr(KisCredentials, "from_environment", forbidden)
    arguments = ["--symbol", "005930", "--start", "2026-09-01", "--end", "2026-09-30"]
    assert main(arguments) == 0
    assert main([*arguments, "--allow-network"]) == 1


def test_slow_small_chunks_exhaust_response_budget() -> None:
    clock = Clock()
    requests: list[httpx.Request] = []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for _index in range(5):
                clock.advance(11)
                yield b" "

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json=_token())
        return httpx.Response(
            200, stream=SlowStream(), headers={"content-type": "application/json"}
        )

    with (
        KisQuoteClient(
            CREDENTIALS,
            _transport=httpx.MockTransport(handle),
            _clock=clock.now,
            _monotonic=clock.monotonic,
            _sleep=clock.advance,
        ) as client,
        pytest.raises(KisQuoteError, match="collection bounds"),
    ):
        client.daily_prices(REQUEST, source_policy=_policy())
    assert len(requests) == 2
    assert clock.elapsed == 35


def test_mock_quote_receipt_normalizes_without_another_request() -> None:
    requests: list[httpx.Request] = []
    with _client(requests, Clock()) as client:
        receipt = client.daily_prices(REQUEST, source_policy=_policy())
    close = datetime(2026, 9, 30, 6, 30, tzinfo=UTC)
    result = normalize_kis_daily_prices(receipt, session_closes={close.date(): close})
    assert len(requests) == 2
    assert len(result.bars) == 1
    assert result.raw_payload_digest == receipt.raw_payload_digest
    assert result.bars[0].price.volume == 1000
    assert not result.bars[0].requires_review
