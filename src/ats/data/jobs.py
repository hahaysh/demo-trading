"""Leased collection jobs with fenced completion and durable batch manifests."""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from ats.data.collection import (
    CalendarWindow,
    archive_kis_batch,
    ingest_kis_daily,
    restore_kis_batch,
)
from ats.data.kis import KisDailyRequest, KisQuoteReceipt
from ats.data.storage import (
    LocalPayloadStore,
    PayloadManifest,
    StoragePermit,
    StoredPayload,
)
from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.domain.strategy import FrozenModel, Identifier, Sha256Digest


class CollectionJob(FrozenModel):
    job_id: Identifier
    request: KisDailyRequest
    calendar: CalendarWindow
    policy_digest: Sha256Digest
    permit_digest: Sha256Digest
    created_at: AwareDatetime
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=10)] = 3


class JobCheckpoint(FrozenModel):
    job: CollectionJob
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"] = "PENDING"
    attempts: Annotated[int, Field(strict=True, ge=0)] = 0
    updated_at: AwareDatetime
    lease_token: UUID | None = None
    lease_until: AwareDatetime | None = None
    archive: StoredPayload | None = None
    failure: Literal["COLLECTION_FAILED"] | None = None


class CollectionJobError(ValueError):
    pass


class CollectionJobStore(LocalPayloadStore):
    def enqueue(self, job: CollectionJob) -> JobCheckpoint:
        job = CollectionJob.model_validate(job.model_dump())
        checkpoint = JobCheckpoint(job=job, updated_at=job.created_at)
        with self._connection(create=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collection_jobs "
                "(job_id TEXT PRIMARY KEY, record TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT record FROM collection_jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()
            if row is not None:
                previous = JobCheckpoint.model_validate_json(row["record"])
                if previous.job != job:
                    raise CollectionJobError(
                        "job identity conflicts with pinned inputs"
                    )
                return previous
            connection.execute(
                "INSERT INTO collection_jobs VALUES (?, ?)",
                (job.job_id, checkpoint.model_dump_json()),
            )
        return checkpoint

    def get_job(self, job_id: str) -> JobCheckpoint:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record FROM collection_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise CollectionJobError("unknown collection job")
            return JobCheckpoint.model_validate_json(row["record"])

    def claim(
        self, job_id: str, *, at: datetime, lease_seconds: int = 60
    ) -> JobCheckpoint:
        if at.tzinfo is None or at.utcoffset() is None or not 1 <= lease_seconds <= 300:
            raise CollectionJobError("aware clock and bounded lease required")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record FROM collection_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise CollectionJobError("unknown collection job")
            previous = JobCheckpoint.model_validate_json(row["record"])
            if at < previous.updated_at:
                raise CollectionJobError("job clock regressed")
            if previous.status == "SUCCEEDED":
                return previous
            if previous.lease_until is not None and at < previous.lease_until:
                raise CollectionJobError("job lease is still active")
            if previous.attempts >= previous.job.max_attempts:
                raise CollectionJobError("collection attempt budget exhausted")
            claimed = JobCheckpoint.model_validate(
                {
                    **previous.model_dump(),
                    "status": "RUNNING",
                    "updated_at": at,
                    "attempts": previous.attempts + 1,
                    "lease_token": uuid4(),
                    "lease_until": at + timedelta(seconds=lease_seconds),
                    "failure": None,
                }
            )
            connection.execute(
                "UPDATE collection_jobs SET record=? WHERE job_id=?",
                (claimed.model_dump_json(), job_id),
            )
            return claimed

    def finish(
        self,
        claim: JobCheckpoint,
        *,
        at: datetime,
        archive: StoredPayload | None = None,
        source_policy: SourceAllowlist | None = None,
        permit: StoragePermit | None = None,
    ) -> JobCheckpoint:
        claim = JobCheckpoint.model_validate(claim.model_dump())
        if at.tzinfo is None or at.utcoffset() is None:
            raise CollectionJobError("aware finish clock required")
        if archive is not None:
            if source_policy is None or permit is None:
                raise CollectionJobError(
                    "completion requires current storage authority"
                )
            batch = restore_kis_batch(
                archive, store=self, source_policy=source_policy, permit=permit, now=at
            )
            if (
                batch.normalization.request != claim.job.request
                or batch.calendar != claim.job.calendar
            ):
                raise CollectionJobError("completed batch differs from the pinned job")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record FROM collection_jobs WHERE job_id=?", (claim.job.job_id,)
            ).fetchone()
            if row is None:
                raise CollectionJobError("unknown collection job")
            current = JobCheckpoint.model_validate_json(row["record"])
            if (
                current != claim
                or current.status != "RUNNING"
                or current.lease_until is None
                or not current.updated_at <= at < current.lease_until
            ):
                raise CollectionJobError("stale worker cannot finish collection")
            if archive is not None:
                archive = StoredPayload.model_validate(archive.model_dump())
                row = connection.execute(
                    "SELECT receipt FROM payloads WHERE source_id=? AND digest=?",
                    (archive.source_id, archive.digest),
                ).fetchone()
                if (
                    row is None
                    or row["receipt"] != archive.model_dump_json()
                    or at >= archive.expires_at
                    or archive.policy_digest != claim.job.policy_digest
                    or archive.permit_digest != claim.job.permit_digest
                ):
                    raise CollectionJobError(
                        "job result is not a retained bound archive"
                    )
                manifest = PayloadManifest(name=claim.job.job_id, payload=archive)
                existing = connection.execute(
                    "SELECT record FROM manifests WHERE name=?", (manifest.name,)
                ).fetchone()
                if (
                    existing is not None
                    and existing["record"] != manifest.model_dump_json()
                ):
                    raise CollectionJobError("job manifest conflicts")
                connection.execute(
                    "INSERT OR IGNORE INTO manifests VALUES (?, ?, ?, ?)",
                    (
                        manifest.name,
                        archive.source_id,
                        archive.digest,
                        manifest.model_dump_json(),
                    ),
                )
            completed = JobCheckpoint.model_validate(
                {
                    **current.model_dump(),
                    "status": "SUCCEEDED" if archive else "FAILED",
                    "updated_at": at,
                    "lease_until": None,
                    "lease_token": None,
                    "archive": archive,
                    "failure": None if archive else "COLLECTION_FAILED",
                }
            )
            connection.execute(
                "UPDATE collection_jobs SET record=? WHERE job_id=?",
                (completed.model_dump_json(), claim.job.job_id),
            )
            return completed


