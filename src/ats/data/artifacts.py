"""Read and verify local content-addressed bytes without network access."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from ats.data.asof import RevisionOrder, select_records_as_of
from ats.domain.data import DataSnapshot, PointInTimeRecord, UniverseMembershipManifest
from ats.domain.strategy import ArtifactRef
from ats.domain.universe import UniverseMember, UniverseMembershipArtifact


class ArtifactResolutionError(ValueError):
    """An artifact cannot be resolved with the required integrity guarantees."""


class LocalArtifactResolver:
    """Read from a trusted, read-only root using root/sha256/<lowercase hex>."""

    def __init__(self, root: Path, *, max_bytes: int = 16 * 1024 * 1024) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        try:
            self._root = root.resolve(strict=True)
            if not self._root.is_dir():
                raise ArtifactResolutionError("artifact root must be a directory")
        except OSError as error:
            raise ArtifactResolutionError("artifact root is unavailable") from error
        self._max_bytes = max_bytes

    @staticmethod
    def _reject_link(path: Path) -> None:
        info = path.lstat()
        attributes: int = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ArtifactResolutionError(
                "artifact links and reparse points are forbidden"
            )

    def read_digest(self, digest: str) -> bytes:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ArtifactResolutionError("invalid SHA-256 digest")
        directory = self._root / "sha256"
        path = directory / digest.removeprefix("sha256:")
        try:
            self._reject_link(directory)
            self._reject_link(path)
            if not path.resolve(strict=True).is_relative_to(self._root):
                raise ArtifactResolutionError("artifact escapes configured root")
            if not stat.S_ISREG(path.stat().st_mode):
                raise ArtifactResolutionError("artifact must be a regular file")
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ArtifactResolutionError("artifact must be a regular file")
                if info.st_size > self._max_bytes:
                    raise ArtifactResolutionError("artifact exceeds byte limit")
                payload = stream.read(self._max_bytes + 1)
        except OSError as error:
            raise ArtifactResolutionError(
                f"artifact is missing or unreadable: {digest}"
            ) from error
        if len(payload) > self._max_bytes:
            raise ArtifactResolutionError("artifact exceeds byte limit")
        if hashlib.sha256(payload).hexdigest() != digest.removeprefix("sha256:"):
            raise ArtifactResolutionError(f"artifact digest mismatch: {digest}")
        return payload

    def read_artifact(self, reference: ArtifactRef) -> bytes:
        reference = ArtifactRef.model_validate(reference.model_dump())
        return self.read_digest(reference.digest)

    def read_raw_payload(self, record: PointInTimeRecord) -> bytes:
        record = PointInTimeRecord.model_validate(record.model_dump())
        return self.read_digest(record.raw_payload_digest)

    def read_revision_evidence(self, order: RevisionOrder) -> bytes:
        order = RevisionOrder.model_validate(order.model_dump())
        return self.read_artifact(order.evidence)

    def read_universe_membership(
        self, reference: UniverseMembershipManifest
    ) -> UniverseMembershipArtifact:
        reference = UniverseMembershipManifest.model_validate(reference.model_dump())
        artifact = UniverseMembershipArtifact.model_validate_json(
            self.read_digest(reference.digest)
        )
        if (
            artifact.manifest_id != reference.manifest_id
            or artifact.as_of != reference.as_of
        ):
            raise ArtifactResolutionError("universe manifest reference mismatch")
        return artifact

    def select_universe_members_as_of(
        self, snapshot: DataSnapshot, *, at: datetime
    ) -> tuple[UniverseMember, ...]:
        snapshot = DataSnapshot.model_validate(snapshot.model_dump())
        if at.tzinfo is None or at.utcoffset() is None:
            raise ArtifactResolutionError("universe cutoff must be timezone-aware")
        if at.astimezone(UTC) > snapshot.observed_through.astimezone(UTC):
            raise ArtifactResolutionError("universe cutoff exceeds snapshot freeze")
        artifact = self.read_universe_membership(snapshot.universe_membership)
        return artifact.members_at(at)

    def select_verified_records_as_of(
        self,
        snapshot: DataSnapshot,
        *,
        at: datetime,
        revision_orders: tuple[RevisionOrder, ...] = (),
    ) -> tuple[PointInTimeRecord, ...]:
        """Verify all visible raw revisions and ordering evidence before returning."""
        snapshot = DataSnapshot.model_validate(snapshot.model_dump())
        orders = tuple(
            RevisionOrder.model_validate(order.model_dump())
            for order in revision_orders
        )
        selected = select_records_as_of(snapshot, at=at, revision_orders=orders)
        cutoff = at.astimezone(UTC)
        for record in snapshot.records:
            if record.observed_at.astimezone(UTC) <= cutoff:
                self.read_raw_payload(record)
        for order in orders:
            if order.observed_at.astimezone(UTC) <= cutoff:
                self.read_revision_evidence(order)
        return selected
