"""Select known revisions without inferring chronology from revision labels."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import AwareDatetime, Field, model_validator

from ats.domain.data import DataSnapshot, PointInTimeRecord
from ats.domain.strategy import ArtifactRef, FrozenModel, Identifier


class AsOfSelectionError(ValueError):
    """Selection cannot safely resolve the supplied history at the cutoff."""


class RevisionOrder(FrozenModel):
    """Source-specific oldest-to-newest revision ordering known at observed_at."""

    source_id: Identifier
    source_item_id: Identifier
    revisions: Annotated[tuple[Identifier, ...], Field(min_length=2)]
    observed_at: AwareDatetime
    evidence: ArtifactRef

    @model_validator(mode="after")
    def require_unique_revisions(self) -> Self:
        if len(self.revisions) != len(set(self.revisions)):
            raise ValueError("revision order must contain unique revisions")
        return self


def select_records_as_of(
    snapshot: DataSnapshot,
    *,
    at: datetime,
    revision_orders: tuple[RevisionOrder, ...] = (),
) -> tuple[PointInTimeRecord, ...]:
    """Return one known revision per source/item or fail without partial output."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise AsOfSelectionError("as-of time must be timezone-aware")
    cutoff = at.astimezone(UTC)
    snapshot = DataSnapshot.model_validate(snapshot.model_dump())
    if cutoff > snapshot.observed_through.astimezone(UTC):
        raise AsOfSelectionError("as-of time exceeds snapshot freeze")

    visible: dict[tuple[str, str], dict[str, PointInTimeRecord]] = {}
    for record in snapshot.records:
        if record.observed_at.astimezone(UTC) <= cutoff:
            key = (record.source_id, record.source_item_id)
            visible.setdefault(key, {})[record.revision] = record

    orders: dict[tuple[str, str], RevisionOrder] = {}
    for supplied in revision_orders:
        order = RevisionOrder.model_validate(supplied.model_dump())
        if order.observed_at.astimezone(UTC) > cutoff:
            continue
        key = (order.source_id, order.source_item_id)
        if key in orders:
            raise AsOfSelectionError(f"multiple revision orders for {key}")
        orders[key] = order

    selected: list[PointInTimeRecord] = []
    for key in sorted(visible.keys() | orders.keys()):
        records = visible.get(key, {})
        order = orders.get(key)
        if order is not None:
            if set(order.revisions) != set(records):
                raise AsOfSelectionError(
                    f"incomplete or unlisted revision history for {key}"
                )
            selected.append(records[order.revisions[-1]])
        elif len(records) == 1:
            selected.append(next(iter(records.values())))
        else:
            raise AsOfSelectionError(
                f"ambiguous revisions require an explicit order for {key}"
            )
    return tuple(selected)
