"""Immutable point-in-time data contracts used by research and signals."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from ats.domain.policy import RightsClass
from ats.domain.strategy import (
    ArtifactRef,
    DatasetSnapshotRef,
    FrozenModel,
    Identifier,
    NonEmptyText,
    Sha256Digest,
)


class CredibilityTier(StrEnum):
    PRIMARY = "PRIMARY"
    LICENSED = "LICENSED"
    PUBLIC = "PUBLIC"
    LOW_TRUST = "LOW_TRUST"


class PointInTimeRecord(FrozenModel):
    """A source revision available to the system at a known observation time."""

    source_id: Identifier
    source_item_id: Identifier
    revision: Identifier
    published_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    effective_at: AwareDatetime | None = None
    instrument_id: Identifier | None = None
    rights_class: RightsClass
    credibility_tier: CredibilityTier
    content_hash: Sha256Digest
    raw_payload_digest: Sha256Digest

    @model_validator(mode="after")
    def reject_impossible_publication_time(self) -> Self:
        if self.published_at is not None and self.published_at > self.observed_at:
            raise ValueError("published_at cannot be later than observed_at")
        return self

    @property
    def revision_key(self) -> tuple[str, str, str]:
        return (self.source_id, self.source_item_id, self.revision)


class UniverseMembershipManifest(FrozenModel):
    """Content-addressed historical universe membership used by a snapshot."""

    manifest_id: Identifier
    as_of: AwareDatetime
    digest: Sha256Digest


class DataSnapshot(FrozenModel):
    """A frozen, reproducible set of records available at a decision cutoff."""

    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["DataSnapshot"] = "DataSnapshot"
    snapshot_id: Identifier
    observed_through: AwareDatetime
    created_at: AwareDatetime
    universe_membership: UniverseMembershipManifest
    records: tuple[PointInTimeRecord, ...] = ()

    @model_validator(mode="after")
    def validate_point_in_time_boundaries(self) -> Self:
        if self.created_at < self.observed_through:
            raise ValueError("created_at cannot be earlier than observed_through")
        if self.universe_membership.as_of > self.observed_through:
            raise ValueError("universe manifest cannot be newer than the snapshot")

        future_records = [
            record.source_item_id
            for record in self.records
            if record.observed_at > self.observed_through
        ]
        if future_records:
            names = ", ".join(sorted(future_records))
            raise ValueError(f"records observed after snapshot freeze: {names}")

        record_keys = [record.revision_key for record in self.records]
        if len(record_keys) != len(set(record_keys)):
            raise ValueError("snapshot records must have unique source revisions")
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


class MarketEventType(StrEnum):
    DISCLOSURE = "DISCLOSURE"
    NEWS = "NEWS"
    RUMOR = "RUMOR"
    CORPORATE_ACTION = "CORPORATE_ACTION"


class MarketEvent(FrozenModel):
    """A derived observation, not an order or an authorization to trade."""

    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["MarketEvent"] = "MarketEvent"
    event_id: Identifier
    event_type: MarketEventType
    summary: NonEmptyText
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    available_at: AwareDatetime
    producer: ArtifactRef
    dataset_snapshot: DatasetSnapshotRef
    evidence: Annotated[tuple[PointInTimeRecord, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        keys = [record.revision_key for record in self.evidence]
        if len(keys) != len(set(keys)):
            raise ValueError("event evidence must have unique source revisions")
        if any(record.observed_at > self.available_at for record in self.evidence):
            raise ValueError("event cannot be available before its evidence")
        if self.available_at > self.dataset_snapshot.observed_through:
            raise ValueError("event is available after snapshot freeze")
        if any(
            record.rights_class is RightsClass.PENDING_REVIEW
            for record in self.evidence
        ):
            raise ValueError("event evidence requires resolved usage rights")
        return self

    def validate_against_snapshot(self, snapshot: DataSnapshot) -> None:
        """Resolve the claimed provenance against the actual frozen input set."""
        if (
            self.dataset_snapshot.snapshot_id != snapshot.snapshot_id
            or self.dataset_snapshot.observed_through != snapshot.observed_through
            or self.dataset_snapshot.digest != snapshot.content_digest()
        ):
            raise ValueError("event snapshot reference does not match snapshot")
        records = {record.revision_key: record for record in snapshot.records}
        for record in self.evidence:
            if records.get(record.revision_key) != record:
                raise ValueError("event evidence does not match snapshot record")
