"""Deterministic local input replay; no strategy execution or broker actions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ats.data.artifacts import LocalArtifactResolver
from ats.data.asof import RevisionOrder
from ats.data.bundle import (
    DecisionInputBundle,
    UnscopedRecordPolicy,
    build_decision_inputs,
)
from ats.data.requirements import SourceDataRequirement
from ats.domain.data import DataSnapshot
from ats.domain.governance import evidence_digest
from ats.domain.policy import PolicyStatus, SourceAllowlist
from ats.domain.strategy import FrozenModel, Sha256Digest


class InputReplayRequest(FrozenModel):
    snapshot: DataSnapshot
    source_policy: SourceAllowlist
    cutoffs: Annotated[tuple[AwareDatetime, ...], Field(min_length=1)]
    data_requirements: tuple[SourceDataRequirement, ...]
    revision_orders: tuple[RevisionOrder, ...] = ()
    unscoped_policy: UnscopedRecordPolicy = UnscopedRecordPolicy.EXCLUDE

    @field_validator("cutoffs")
    @classmethod
    def normalize_cutoffs(cls, values: tuple[datetime, ...]) -> tuple[datetime, ...]:
        return tuple(value.astimezone(UTC) for value in values)

    @model_validator(mode="after")
    def validate_replay(self) -> Self:
        if any(
            previous >= current
            for previous, current in zip(self.cutoffs, self.cutoffs[1:], strict=False)
        ):
            raise ValueError("replay cutoffs must be strictly increasing")
        horizon = min(
            self.snapshot.observed_through.astimezone(UTC),
            self.snapshot.universe_membership.as_of.astimezone(UTC),
        )
        if self.cutoffs[-1] > horizon:
            raise ValueError("replay cutoff exceeds input horizon")
        metadata = self.source_policy.metadata
        if (
            metadata.status is not PolicyStatus.APPROVED
            or metadata.approved_at is None
            or metadata.approved_at.astimezone(UTC) > self.cutoffs[0]
        ):
            raise ValueError("replay source policy must be approved by first cutoff")
        ids = [item.requirement_id for item in self.data_requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("replay data requirement IDs must be unique")
        return self


class InputReplayResult(FrozenModel):
    request_digest: Sha256Digest
    bundles: Annotated[tuple[DecisionInputBundle, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_sequence(self) -> Self:
        first = self.bundles[0]
        for bundle in self.bundles:
            if (
                bundle.snapshot != first.snapshot
                or bundle.source_policy != first.source_policy
                or bundle.universe != first.universe
                or bundle.data_requirements != first.data_requirements
                or bundle.unscoped_policy != first.unscoped_policy
            ):
                raise ValueError(
                    "replay bundles must share pinned inputs and requirements"
                )
        if any(
            previous.cutoff.astimezone(UTC) >= current.cutoff.astimezone(UTC)
            for previous, current in zip(self.bundles, self.bundles[1:], strict=False)
        ):
            raise ValueError("replay bundle cutoffs must be strictly increasing")
        return self

    def content_digest(self) -> str:
        return evidence_digest(self)

    def validate_against_request(self, request: InputReplayRequest) -> None:
        """Check receipt completeness and declared provenance, not rerun artifact I/O."""
        request = InputReplayRequest.model_validate(request.model_dump())
        result = InputReplayResult.model_validate(self.model_dump())
        if result.request_digest != evidence_digest(request):
            raise ValueError("replay request digest mismatch")
        if (
            tuple(bundle.cutoff.astimezone(UTC) for bundle in result.bundles)
            != request.cutoffs
        ):
            raise ValueError("replay result does not cover the exact requested cutoffs")
        for bundle in result.bundles:
            if (
                bundle.snapshot.snapshot_id != request.snapshot.snapshot_id
                or bundle.snapshot.observed_through != request.snapshot.observed_through
                or bundle.snapshot.digest != request.snapshot.content_digest()
                or bundle.universe != request.snapshot.universe_membership
                or bundle.data_requirements != request.data_requirements
                or bundle.unscoped_policy != request.unscoped_policy
                or bundle.revision_orders
                != tuple(
                    sorted(
                        (
                            order
                            for order in request.revision_orders
                            if order.observed_at.astimezone(UTC) <= bundle.cutoff
                        ),
                        key=lambda order: (order.source_id, order.source_item_id),
                    )
                )
            ):
                raise ValueError("replay bundle does not match requested provenance")
            bundle.validate_source_policy(request.source_policy)


class InputReplayError(ValueError):
    """A cutoff failed; no partial replay result is published."""

    def __init__(self, cutoff: datetime, index: int) -> None:
        self.cutoff = cutoff
        self.index = index
        super().__init__(
            f"input replay failed at index {index}, cutoff {cutoff.isoformat()}"
        )


def replay_inputs(
    resolver: LocalArtifactResolver, request: InputReplayRequest
) -> InputReplayResult:
    """Build all bundles in memory, stopping at the first failure without fallback."""
    request = InputReplayRequest.model_validate(request.model_dump())
    bundles: list[DecisionInputBundle] = []
    for index, cutoff in enumerate(request.cutoffs):
        try:
            bundle = build_decision_inputs(
                resolver,
                request.snapshot,
                at=cutoff,
                source_policy=request.source_policy,
                data_requirements=request.data_requirements,
                revision_orders=request.revision_orders,
                unscoped_policy=request.unscoped_policy,
            )
        except (ValueError, OSError) as error:
            raise InputReplayError(cutoff, index) from error
        bundles.append(bundle)
    result = InputReplayResult(
        request_digest=evidence_digest(request), bundles=tuple(bundles)
    )
    result.validate_against_request(request)
    return result
