"""Immutable strategy contracts and governed lifecycle rules."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StringConstraints,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
        strip_whitespace=True,
    ),
]
VersionLabel = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        strip_whitespace=True,
    ),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"),
]
Sha256Digest = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]
ParameterPath = Annotated[
    str,
    StringConstraints(pattern=r"^signal\.[a-z][a-z0-9_]{1,63}$"),
]
NonEmptyText = Annotated[
    str,
    StringConstraints(min_length=3, max_length=2_000, strip_whitespace=True),
]
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeFiniteFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class FrozenModel(BaseModel):
    """Base model that rejects extra fields and assignment."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"


class StrategyFamily(StrEnum):
    MOMENTUM = "MOMENTUM"
    TREND = "TREND"
    MEAN_REVERSION = "MEAN_REVERSION"
    MODEL = "MODEL"


class PortfolioMethod(StrEnum):
    EQUAL_WEIGHT = "EQUAL_WEIGHT"
    SCORE_WEIGHTED = "SCORE_WEIGHTED"
    VOLATILITY_TARGETED = "VOLATILITY_TARGETED"


class LifecycleState(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    BACKTESTED = "BACKTESTED"
    PAPER = "PAPER"
    CHAMPION = "CHAMPION"
    RETIRED = "RETIRED"
    QUARANTINED = "QUARANTINED"


class LifecycleActor(StrEnum):
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


class ArtifactRef(FrozenModel):
    artifact_id: Identifier
    version: VersionLabel
    digest: Sha256Digest


class StrategyVersion(FrozenModel):
    strategy_id: UUID
    version_id: UUID
    semantic_version: SemanticVersion
    parent_version_id: UUID | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def reject_self_parent(self) -> Self:
        if self.parent_version_id == self.version_id:
            raise ValueError("a strategy version cannot be its own parent")
        return self


class UniverseSpec(FrozenModel):
    market: Literal["KRX"] = "KRX"
    base_index: Literal["KOSPI_200"] = "KOSPI_200"
    asset_classes: tuple[AssetClass, ...] = (
        AssetClass.EQUITY,
        AssetClass.ETF,
    )
    membership_snapshot_id: Identifier
    etf_allowlist_id: Identifier

    @model_validator(mode="after")
    def require_unique_asset_classes(self) -> Self:
        if len(set(self.asset_classes)) != len(self.asset_classes):
            raise ValueError("asset classes must be unique")
        return self


class ScheduleSpec(FrozenModel):
    timezone: Literal["Asia/Seoul"] = "Asia/Seoul"
    decision_phase: Literal["AFTER_CLOSE"] = "AFTER_CLOSE"
    execution_phase: Literal["NEXT_SESSION_OPEN"] = "NEXT_SESSION_OPEN"


class SignalParameter(FrozenModel):
    name: ParameterPath
    value: FiniteFloat


class ParameterBounds(FrozenModel):
    name: ParameterPath
    minimum: FiniteFloat
    maximum: FiniteFloat
    step: PositiveFiniteFloat

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("minimum cannot exceed maximum")
        if not self.permits(self.minimum) or not self.permits(self.maximum):
            raise ValueError("minimum and maximum must align to step")
        return self

    def permits(self, value: float) -> bool:
        if not self.minimum <= value <= self.maximum:
            return False
        step_count = (value - self.minimum) / self.step
        return math.isclose(
            step_count,
            round(step_count),
            rel_tol=0.0,
            abs_tol=1e-9,
        )


class MutationPolicy(FrozenModel):
    allowed_parameters: tuple[ParameterBounds, ...] = ()
    max_parameter_changes: Annotated[int, Field(ge=1, le=20)] = 3

    @model_validator(mode="after")
    def require_unique_parameters(self) -> Self:
        names = [parameter.name for parameter in self.allowed_parameters]
        if len(names) != len(set(names)):
            raise ValueError("mutable parameter names must be unique")
        return self


class ParameterMutation(FrozenModel):
    name: ParameterPath
    previous_value: FiniteFloat
    new_value: FiniteFloat

    @model_validator(mode="after")
    def reject_noop(self) -> Self:
        if math.isclose(
            self.previous_value,
            self.new_value,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("a mutation must change the parameter value")
        return self


class SignalSpec(FrozenModel):
    family: StrategyFamily
    implementation: ArtifactRef
    parameters: tuple[SignalParameter, ...]

    @model_validator(mode="after")
    def require_unique_parameters(self) -> Self:
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("signal parameter names must be unique")
        return self


class PortfolioSpec(FrozenModel):
    method: PortfolioMethod
    max_positions: Annotated[int, Field(ge=1, le=200)]
    cash_buffer_bps: Annotated[int, Field(ge=0, le=10_000)] = 0
    turnover_budget: Annotated[float, Field(gt=0, le=2, allow_inf_nan=False)] = 1


class PolicyRef(FrozenModel):
    policy_id: Identifier
    version: VersionLabel
    digest: Sha256Digest


class DatasetSnapshotRef(FrozenModel):
    snapshot_id: Identifier
    observed_through: AwareDatetime
    digest: Sha256Digest


class StrategySpec(FrozenModel):
    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["Strategy"] = "Strategy"
    name: Annotated[str, StringConstraints(min_length=3, max_length=120)]
    version: StrategyVersion
    research_hypothesis: NonEmptyText
    universe: UniverseSpec
    schedule: ScheduleSpec = ScheduleSpec()
    features: tuple[ArtifactRef, ...] = ()
    model: ArtifactRef | None = None
    prompt_versions: tuple[ArtifactRef, ...] = ()
    signal: SignalSpec
    portfolio: PortfolioSpec
    risk_policy: PolicyRef
    mutation_policy: MutationPolicy
    mutations: tuple[ParameterMutation, ...] = ()
    dataset_snapshot: DatasetSnapshotRef
    code_artifact: ArtifactRef
    container_image_digest: Sha256Digest
    provenance_hash: Sha256Digest

    @model_validator(mode="after")
    def validate_strategy_boundaries(self) -> Self:
        parameter_by_name = {
            parameter.name: parameter for parameter in self.signal.parameters
        }
        bounds_by_name = {
            bounds.name: bounds for bounds in self.mutation_policy.allowed_parameters
        }

        unknown_bounds = set(bounds_by_name).difference(parameter_by_name)
        if unknown_bounds:
            names = ", ".join(sorted(unknown_bounds))
            raise ValueError(f"mutation policy references unknown parameters: {names}")

        for name, bounds in bounds_by_name.items():
            if not bounds.permits(parameter_by_name[name].value):
                raise ValueError(f"current value for {name} violates mutation bounds")

        mutation_names = [mutation.name for mutation in self.mutations]
        if len(mutation_names) != len(set(mutation_names)):
            raise ValueError("mutation records must have unique parameter names")
        if self.version.parent_version_id is None and self.mutations:
            raise ValueError("a root strategy cannot contain mutation records")

        for mutation in self.mutations:
            if mutation.name not in bounds_by_name:
                raise ValueError(f"mutation is not permitted: {mutation.name}")
            if not bounds_by_name[mutation.name].permits(mutation.new_value):
                raise ValueError(
                    f"mutation is outside declared bounds: {mutation.name}"
                )
            if not math.isclose(
                parameter_by_name[mutation.name].value,
                mutation.new_value,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError(
                    f"mutation record does not match current value: {mutation.name}"
                )

        if len(self.mutations) > self.mutation_policy.max_parameter_changes:
            raise ValueError("mutation count exceeds the declared budget")
        if self.signal.family is StrategyFamily.MODEL and self.model is None:
            raise ValueError("model strategies require a model artifact")
        return self

    def content_digest(self) -> str:
        payload = self.model_dump(mode="json")
        canonical_json = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


_ALLOWED_LIFECYCLE_TRANSITIONS: Mapping[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.DRAFT: frozenset(
        {LifecycleState.VALIDATED, LifecycleState.QUARANTINED}
    ),
    LifecycleState.VALIDATED: frozenset(
        {LifecycleState.BACKTESTED, LifecycleState.QUARANTINED}
    ),
    LifecycleState.BACKTESTED: frozenset(
        {LifecycleState.PAPER, LifecycleState.QUARANTINED}
    ),
    LifecycleState.PAPER: frozenset(
        {
            LifecycleState.CHAMPION,
            LifecycleState.RETIRED,
            LifecycleState.QUARANTINED,
        }
    ),
    LifecycleState.CHAMPION: frozenset(
        {LifecycleState.RETIRED, LifecycleState.QUARANTINED}
    ),
    LifecycleState.RETIRED: frozenset(),
    LifecycleState.QUARANTINED: frozenset(),
}


def is_lifecycle_transition_allowed(
    from_state: LifecycleState | None,
    to_state: LifecycleState,
) -> bool:
    if from_state is None:
        return to_state is LifecycleState.DRAFT
    return to_state in _ALLOWED_LIFECYCLE_TRANSITIONS[from_state]


class StrategyLifecycleEvent(FrozenModel):
    event_id: UUID
    strategy_version_id: UUID
    from_state: LifecycleState | None
    to_state: LifecycleState
    actor: LifecycleActor
    occurred_at: AwareDatetime
    evidence: tuple[ArtifactRef, ...] = ()
    approval_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        if not is_lifecycle_transition_allowed(self.from_state, self.to_state):
            raise ValueError(
                f"lifecycle transition is not allowed: "
                f"{self.from_state} -> {self.to_state}"
            )
        if self.from_state is not None and not self.evidence:
            raise ValueError("state transitions require evidence")
        if self.to_state is LifecycleState.CHAMPION and (
            self.actor is not LifecycleActor.HUMAN or self.approval_id is None
        ):
            raise ValueError("champion promotion requires explicit human approval")
        return self


def _numeric_mutation_value(raw_value: object, parameter_name: str) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise TypeError(f"mutation must be numeric: {parameter_name}")
    return float(raw_value)


def create_challenger(
    parent: StrategySpec,
    version: StrategyVersion,
    mutations: Mapping[str, int | float],
    *,
    hypothesis: str,
    provenance_hash: str,
) -> StrategySpec:
    """Create a validated immutable descendant through declared parameter changes."""

    if version.strategy_id != parent.version.strategy_id:
        raise ValueError("challenger must remain in the parent's strategy lineage")
    if version.parent_version_id != parent.version.version_id:
        raise ValueError("challenger must reference the exact parent version")
    if version.version_id == parent.version.version_id:
        raise ValueError("challenger requires a new version id")
    if not mutations:
        raise ValueError("challenger requires at least one parameter mutation")
    if len(mutations) > parent.mutation_policy.max_parameter_changes:
        raise ValueError("mutation count exceeds the declared budget")

    bounds_by_name = {
        bounds.name: bounds for bounds in parent.mutation_policy.allowed_parameters
    }
    unknown = set(mutations).difference(bounds_by_name)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"mutation is not permitted: {names}")

    updated_parameters: list[SignalParameter] = []
    mutation_records: list[ParameterMutation] = []
    for parameter in parent.signal.parameters:
        if parameter.name not in mutations:
            updated_parameters.append(parameter)
            continue

        new_value = _numeric_mutation_value(
            mutations[parameter.name],
            parameter.name,
        )
        bounds = bounds_by_name[parameter.name]
        if not bounds.permits(new_value):
            raise ValueError(f"mutation is outside declared bounds: {parameter.name}")

        mutation_records.append(
            ParameterMutation(
                name=parameter.name,
                previous_value=parameter.value,
                new_value=new_value,
            )
        )
        updated_parameters.append(SignalParameter(name=parameter.name, value=new_value))

    signal = parent.signal.model_copy(update={"parameters": tuple(updated_parameters)})
    payload = parent.model_dump(mode="python")
    payload.update(
        version=version,
        research_hypothesis=hypothesis,
        signal=signal,
        mutations=tuple(mutation_records),
        provenance_hash=provenance_hash,
    )
    return StrategySpec.model_validate(payload)
