"""Local per-decision input composition, without engine or broker execution."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from ats.data.artifacts import LocalArtifactResolver
from ats.data.asof import RevisionOrder
from ats.data.requirements import SourceDataRequirement, validate_data_requirements
from ats.domain.data import DataSnapshot, PointInTimeRecord, UniverseMembershipManifest
from ats.domain.governance import evidence_digest
from ats.domain.policy import (
    PolicyStatus,
    RightsClass,
    SourceAllowlist,
    SourceEntry,
    SourceReviewState,
)
from ats.domain.strategy import DatasetSnapshotRef, FrozenModel, Identifier, PolicyRef
from ats.domain.universe import UniverseMember


class SourceEligibilityError(ValueError):
    """The supplied source policy does not permit use of an input."""


def _approved_sources(
    policy: SourceAllowlist, cutoff: datetime
) -> dict[str, SourceEntry]:
    if (
        policy.metadata.status is not PolicyStatus.APPROVED
        or policy.metadata.approved_at is None
        or policy.metadata.approved_at.astimezone(UTC) > cutoff
    ):
        raise SourceEligibilityError("source policy must be approved by the cutoff")
    return {source.source_id: source for source in policy.sources}


def _require_source(sources: dict[str, SourceEntry], source_id: str) -> SourceEntry:
    source = sources.get(source_id)
    if (
        source is None
        or not source.enabled
        or source.legal_review is not SourceReviewState.APPROVED
        or source.rights.classification is RightsClass.PENDING_REVIEW
    ):
        raise SourceEligibilityError(f"source is not eligible: {source_id}")
    return source


def _require_record(sources: dict[str, SourceEntry], record: PointInTimeRecord) -> None:
    source = _require_source(sources, record.source_id)
    if record.rights_class != source.rights.classification:
        raise SourceEligibilityError(
            f"record rights do not match source policy: {record.source_id}"
        )


class UnscopedRecordPolicy(StrEnum):
    EXCLUDE = "EXCLUDE"
    INCLUDE = "INCLUDE"


class ExcludedRecord(FrozenModel):
    source_id: Identifier
    source_item_id: Identifier
    revision: Identifier
    reason: Literal["OUTSIDE_UNIVERSE", "UNSCOPED"]


class DecisionInputBundle(FrozenModel):
    """In-process provenance receipt; construction alone does not verify bytes."""

    snapshot: DatasetSnapshotRef
    cutoff: AwareDatetime
    source_policy: PolicyRef
    data_requirements: tuple[SourceDataRequirement, ...] = ()
    universe: UniverseMembershipManifest
    members: tuple[UniverseMember, ...]
    revision_orders: tuple[RevisionOrder, ...]
    unscoped_policy: UnscopedRecordPolicy = UnscopedRecordPolicy.EXCLUDE
    records: tuple[PointInTimeRecord, ...]
    excluded: tuple[ExcludedRecord, ...]

    @model_validator(mode="after")
    def validate_cutoff_and_scope(self) -> Self:
        cutoff = self.cutoff.astimezone(UTC)
        if cutoff > min(
            self.snapshot.observed_through.astimezone(UTC),
            self.universe.as_of.astimezone(UTC),
        ):
            raise ValueError("bundle cutoff exceeds input horizon")
        member_ids = [member.instrument_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("bundle members must be unique")
        for member in self.members:
            if (
                member.observed_at.astimezone(UTC) > cutoff
                or member.effective_from.astimezone(UTC) > cutoff
                or (
                    member.effective_until is not None
                    and cutoff >= member.effective_until.astimezone(UTC)
                )
            ):
                raise ValueError("bundle membership is not known and effective")
        order_keys = [
            (order.source_id, order.source_item_id) for order in self.revision_orders
        ]
        if len(order_keys) != len(set(order_keys)):
            raise ValueError("bundle revision orders must be unique")
        if any(
            order.observed_at.astimezone(UTC) > cutoff for order in self.revision_orders
        ):
            raise ValueError("bundle revision order was not known at cutoff")
        keys = [(record.source_id, record.source_item_id) for record in self.records]
        keys += [(record.source_id, record.source_item_id) for record in self.excluded]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "bundle records and exclusions must have unique source items"
            )
        for record in self.records:
            if record.observed_at.astimezone(UTC) > cutoff:
                raise ValueError("bundle record was not known at cutoff")
            if record.instrument_id is None:
                if self.unscoped_policy is UnscopedRecordPolicy.EXCLUDE:
                    raise ValueError("bundle excludes unscoped records")
            elif record.instrument_id not in member_ids:
                raise ValueError("bundle record is outside universe")
        validate_data_requirements(self.data_requirements, self.records, at=self.cutoff)
        return self

    def content_digest(self) -> str:
        return evidence_digest(self)

    def validate_source_policy(self, policy: SourceAllowlist) -> None:
        """Resolve policy identity and receipt eligibility, not signatures or hidden history."""
        bundle = DecisionInputBundle.model_validate(self.model_dump())
        policy = SourceAllowlist.model_validate(policy.model_dump())
        if (
            bundle.source_policy.policy_id != policy.metadata.policy_id
            or bundle.source_policy.version != policy.metadata.version
            or bundle.source_policy.digest != evidence_digest(policy)
        ):
            raise SourceEligibilityError("bundle source policy reference mismatch")
        sources = _approved_sources(policy, bundle.cutoff.astimezone(UTC))
        for requirement in bundle.data_requirements:
            _require_source(sources, requirement.source_id)
        for record in bundle.records:
            _require_record(sources, record)
        for excluded in bundle.excluded:
            _require_source(sources, excluded.source_id)
        for order in bundle.revision_orders:
            _require_source(sources, order.source_id)


def build_decision_inputs(
    resolver: LocalArtifactResolver,
    snapshot: DataSnapshot,
    *,
    at: datetime,
    source_policy: SourceAllowlist,
    data_requirements: tuple[SourceDataRequirement, ...] = (),
    revision_orders: tuple[RevisionOrder, ...] = (),
    unscoped_policy: UnscopedRecordPolicy = UnscopedRecordPolicy.EXCLUDE,
) -> DecisionInputBundle:
    """Verify visible history before filtering; return no partial bundle on error."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("bundle cutoff must be timezone-aware")
    cutoff = at.astimezone(UTC)
    policy = UnscopedRecordPolicy(unscoped_policy)
    snapshot = DataSnapshot.model_validate(snapshot.model_dump())
    orders = tuple(
        RevisionOrder.model_validate(order.model_dump()) for order in revision_orders
    )
    source_policy = SourceAllowlist.model_validate(source_policy.model_dump())
    sources = _approved_sources(source_policy, cutoff)
    requirements = tuple(
        SourceDataRequirement.model_validate(item.model_dump())
        for item in data_requirements
    )
    if len({item.requirement_id for item in requirements}) != len(requirements):
        raise ValueError("data requirement IDs must be unique")
    for requirement in requirements:
        _require_source(sources, requirement.source_id)
    for record in snapshot.records:
        if record.observed_at.astimezone(UTC) <= cutoff:
            _require_record(sources, record)
    for order in orders:
        if order.observed_at.astimezone(UTC) <= cutoff:
            _require_source(sources, order.source_id)
    selected = resolver.select_verified_records_as_of(
        snapshot, at=cutoff, revision_orders=orders
    )
    members = resolver.select_universe_members_as_of(snapshot, at=cutoff)
    member_ids = {member.instrument_id for member in members}
    records: list[PointInTimeRecord] = []
    excluded: list[ExcludedRecord] = []
    for record in selected:
        reason: Literal["OUTSIDE_UNIVERSE", "UNSCOPED"] | None = None
        if record.instrument_id is None:
            if policy is UnscopedRecordPolicy.EXCLUDE:
                reason = "UNSCOPED"
        elif record.instrument_id not in member_ids:
            reason = "OUTSIDE_UNIVERSE"
        if reason is None:
            records.append(record)
        else:
            excluded.append(
                ExcludedRecord(
                    source_id=record.source_id,
                    source_item_id=record.source_item_id,
                    revision=record.revision,
                    reason=reason,
                )
            )
    applicable = sorted(
        (order for order in orders if order.observed_at.astimezone(UTC) <= cutoff),
        key=lambda order: (order.source_id, order.source_item_id),
    )
    return DecisionInputBundle(
        snapshot=DatasetSnapshotRef(
            snapshot_id=snapshot.snapshot_id,
            observed_through=snapshot.observed_through,
            digest=snapshot.content_digest(),
        ),
        cutoff=cutoff,
        data_requirements=requirements,
        source_policy=PolicyRef(
            policy_id=source_policy.metadata.policy_id,
            version=source_policy.metadata.version,
            digest=evidence_digest(source_policy),
        ),
        universe=snapshot.universe_membership,
        members=members,
        revision_orders=tuple(applicable),
        unscoped_policy=policy,
        records=tuple(records),
        excluded=tuple(excluded),
    )
