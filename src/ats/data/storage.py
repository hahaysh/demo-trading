"""Local, policy-gated payload storage; no network or implicit collection approval."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

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
        if self.database.is_file():
            with self._connection() as connection:
                self._check_quarantine(connection, permit.source_id)

    @staticmethod
    def _check_quarantine(connection: sqlite3.Connection, source_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM source_quarantine WHERE source_id=?", (source_id,)
        ).fetchone():
            raise StorageError(
                "source is quarantined after item withdrawal; operator review required"
            )

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
            connection.execute(
                "CREATE TABLE IF NOT EXISTS source_quarantine (source_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, removed_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS storage_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO storage_identity VALUES (1,?)", (str(uuid4()),)
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS payload_tombstones ("
                "source_id TEXT NOT NULL, digest TEXT NOT NULL, "
                "removed_at TEXT NOT NULL, reason TEXT NOT NULL, "
                "PRIMARY KEY(source_id,digest))"
            )
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
            connection.execute(
                "CREATE TABLE IF NOT EXISTS payload_dependencies ("
                "source_id TEXT NOT NULL,digest TEXT NOT NULL,parent_source TEXT NOT NULL,"
                "parent_digest TEXT NOT NULL,PRIMARY KEY(source_id,digest,parent_source,parent_digest),"
                "FOREIGN KEY(source_id,digest) REFERENCES payloads(source_id,digest) ON DELETE CASCADE)"
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
        parents: tuple[StoredPayload, ...] = (),
    ) -> StoredPayload:
        observed_at, now = _utc(observed_at), _utc(now)
        expiry = _deadline(permit, source_policy, observed_at, now)
        if len(parents) > 2048 or len(
            {(item.source_id, item.digest) for item in parents}
        ) != len(parents):
            raise StorageError("dependencies must be unique and bounded")
        for parent in parents:
            self.read(parent, now=now, source_policy=source_policy, permit=permit)
            if parent.observed_at > observed_at:
                raise StorageError("derived payload cannot precede its source")
            expiry = min(expiry, parent.expires_at)
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
            self._check_quarantine(connection, receipt.source_id)
            for parent in parents:
                retained = connection.execute(
                    "SELECT receipt FROM payloads WHERE source_id=? AND digest=?",
                    (parent.source_id, parent.digest),
                ).fetchone()
                if retained is None or retained["receipt"] != parent.model_dump_json():
                    raise StorageError("dependency removed before derived write")
            if connection.execute(
                "SELECT 1 FROM payload_tombstones WHERE source_id=? AND digest=?",
                (receipt.source_id, receipt.digest),
            ).fetchone():
                raise StorageError("tombstoned payload cannot be collected again")
            row = connection.execute(
                "SELECT receipt, substr(payload, 1, ?) AS payload, length(payload) AS size "
                "FROM payloads WHERE source_id=? AND digest=?",
                (self.max_bytes + 1, receipt.source_id, receipt.digest),
            ).fetchone()
            if row is not None:
                dependencies = {
                    (item["parent_source"], item["parent_digest"])
                    for item in connection.execute(
                        "SELECT parent_source,parent_digest FROM payload_dependencies WHERE source_id=? AND digest=?",
                        (receipt.source_id, receipt.digest),
                    )
                }
                if dependencies != {(item.source_id, item.digest) for item in parents}:
                    raise StorageError("payload dependency set is immutable")
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
                    != min(
                        [
                            _deadline(permit, source_policy, previous.observed_at, now),
                            *[item.expires_at for item in parents],
                        ]
                    )
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
            connection.executemany(
                "INSERT INTO payload_dependencies VALUES (?,?,?,?)",
                [
                    (receipt.source_id, receipt.digest, parent.source_id, parent.digest)
                    for parent in parents
                ],
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
            or receipt.expires_at > expiry
            or now >= receipt.expires_at
        ):
            raise StorageError("receipt does not match current storage authorization")
        with self._connection() as connection:
            dependencies = connection.execute(
                "WITH RECURSIVE ancestors(source_id,digest) AS ("
                "SELECT parent_source,parent_digest FROM payload_dependencies WHERE source_id=? AND digest=? "
                "UNION SELECT d.parent_source,d.parent_digest FROM payload_dependencies d "
                "JOIN ancestors a ON d.source_id=a.source_id AND d.digest=a.digest) "
                "SELECT p.receipt,p.payload,p.expires_at FROM ancestors a "
                "LEFT JOIN payloads p ON p.source_id=a.source_id AND p.digest=a.digest",
                (receipt.source_id, receipt.digest),
            ).fetchall()
            for dependency in dependencies:
                if dependency["receipt"] is None:
                    raise StorageError("derived source is missing")
                parent = StoredPayload.model_validate_json(dependency["receipt"])
                if (
                    parent.source_id != permit.source_id
                    or parent.policy_digest != receipt.policy_digest
                    or parent.permit_digest != receipt.permit_digest
                    or parent.observed_at > receipt.observed_at
                    or now >= parent.expires_at
                    or hashlib.sha256(dependency["payload"]).hexdigest()
                    != parent.digest[7:]
                ):
                    raise StorageError("derived source integrity or retention failed")
                expiry = min(expiry, parent.expires_at)
            if receipt.expires_at != expiry:
                raise StorageError("derived receipt retention mismatch")
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

    def withdraw(
        self,
        receipt: StoredPayload,
        *,
        now: datetime,
        source_policy: SourceAllowlist,
        permit: StoragePermit,
    ) -> int:
        self.read(receipt, now=now, source_policy=source_policy, permit=permit)
        with self._connection() as connection:
            return self._remove(
                connection,
                ((receipt.source_id, receipt.digest),),
                now=_utc(now),
                reason="WITHDRAWN",
            )

    @staticmethod
    def _remove(
        connection: sqlite3.Connection,
        roots: tuple[tuple[str, str], ...],
        *,
        now: datetime,
        reason: str,
    ) -> int:
        affected: set[tuple[str, str]] = set(roots)
        pending = list(roots)
        while pending:
            source_id, digest = pending.pop()
            for row in connection.execute(
                "SELECT source_id,digest FROM payload_dependencies WHERE parent_source=? AND parent_digest=?",
                (source_id, digest),
            ):
                identity = (row["source_id"], row["digest"])
                if identity not in affected:
                    affected.add(identity)
                    pending.append(identity)
        connection.executemany(
            "INSERT OR IGNORE INTO payload_tombstones VALUES (?,?,?,?)",
            [
                (source_id, digest, now.isoformat(timespec="microseconds"), reason)
                for source_id, digest in affected
            ],
        )
        removed = 0
        for identity in affected:
            removed += connection.execute(
                "DELETE FROM payloads WHERE source_id=? AND digest=?", identity
            ).rowcount
        return removed

    def purge_expired(self, *, now: datetime) -> int:
        now = _utc(now)
        with self._connection() as connection:
            roots = tuple(
                (row["source_id"], row["digest"])
                for row in connection.execute(
                    "SELECT source_id,digest FROM payloads WHERE expires_at<=?",
                    (now.isoformat(timespec="microseconds"),),
                )
            )
            return self._remove(connection, roots, now=now, reason="EXPIRED")

    @staticmethod
    def _check_data_backup(connection: sqlite3.Connection) -> None:
        allowed = {
            "source_quarantine",
            "storage_identity",
            "payloads",
            "manifests",
            "payload_tombstones",
            "payload_dependencies",
            "info_schedules",
            "info_runs",
            "info_index",
        }
        objects = connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if any(
            kind not in ("table", "index") or kind == "table" and name not in allowed
            for name, kind in objects
        ):
            raise StorageError("only data stores can use this recovery path")
        if not {"storage_identity", "payloads", "payload_tombstones"}.issubset(
            {name for name, kind in objects if kind == "table"}
        ):
            raise StorageError("data backup lacks retention history")
        if (
            connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise StorageError("data backup integrity failed")

    def backup(self, destination: Path) -> str:
        self._check_path()
        target = LocalPayloadStore(destination)
        target._check_path()
        if not self.database.is_file() or target.database.exists():
            raise StorageError("existing source and new backup destination required")
        target.database.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=target.database.parent, suffix=".partial"
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            source = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
            output = sqlite3.connect(temporary)
            try:
                self._check_data_backup(source)
                source.backup(output)
                self._check_data_backup(output)
            finally:
                output.close()
                source.close()
            with temporary.open("rb") as stream:
                digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            os.link(temporary, target.database)
            return digest
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def restore_data_backup(
        cls,
        backup: Path,
        destination: Path,
        *,
        expected_digest: str,
        now: datetime,
        retention_authority: LocalPayloadStore,
    ) -> LocalPayloadStore:
        now = _utc(now)
        source, target = LocalPayloadStore(backup), LocalPayloadStore(destination)
        source._check_path()
        target._check_path()
        if not source.database.is_file() or target.database.exists():
            raise StorageError("existing backup and new restore destination required")
        target.database.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=target.database.parent, suffix=".partial"
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copyfile(source.database, temporary)
            with temporary.open("rb") as stream:
                if (
                    "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                    != expected_digest
                ):
                    raise StorageError("backup digest mismatch")
            with retention_authority._connection() as authority:
                connection = sqlite3.connect(temporary)
                connection.row_factory = sqlite3.Row
                try:
                    cls._check_data_backup(connection)
                    identity = connection.execute(
                        "SELECT identity FROM storage_identity WHERE singleton=1"
                    ).fetchone()
                    current = authority.execute(
                        "SELECT identity FROM storage_identity WHERE singleton=1"
                    ).fetchone()
                    if identity is None or current is None or identity[0] != current[0]:
                        raise StorageError(
                            "backup retention authority identity mismatch"
                        )
                    tombstones = authority.execute(
                        "SELECT * FROM payload_tombstones"
                    ).fetchall()
                    if any(
                        datetime.fromisoformat(item["removed_at"]) > now
                        for item in tombstones
                    ):
                        raise StorageError(
                            "restore clock precedes retained deletion history"
                        )
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("PRAGMA secure_delete=ON")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS source_quarantine (source_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, removed_at TEXT NOT NULL)"
                    )
                    for quarantine in authority.execute(
                        "SELECT source_id,item_id,removed_at FROM source_quarantine"
                    ):
                        if datetime.fromisoformat(quarantine["removed_at"]) > now:
                            raise StorageError(
                                "restore clock precedes source quarantine"
                            )
                        connection.execute(
                            "INSERT OR IGNORE INTO source_quarantine VALUES (?,?,?)",
                            tuple(quarantine),
                        )
                    for item in tombstones:
                        cls._remove(
                            connection,
                            ((item["source_id"], item["digest"]),),
                            now=datetime.fromisoformat(item["removed_at"]),
                            reason=item["reason"],
                        )
                    connection.commit()
                finally:
                    connection.close()
                LocalPayloadStore(temporary).purge_expired(now=now)
                os.link(temporary, target.database)
            return target
        finally:
            temporary.unlink(missing_ok=True)
