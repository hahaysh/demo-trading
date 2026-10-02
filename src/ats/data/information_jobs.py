"""Durable information schedules, fenced leases and retained revision indexes."""

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from ats.data.information import (
    InformationBatch,
    InformationClient,
    InformationError,
    InformationSource,
    TextObservation,
)
from ats.data.storage import (
    LocalPayloadStore,
    PayloadManifest,
    StoragePermit,
    StoredPayload,
)
from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.domain.prices import SEOUL
from ats.domain.strategy import FrozenModel, Identifier, Sha256Digest


class InformationSchedule(FrozenModel):
    schedule_id: Identifier
    source: InformationSource
    policy_digest: Sha256Digest
    permit_digest: Sha256Digest
    starts_at: AwareDatetime
    cadence_seconds: Annotated[int, Field(strict=True, ge=60, le=604800)] = 86400
    overlap_seconds: Annotated[int, Field(strict=True, ge=0, le=604800)] = 86400
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=10)] = 3


class InformationRun(FrozenModel):
    run_id: Identifier
    schedule: InformationSchedule
    window_start: AwareDatetime
    window_end: AwareDatetime
    updated_at: AwareDatetime
    status: Literal["RUNNING", "FAILED", "SUCCEEDED", "GAP"]
    attempts: int
    lease: UUID | None
    lease_until: AwareDatetime | None
    archive: StoredPayload | None = None


