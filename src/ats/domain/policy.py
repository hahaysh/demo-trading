"""Typed, operator-owned policy contracts."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, TypeVar, cast

import yaml
from pydantic import AwareDatetime, Field, model_validator

from ats.domain.strategy import (
    AssetClass,
    FrozenModel,
    Identifier,
    NonEmptyText,
    VersionLabel,
)


class PolicyStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


class SourceReviewState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RightsClass(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED_PUBLIC = "APPROVED_PUBLIC"
    LICENSED = "LICENSED"


class SourceCategory(StrEnum):
    MARKET = "MARKET"
    DISCLOSURE = "DISCLOSURE"
    EXCHANGE = "EXCHANGE"
    NEWS = "NEWS"
    PUBLIC_CHANNEL = "PUBLIC_CHANNEL"
    PUBLIC_COMMUNITY = "PUBLIC_COMMUNITY"


class PolicyMetadata(FrozenModel):
    policy_id: Identifier
    version: VersionLabel
    status: PolicyStatus = PolicyStatus.DRAFT
    owner: Literal["operator"] = "operator"
    approved_by: NonEmptyText | None = None
    approved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def require_complete_approval(self) -> Self:
        has_approval = self.approved_by is not None or self.approved_at is not None
        if self.status is PolicyStatus.APPROVED:
            if self.approved_by is None or self.approved_at is None:
                raise ValueError(
                    "approved policies require approver identity and timestamp"
                )
        elif has_approval:
            raise ValueError("only approved policies may contain approval metadata")
        return self


class RiskScope(FrozenModel):
    market: Literal["KRX"] = "KRX"
    paper_only: Literal[True] = True
    long_only: Literal[True] = True
    allowed_asset_classes: tuple[AssetClass, ...] = (
        AssetClass.EQUITY,
        AssetClass.ETF,
    )


class RiskLimits(FrozenModel):
    max_symbol_weight: Annotated[float, Field(gt=0, le=1)]
    max_gross_exposure: Annotated[float, Field(gt=0, le=1)]
    daily_portfolio_loss_halt: Annotated[float, Field(gt=0, le=1)]
    portfolio_drawdown_halt: Annotated[float, Field(gt=0, le=1)]
    max_price_age_seconds: Annotated[int, Field(ge=1, le=86_400)]


class KillSwitchPolicy(FrozenModel):
    fail_closed: Literal[True] = True
    reject_orders_while_active: Literal[True] = True
    human_reset_required: Literal[True] = True


class RiskPolicy(FrozenModel):
    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["RiskPolicy"] = "RiskPolicy"
    metadata: PolicyMetadata
    scope: RiskScope = RiskScope()
    limits: RiskLimits
    kill_switch: KillSwitchPolicy = KillSwitchPolicy()
    agent_editable: Literal[False] = False


class PromotionGates(FrozenModel):
    min_net_oos_sharpe: Annotated[float, Field(ge=0)]
    max_drawdown: Annotated[float, Field(gt=0, le=1)]
    walk_forward_folds: Annotated[int, Field(ge=2, le=20)]
    min_positive_excess_folds: Annotated[int, Field(ge=1, le=20)]
    min_deflated_sharpe_confidence: Annotated[float, Field(gt=0, le=1)]
    require_dual_engine_reproducibility: Literal[True] = True
    allow_data_integrity_violations: Literal[False] = False
    min_paper_sessions: Annotated[int, Field(ge=1)]
    max_hard_risk_breaches: Literal[0] = 0
    require_human_approval: Literal[True] = True

    @model_validator(mode="after")
    def validate_fold_threshold(self) -> Self:
        if self.min_positive_excess_folds > self.walk_forward_folds:
            raise ValueError("positive folds cannot exceed total folds")
        return self


class PromotionPolicy(FrozenModel):
    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["PromotionPolicy"] = "PromotionPolicy"
    metadata: PolicyMetadata
    gates: PromotionGates
    agent_editable: Literal[False] = False


class SourceRights(FrozenModel):
    classification: RightsClass
    retention_days: Annotated[int, Field(ge=1)] | None = None
    redistribution_allowed: bool = False


class SourceEntry(FrozenModel):
    source_id: Identifier
    category: SourceCategory
    enabled: bool = False
    legal_review: SourceReviewState = SourceReviewState.PENDING
    rights: SourceRights
    rate_limit_per_minute: Annotated[int, Field(ge=1)] | None = None
    notes: NonEmptyText

    @model_validator(mode="after")
    def block_unapproved_source(self) -> Self:
        if self.enabled:
            if self.legal_review is not SourceReviewState.APPROVED:
                raise ValueError("enabled sources require approved legal review")
            if self.rights.classification is RightsClass.PENDING_REVIEW:
                raise ValueError("enabled sources require resolved usage rights")
            if self.rate_limit_per_minute is None:
                raise ValueError("enabled sources require an explicit rate limit")
        return self


class SourceAllowlist(FrozenModel):
    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["SourceAllowlist"] = "SourceAllowlist"
    metadata: PolicyMetadata
    default_action: Literal["DENY"] = "DENY"
    private_or_mnpi_sources_allowed: Literal[False] = False
    sources: tuple[SourceEntry, ...]
    agent_editable: Literal[False] = False

    @model_validator(mode="after")
    def require_unique_sources(self) -> Self:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source ids must be unique")
        return self


PolicyModel = TypeVar("PolicyModel", bound=FrozenModel)


def load_policy(path: Path, model_type: type[PolicyModel]) -> PolicyModel:
    raw_data = cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))
    if not isinstance(raw_data, dict):
        raise ValueError(f"policy document must be a mapping: {path}")
    return model_type.model_validate(raw_data)
