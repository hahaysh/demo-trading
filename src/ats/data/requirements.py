"""Explicit local data requirements; no inferred production freshness limits."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from ats.domain.data import PointInTimeRecord
from ats.domain.strategy import FrozenModel, Identifier


class DataRequirementError(ValueError):
    """Admitted inputs cannot satisfy a declared data requirement."""


class FreshnessBasis(StrEnum):
    OBSERVED = "OBSERVED"
    PUBLISHED = "PUBLISHED"
    EFFECTIVE = "EFFECTIVE"


class SourceDataRequirement(FrozenModel):
    requirement_id: Identifier
    source_id: Identifier
    source_item_id: Identifier | None = None
    instrument_id: Identifier | None = None
    min_records: Annotated[int, Field(strict=True, ge=1)]
    freshness_basis: FreshnessBasis
    max_age_seconds: Annotated[int, Field(strict=True, ge=0)]


def validate_data_requirements(
    requirements: tuple[SourceDataRequirement, ...],
    records: tuple[PointInTimeRecord, ...],
    *,
    at: datetime,
) -> None:
    """Require enough admitted records and freshness of every matching record."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise DataRequirementError("requirement cutoff must be timezone-aware")
    cutoff = at.astimezone(UTC)
    requirements = tuple(
        SourceDataRequirement.model_validate(item.model_dump()) for item in requirements
    )
    records = tuple(
        PointInTimeRecord.model_validate(item.model_dump()) for item in records
    )
    ids = [item.requirement_id for item in requirements]
    if len(ids) != len(set(ids)):
        raise DataRequirementError("data requirement IDs must be unique")
    for requirement in requirements:
        matching = tuple(
            record
            for record in records
            if record.source_id == requirement.source_id
            and (
                requirement.source_item_id is None
                or record.source_item_id == requirement.source_item_id
            )
            and (
                requirement.instrument_id is None
                or record.instrument_id == requirement.instrument_id
            )
        )
        if len(matching) < requirement.min_records:
            raise DataRequirementError(
                f"missing required data: {requirement.requirement_id}"
            )
        for record in matching:
            timestamp = {
                FreshnessBasis.OBSERVED: record.observed_at,
                FreshnessBasis.PUBLISHED: record.published_at,
                FreshnessBasis.EFFECTIVE: record.effective_at,
            }[requirement.freshness_basis]
            if record.observed_at.astimezone(UTC) > cutoff:
                raise DataRequirementError(
                    f"future observation: {requirement.requirement_id}"
                )
            if timestamp is None:
                raise DataRequirementError(
                    f"missing freshness timestamp: {requirement.requirement_id}"
                )
            age = (cutoff - timestamp.astimezone(UTC)).total_seconds()
            if age < 0:
                raise DataRequirementError(
                    f"future freshness timestamp: {requirement.requirement_id}"
                )
            if age > requirement.max_age_seconds:
                raise DataRequirementError(
                    f"stale required data: {requirement.requirement_id}"
                )