class InformationStore(LocalPayloadStore):
    def claim(
        self, schedule: InformationSchedule, *, at: datetime
    ) -> InformationRun | None:
        schedule = InformationSchedule.model_validate(schedule.model_dump())
        if at.tzinfo is None or at.utcoffset() is None or at < schedule.starts_at:
            raise InformationError("schedule clock is invalid or before activation")
        with self._connection(create=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS info_schedules (id TEXT PRIMARY KEY, definition TEXT NOT NULL, watermark TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS info_runs (id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, record TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS info_index (source_id TEXT NOT NULL, item_id TEXT NOT NULL, revision TEXT NOT NULL, observed_at TEXT NOT NULL, digest TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(source_id,item_id,revision), FOREIGN KEY(source_id,digest) REFERENCES payloads(source_id,digest) ON DELETE CASCADE)"
            )
            row = connection.execute(
                "SELECT definition,watermark FROM info_schedules WHERE id=?",
                (schedule.schedule_id,),
            ).fetchone()
            if row is not None and row["definition"] != schedule.model_dump_json():
                raise InformationError("schedule is immutable; use a new version")
            if row is None:
                connection.execute(
                    "INSERT INTO info_schedules VALUES (?, ?, NULL)",
                    (schedule.schedule_id, schedule.model_dump_json()),
                )
            watermark = (
                datetime.fromisoformat(row["watermark"])
                if row is not None and row["watermark"]
                else schedule.starts_at
            )
            end = watermark + timedelta(seconds=schedule.cadence_seconds)
            if end > at:
                return None
            start = max(
                schedule.starts_at,
                watermark - timedelta(seconds=schedule.overlap_seconds),
            )
            run_id = (
                "info-"
                + evidence_digest(schedule)[7:23]
                + "-"
                + end.astimezone(UTC).strftime("%Y%m%dt%H%M%S")
            )
            previous_row = connection.execute(
                "SELECT record FROM info_runs WHERE id=?", (run_id,)
            ).fetchone()
            attempts = 0
            if previous_row is not None:
                previous = InformationRun.model_validate_json(previous_row["record"])
                if (
                    previous.updated_at > at
                    or previous.lease_until is not None
                    and at < previous.lease_until
                ):
                    raise InformationError(
                        "information run lease is active or clock regressed"
                    )
                if previous.status == "SUCCEEDED":
                    raise InformationError("inconsistent information checkpoint")
                attempts = previous.attempts
            if attempts >= schedule.max_attempts:
                raise InformationError("information retry budget exhausted")
            run = InformationRun(
                run_id=run_id,
                schedule=schedule,
                window_start=start,
                window_end=end,
                updated_at=at,
                status="RUNNING",
                attempts=attempts + 1,
                lease=uuid4(),
                lease_until=at + timedelta(seconds=300),
            )
            connection.execute(
                "INSERT INTO info_runs VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                (run_id, schedule.schedule_id, run.model_dump_json()),
            )
            return run

    def finish(
        self,
        run: InformationRun,
        *,
        at: datetime,
        batch: InformationBatch | None,
        policy: SourceAllowlist,
        permit: StoragePermit,
    ) -> InformationRun:
        self.check_authorization(now=at, source_policy=policy, permit=permit)
        retained: list[tuple[TextObservation, StoredPayload]] = []
        archive: StoredPayload | None = None
        status: Literal["FAILED", "SUCCEEDED", "GAP"] = "FAILED"
        if batch is not None:
            batch = InformationBatch.model_validate(batch.model_dump())
            if (
                batch.source_digest != evidence_digest(run.schedule.source)
                or batch.observed_at > at
            ):
                raise InformationError(
                    "information result not bound to scheduled source"
                )
            if batch.coverage == "REQUEST_RANGE" and (
                batch.requested_start != run.window_start.astimezone(SEOUL).date()
                or batch.requested_end != run.window_end.astimezone(SEOUL).date()
            ):
                raise InformationError(
                    "information request window differs from scheduled window"
                )
            for page in batch.pages:
                self.read(page, now=at, source_policy=policy, permit=permit)
            status = "SUCCEEDED"
            if batch.coverage in ("FEED_WINDOW", "SEARCH_WINDOW"):
                published = [
                    item.published_at
                    for item in batch.observations
                    if item.published_at is not None
                ]
                if not published or min(published) > run.window_start:
                    status = "GAP"
            for item in batch.observations:
                item_receipt = self.put(
                    item.model_dump_json().encode(),
                    observed_at=item.observed_at,
                    now=at,
                    source_policy=policy,
                    permit=permit,
                )
                retained.append((item, item_receipt))
            observed = min(page.observed_at for page in batch.pages)
            archive = self.put(
                batch.model_dump_json().encode(),
                observed_at=observed,
                now=at,
                source_policy=policy,
                permit=permit,
            )
        completed = InformationRun.model_validate(
            {
                **run.model_dump(),
                "status": status,
                "archive": archive,
                "lease": None,
                "lease_until": None,
                "updated_at": at,
            }
        )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record FROM info_runs WHERE id=?", (run.run_id,)
            ).fetchone()
            if (
                row is None
                or InformationRun.model_validate_json(row["record"]) != run
                or run.lease_until is None
                or not run.updated_at <= at < run.lease_until
            ):
                raise InformationError("stale information worker cannot publish")
            for item, receipt in retained:
                previous = connection.execute(
                    "SELECT observed_at FROM info_index WHERE source_id=? AND item_id=? AND revision=?",
                    (item.source_id, item.item_id, item.revision),
                ).fetchone()
                if (
                    previous is not None
                    and datetime.fromisoformat(previous["observed_at"])
                    > item.observed_at
                ):
                    raise InformationError("information observation clock regressed")
                connection.execute(
                    "INSERT OR IGNORE INTO info_index VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.source_id,
                        item.item_id,
                        item.revision,
                        item.observed_at.astimezone(UTC).isoformat(),
                        receipt.digest,
                        receipt.model_dump_json(),
                    ),
                )
            if archive is not None:
                manifest = PayloadManifest(name=run.run_id, payload=archive)
                connection.execute(
                    "INSERT INTO manifests VALUES (?, ?, ?, ?) ON CONFLICT(name) DO NOTHING",
                    (
                        manifest.name,
                        archive.source_id,
                        archive.digest,
                        manifest.model_dump_json(),
                    ),
                )
            connection.execute(
                "UPDATE info_runs SET record=? WHERE id=?",
                (completed.model_dump_json(), run.run_id),
            )
            if status == "SUCCEEDED":
                connection.execute(
                    "UPDATE info_schedules SET watermark=? WHERE id=?",
                    (
                        run.window_end.astimezone(UTC).isoformat(),
                        run.schedule.schedule_id,
                    ),
                )
        return completed

    def observations(
        self,
        *,
        at: datetime,
        now: datetime,
        policy: SourceAllowlist,
        permit: StoragePermit,
    ) -> tuple[TextObservation, ...]:
        self.check_authorization(now=now, source_policy=policy, permit=permit)
        if at.tzinfo is None or at.utcoffset() is None or at > now:
            raise InformationError("invalid information cutoff")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT receipt FROM info_index WHERE source_id=? AND observed_at<=? ORDER BY observed_at,item_id,revision",
                (permit.source_id, at.astimezone(UTC).isoformat()),
            ).fetchall()
        result: list[TextObservation] = []
        for row in rows:
            receipt = StoredPayload.model_validate_json(row["receipt"])
            item = TextObservation.model_validate_json(
                self.read(receipt, now=now, source_policy=policy, permit=permit)
            )
            self.read(item.raw, now=now, source_policy=policy, permit=permit)
            if item.origin_raw is not None:
                self.read(item.origin_raw, now=now, source_policy=policy, permit=permit)
            if item.observed_at > at:
                raise InformationError("information index availability mismatch")
            result.append(item)
        return tuple(result)

    def status(self) -> tuple[InformationRun, ...]:
        with self._connection() as connection:
            return tuple(
                InformationRun.model_validate_json(row["record"])
                for row in connection.execute(
                    "SELECT record FROM info_runs ORDER BY id"
                )
            )


def run_information_schedule(
    store: InformationStore,
    schedule: InformationSchedule,
    client: InformationClient,
    collect: Callable[[datetime, datetime], InformationBatch],
) -> InformationRun | None:
    client.authorize()
    if (
        schedule.source != client.source
        or schedule.policy_digest != evidence_digest(client.policy)
        or schedule.permit_digest != evidence_digest(client.permit)
        or client.store.database != store.database
    ):
        raise InformationError("scheduled source authority mismatch")
    run = store.claim(schedule, at=client.clock())
    if run is None:
        return None
    try:
        batch = collect(run.window_start, run.window_end)
        return store.finish(
            run,
            at=client.clock(),
            batch=batch,
            policy=client.policy,
            permit=client.permit,
        )
    except Exception:
        with suppress(InformationError):
            store.finish(
                run,
                at=client.clock(),
                batch=None,
                policy=client.policy,
                permit=client.permit,
            )
        raise
