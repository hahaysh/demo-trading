"""Historical membership declarations, separate from their digest references."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from ats.domain.strategy import ArtifactRef, AssetClass, FrozenModel, Identifier


class UniverseMember(FrozenModel):
    instrument_id: Identifier
    asset_class: AssetClass
    membership_basis: Literal["KOSPI_200", "ETF_ALLOWLIST"]
    observed_at: AwareDatetime
    effective_from: AwareDatetime
    effective_until: AwareDatetime | None = None
    evidence: ArtifactRef

    @model_validator(mode="after")
    def validate_membership(self) -> Self:
        if self.effective_until is not None and (
            self.effective_until.astimezone(UTC) <= self.effective_from.astimezone(UTC)
        ):
            raise ValueError("membership interval must be nonempty and ordered")
        expected = (
            "KOSPI_200" if self.asset_class is AssetClass.EQUITY else "ETF_ALLOWLIST"
        )
        if self.membership_basis != expected:
            raise ValueError("membership basis does not match asset class")
        return self


class UniverseMembershipArtifact(FrozenModel):
    api_version: Literal["ats/v1"] = "ats/v1"
    kind: Literal["UniverseMembershipArtifact"] = "UniverseMembershipArtifact"
    manifest_id: Identifier
    market: Literal["KRX"] = "KRX"
    as_of: AwareDatetime
    members: tuple[UniverseMember, ...] = ()

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        horizon = self.as_of.astimezone(UTC)
        by_instrument: dict[str, list[UniverseMember]] = {}
        for member in self.members:
            if member.observed_at.astimezone(UTC) > horizon:
                raise ValueError("membership observation exceeds manifest horizon")
            by_instrument.setdefault(member.instrument_id, []).append(member)
        for history in by_instrument.values():
            ordered = sorted(
                history, key=lambda member: member.effective_from.astimezone(UTC)
            )
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if previous.effective_until is None or (
                    current.effective_from.astimezone(UTC)
                    < previous.effective_until.astimezone(UTC)
                ):
                    raise ValueError("duplicate or overlapping membership intervals")
        return self

    def members_at(self, at: datetime) -> tuple[UniverseMember, ...]:
        """Select declarations known and effective at at; never extrapolate."""
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("membership cutoff must be timezone-aware")
        validated = UniverseMembershipArtifact.model_validate(self.model_dump())
        cutoff = at.astimezone(UTC)
        if cutoff > validated.as_of.astimezone(UTC):
            raise ValueError("membership cutoff exceeds manifest horizon")
        selected = (
            member
            for member in validated.members
            if member.observed_at.astimezone(UTC) <= cutoff
            and member.effective_from.astimezone(UTC) <= cutoff
            and (
                member.effective_until is None
                or cutoff < member.effective_until.astimezone(UTC)
            )
        )
        return tuple(sorted(selected, key=lambda member: member.instrument_id))

    def require_member(self, instrument_id: str, *, at: datetime) -> UniverseMember:
        for member in self.members_at(at):
            if member.instrument_id == instrument_id:
                return member
        raise ValueError("instrument is not a known effective universe member")
