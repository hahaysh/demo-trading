"""Local, policy-gated payload storage; no network or implicit collection approval."""

from __future__ import annotations

import hashlib
import sqlite3
import stat
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from ats.domain.governance import evidence_digest
from ats.domain.policy import (
    PolicyMetadata,
    PolicyStatus,
    RightsClass,
    SourceAllowlist,
    SourceReviewState,
)
from ats.domain.strategy import FrozenModel, Identifier, Sha256Digest


class StorageError(ValueError):
    """The caller's storage request cannot be admitted safely."""


class StoragePermit(FrozenModel):
    metadata: PolicyMetadata
    source_id: Identifier
    source_policy_digest: Sha256Digest
    allow_persistence: bool = False
    destination: Literal["LOCAL_ONLY"] = "LOCAL_ONLY"
    retention_days: Annotated[int, Field(strict=True, ge=1)]
    expires_at: AwareDatetime
    agent_editable: Literal[False] = False


class StoredPayload(FrozenModel):
    source_id: Identifier
    digest: Sha256Digest
    policy_digest: Sha256Digest
    permit_digest: Sha256Digest
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    byte_count: Annotated[int, Field(strict=True, ge=1)]


class PayloadManifest(FrozenModel):
    name: Identifier
    payload: StoredPayload


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("storage timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _deadline(
    permit: StoragePermit, policy: SourceAllowlist, observed_at: datetime, now: datetime
) -> datetime:
    permit = StoragePermit.model_validate(permit.model_dump())
    policy = SourceAllowlist.model_validate(policy.model_dump())
    source = next(
        (entry for entry in policy.sources if entry.source_id == permit.source_id), None
    )
    if (
        not permit.allow_persistence
        or permit.metadata.status is not PolicyStatus.APPROVED
        or permit.metadata.approved_at is None
        or permit.metadata.approved_at > observed_at
        or policy.metadata.status is not PolicyStatus.APPROVED
        or policy.metadata.approved_at is None
        or policy.metadata.approved_at > observed_at
        or evidence_digest(policy) != permit.source_policy_digest
        or source is None
        or not source.enabled
        or source.legal_review is not SourceReviewState.APPROVED
        or source.rights.classification is RightsClass.PENDING_REVIEW
        or source.rights.retention_days is None
        or observed_at > now
    ):
        raise StorageError(
            "approved source policy and separate storage permit required"
        )
    expiry = min(
        observed_at
        + timedelta(days=min(source.rights.retention_days, permit.retention_days)),
        _utc(permit.expires_at),
    )
    if now >= expiry:
        raise StorageError("payload retention or storage authorization expired")
    return expiry


