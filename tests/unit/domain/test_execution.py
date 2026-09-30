from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from ats.domain.execution import OrderIntent, RiskCheck, RiskDecision
from ats.domain.governance import evidence_digest
from ats.domain.policy import RiskPolicy
from ats.domain.strategy import ArtifactRef

CREATED = datetime(2026, 9, 30, 7, tzinfo=UTC)
CHECKED = CREATED + timedelta(minutes=1)
EXPIRY = CREATED + timedelta(minutes=5)
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
ARTIFACT = ArtifactRef(artifact_id="test-evidence", version="1.0.0", digest=DIGEST)


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