def run_collection_job(
    store: CollectionJobStore,
    job: CollectionJob,
    *,
    source_policy: SourceAllowlist,
    permit: StoragePermit,
    fetch: Callable[[KisDailyRequest], KisQuoteReceipt],
    clock: Callable[[], datetime],
) -> JobCheckpoint:
    at = clock()
    store.check_authorization(now=at, source_policy=source_policy, permit=permit)
    if job.policy_digest != evidence_digest(
        source_policy
    ) or job.permit_digest != evidence_digest(permit):
        raise CollectionJobError("job policy binding mismatch")
    store.enqueue(job)
    claim = store.claim(job.job_id, at=at)
    if claim.status == "SUCCEEDED":
        if claim.archive is None:
            raise CollectionJobError("successful job is missing its archive")
        restore_kis_batch(
            claim.archive,
            store=store,
            source_policy=source_policy,
            permit=permit,
            now=at,
        )
        return claim
    try:
        receipt = fetch(job.request)
        if receipt.request != job.request or receipt.observed_at < at:
            raise CollectionJobError(
                "fetch returned a different request or stale observation"
            )
        import hashlib

        digest = "sha256:" + hashlib.sha256(receipt.raw_payload).hexdigest()
        previous = store.receipt_for_digest(
            digest, now=clock(), source_policy=source_policy, permit=permit
        )
        if previous is not None:
            receipt = replace(receipt, observed_at=previous.observed_at)
        batch = ingest_kis_daily(
            receipt,
            calendar=job.calendar,
            store=store,
            source_policy=source_policy,
            permit=permit,
            now=clock(),
        )
        archive = archive_kis_batch(
            batch, store=store, source_policy=source_policy, permit=permit, now=clock()
        )
        return store.finish(
            claim,
            at=clock(),
            archive=archive,
            source_policy=source_policy,
            permit=permit,
        )
    except Exception:
        with suppress(CollectionJobError):
            store.finish(claim, at=clock())
        raise