class LocalPayloadStore:
    """Trusted local filesystem only; SQLite commits payload and receipt together."""

    def __init__(self, database: Path, *, max_bytes: int = 16 * 1024 * 1024) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise StorageError("max_bytes must be a positive integer")
        self.database = database.absolute()
        self.max_bytes = max_bytes

    def _check_path(self) -> None:
        for path in (self.database, *self.database.parents):
            if path.is_symlink():
                raise StorageError("storage links are forbidden")
            if path.exists():
                info = path.lstat()
                attributes: int = getattr(info, "st_file_attributes", 0)
                if stat.S_ISLNK(info.st_mode) or (
                    attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise StorageError("storage reparse points are forbidden")

    def check_authorization(
        self, *, now: datetime, source_policy: SourceAllowlist, permit: StoragePermit
    ) -> None:
        _deadline(permit, source_policy, _utc(now), _utc(now))

    def receipt_for_digest(
        self,
        digest: str,
        *,
        now: datetime,
        source_policy: SourceAllowlist,
        permit: StoragePermit,
    ) -> StoredPayload | None:
        self.check_authorization(now=now, source_policy=source_policy, permit=permit)
        with self._connection(create=True) as connection:
            row = connection.execute(
                "SELECT receipt FROM payloads WHERE source_id=? AND digest=?",
                (permit.source_id, digest),
            ).fetchone()
        if row is None:
            return None
        receipt = StoredPayload.model_validate_json(row["receipt"])
        if receipt.digest != digest:
            raise StorageError("payload lookup identity mismatch")
        self.read(receipt, now=now, source_policy=source_policy, permit=permit)
        return receipt

    @contextmanager
    def _connection(
        self, *, create: bool = False
    ) -> Generator[sqlite3.Connection, None, None]:
        self._check_path()
        if not create and not self.database.is_file():
            raise StorageError("payload database does not exist")
        if create:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("BEGIN IMMEDIATE")
            if create:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS payloads ("
                    "source_id TEXT NOT NULL, digest TEXT NOT NULL, "
                    "receipt TEXT NOT NULL, expires_at TEXT NOT NULL, "
                    "payload BLOB NOT NULL, PRIMARY KEY(source_id, digest))"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS manifests (name TEXT PRIMARY KEY, "
                    "source_id TEXT NOT NULL, digest TEXT NOT NULL, record TEXT NOT NULL, "
                    "FOREIGN KEY(source_id,digest) REFERENCES payloads(source_id,digest) "
                    "ON DELETE CASCADE)"
                )
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def put(
        self,
        payload: bytes,
        *,
        observed_at: datetime,
        now: datetime,
        source_policy: SourceAllowlist,
        permit: StoragePermit,
    ) -> StoredPayload:
        observed_at, now = _utc(observed_at), _utc(now)
        expiry = _deadline(permit, source_policy, observed_at, now)
        if not payload or len(payload) > self.max_bytes:
            raise StorageError("payload is empty or exceeds byte limit")
        receipt = StoredPayload(
            source_id=permit.source_id,
            digest="sha256:" + hashlib.sha256(payload).hexdigest(),
            policy_digest=evidence_digest(source_policy),
            permit_digest=evidence_digest(permit),
            observed_at=observed_at,
            expires_at=expiry,
            byte_count=len(payload),
        )
        with self._connection(create=True) as connection:
            row = connection.execute(
                "SELECT receipt, substr(payload, 1, ?) AS payload, length(payload) AS size "
                "FROM payloads WHERE source_id=? AND digest=?",
                (self.max_bytes + 1, receipt.source_id, receipt.digest),
            ).fetchone()
            if row is not None:
                previous = StoredPayload.model_validate_json(row["receipt"])
                if (
                    previous.policy_digest != receipt.policy_digest
                    or previous.permit_digest != receipt.permit_digest
                    or previous.observed_at > observed_at
                    or now >= previous.expires_at
                    or row["size"] != receipt.byte_count
                    or previous.source_id != receipt.source_id
                    or previous.digest != receipt.digest
                    or previous.byte_count != receipt.byte_count
                    or previous.expires_at
                    != _deadline(permit, source_policy, previous.observed_at, now)
                    or row["payload"] != payload
                ):
                    raise StorageError("existing payload conflicts or has expired")
                return previous
            connection.execute(
                "INSERT INTO payloads VALUES (?, ?, ?, ?, ?)",
                (
                    receipt.source_id,
                    receipt.digest,
                    receipt.model_dump_json(),
                    expiry.isoformat(timespec="microseconds"),
                    payload,
                ),
            )
        return receipt

    def read(
        self,
        receipt: StoredPayload,
        *,
        now: datetime,
        source_policy: SourceAllowlist,
        permit: StoragePermit,
    ) -> bytes:
        receipt = StoredPayload.model_validate(receipt.model_dump())
        now = _utc(now)
        expiry = _deadline(permit, source_policy, _utc(receipt.observed_at), now)
        if (
            receipt.source_id != permit.source_id
            or receipt.policy_digest != evidence_digest(source_policy)
            or receipt.permit_digest != evidence_digest(permit)
            or receipt.expires_at != expiry
            or now >= receipt.expires_at
        ):
            raise StorageError("receipt does not match current storage authorization")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT receipt, substr(payload, 1, ?) AS payload, length(payload) AS size "
                "FROM payloads WHERE source_id=? AND digest=?",
                (self.max_bytes + 1, receipt.source_id, receipt.digest),
            ).fetchone()
            if row is None or row["receipt"] != receipt.model_dump_json():
                raise StorageError("payload missing or receipt changed")
            payload: object = row["payload"]
            if (
                not isinstance(payload, bytes)
                or row["size"] != receipt.byte_count
                or len(payload) != receipt.byte_count
                or len(payload) > self.max_bytes
                or "sha256:" + hashlib.sha256(payload).hexdigest() != receipt.digest
            ):
                raise StorageError("stored payload integrity check failed")
            return payload

    def bind_manifest(
        self,
        manifest: PayloadManifest,
        *,
        now: datetime,
        source_policy: SourceAllowlist,
        permit: StoragePermit,
    ) -> PayloadManifest:
        manifest = PayloadManifest.model_validate(manifest.model_dump())
        self.read(manifest.payload, now=now, source_policy=source_policy, permit=permit)
        with self._connection(create=True) as connection:
            encoded = manifest.model_dump_json()
            previous = connection.execute(
                "SELECT record FROM manifests WHERE name=?", (manifest.name,)
            ).fetchone()
            if previous is not None and previous["record"] != encoded:
                raise StorageError("manifest is immutable; use a new version name")
            connection.execute(
                "INSERT OR IGNORE INTO manifests VALUES (?, ?, ?, ?)",
                (
                    manifest.name,
                    manifest.payload.source_id,
                    manifest.payload.digest,
                    encoded,
                ),
            )
        return manifest

    def resolve_manifest(
        self,
        name: str,
        *,
        now: datetime,
        source_policy: SourceAllowlist,
        permit: StoragePermit,
    ) -> PayloadManifest:
        _deadline(permit, source_policy, _utc(now), _utc(now))
        with self._connection(create=True) as connection:
            row = connection.execute(
                "SELECT record FROM manifests WHERE name=?", (name,)
            ).fetchone()
            if row is None:
                raise StorageError("manifest is missing or expired")
            manifest = PayloadManifest.model_validate_json(row["record"])
            if manifest.name != name:
                raise StorageError("manifest identity changed")
        self.read(manifest.payload, now=now, source_policy=source_policy, permit=permit)
        return manifest

    def purge_expired(self, *, now: datetime) -> int:
        now = _utc(now)
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM payloads WHERE expires_at<=?",
                (now.isoformat(timespec="microseconds"),),
            )
            return cursor.rowcount
