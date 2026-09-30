from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ats.domain.policy import (
    PolicyMetadata,
    PolicyStatus,
    PromotionGates,
    PromotionPolicy,
    RiskPolicy,
    SourceAllowlist,
    SourceEntry,
    SourceReviewState,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[3]


def test_repository_policies_are_valid_and_pending_operator_approval() -> None:
    risk = load_policy(ROOT / "config" / "risk-policy.yaml", RiskPolicy)
    promotion = load_policy(
        ROOT / "config" / "promotion-policy.yaml",
        PromotionPolicy,
    )
    sources = load_policy(
        ROOT / "config" / "source-allowlist.yaml",
        SourceAllowlist,
    )

    assert risk.metadata.status is PolicyStatus.DRAFT
    assert risk.limits.max_symbol_weight == 0.10
    assert risk.limits.daily_portfolio_loss_halt == 0.01
    assert risk.limits.portfolio_drawdown_halt == 0.15
    assert risk.agent_editable is False

    assert promotion.metadata.status is PolicyStatus.DRAFT
    assert promotion.gates.min_paper_sessions == 20
    assert promotion.gates.require_human_approval is True
    assert promotion.agent_editable is False

    assert sources.default_action == "DENY"
    assert sources.private_or_mnpi_sources_allowed is False
    assert all(not source.enabled for source in sources.sources)


def test_enabled_source_requires_legal_approval() -> None:
    document = load_policy(
        ROOT / "config" / "source-allowlist.yaml",
        SourceAllowlist,
    )
    pending_source = document.sources[0]

    with pytest.raises(ValidationError, match="approved legal review"):
        SourceEntry.model_validate(
            {
                **pending_source.model_dump(mode="python"),
                "enabled": True,
                "legal_review": SourceReviewState.PENDING,
            }
        )


def test_policy_approval_requires_human_identity_and_timestamp() -> None:
    with pytest.raises(ValidationError, match="approver identity"):
        PolicyMetadata(
            policy_id="paper-risk-policy",
            version="1.0.0",
            status=PolicyStatus.APPROVED,
        )

    metadata = PolicyMetadata(
        policy_id="paper-risk-policy",
        version="1.0.0",
        status=PolicyStatus.APPROVED,
        approved_by="risk-committee",
        approved_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert metadata.status is PolicyStatus.APPROVED


def test_promotion_positive_folds_cannot_exceed_total() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        PromotionGates(
            min_net_oos_sharpe=0.8,
            max_drawdown=0.15,
            walk_forward_folds=5,
            min_positive_excess_folds=6,
            min_deflated_sharpe_confidence=0.95,
            min_paper_sessions=20,
        )
