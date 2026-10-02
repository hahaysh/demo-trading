"""Retained KIS snapshot manifests and a policy-gated artifact resolver."""

import json
from datetime import datetime

from ats.data.artifacts import ArtifactResolutionError, LocalArtifactResolver
from ats.data.asof import RevisionOrder
from ats.data.collection import restore_kis_batch
from ats.data.storage import (
    LocalPayloadStore,
    PayloadManifest,
    StoragePermit,
    StoredPayload,
)
from ats.domain.data import (
    CredibilityTier,
    DataSnapshot,
    PointInTimeRecord,
    UniverseMembershipManifest,
)
from ats.domain.policy import SourceAllowlist
from ats.domain.strategy import ArtifactRef, FrozenModel
from ats.domain.universe import UniverseMembershipArtifact


class NormalizedArtifact(FrozenModel):
    record: PointInTimeRecord
    payload: StoredPayload
    batch: StoredPayload


class DatasetManifest(FrozenModel):
    snapshot: DataSnapshot
    prices: tuple[NormalizedArtifact, ...]
    revision_orders: tuple[RevisionOrder, ...]
    archives: tuple[StoredPayload, ...]

    def revisions_at(self, at: datetime) -> tuple[RevisionOrder, ...]:
        latest: dict[tuple[str, str], RevisionOrder] = {}
        for order in self.revision_orders:
            if order.observed_at <= at:
                latest[(order.source_id, order.source_item_id)] = order
        return tuple(latest[key] for key in sorted(latest))


class StoredArtifactResolver(LocalArtifactResolver):
    def __init__(
        self,
        store: LocalPayloadStore,
        manifest: DatasetManifest,
        *,
        source_policy: SourceAllowlist,
        permit: StoragePermit,
        now: datetime,
    ) -> None:
        self.store = store
        self.manifest = DatasetManifest.model_validate(manifest.model_dump())
        self.policy = source_policy
        self.permit = permit
        self.now = now

    def read_digest(self, digest: str) -> bytes:
        receipt = self.store.receipt_for_digest(
            digest, now=self.now, source_policy=self.policy, permit=self.permit
        )
        if receipt is None:
            raise ArtifactResolutionError("retained artifact missing")
        return self.store.read(
            receipt, now=self.now, source_policy=self.policy, permit=self.permit
        )

    def read_normalized_payload(self, record: PointInTimeRecord) -> bytes:
        binding = next(
            (price for price in self.manifest.prices if price.record == record), None
        )
        if binding is None:
            raise ArtifactResolutionError("price record is not in the pinned manifest")
        batch = restore_kis_batch(
            binding.batch,
            store=self.store,
            source_policy=self.policy,
            permit=self.permit,
            now=self.now,
        )
        self.read_raw_payload(record)
        payload = self.store.read(
            binding.payload, now=self.now, source_policy=self.policy, permit=self.permit
        )
        if (
            batch.raw.digest != record.raw_payload_digest
            or batch.raw.observed_at != record.observed_at
            or not any(
                bar.price.model_dump_json().encode() == payload
                for bar in batch.normalization.bars
            )
        ):
            raise ArtifactResolutionError(
                "normalized price does not match its raw batch"
            )
        return payload


def publish_kis_snapshot(
    store: LocalPayloadStore,
    names: tuple[str, ...],
    *,
    snapshot_id: str,
    universe: UniverseMembershipArtifact,
    observed_through: datetime,
    now: datetime,
    source_policy: SourceAllowlist,
    permit: StoragePermit,
) -> PayloadManifest:
    if not names or len(names) != len(set(names)):
        raise ValueError("snapshot needs unique batch manifests")
    universe = UniverseMembershipArtifact.model_validate(universe.model_dump())
    archives = tuple(
        store.resolve_manifest(
            name, now=now, source_policy=source_policy, permit=permit
        ).payload
        for name in names
    )
    batches = tuple(
        restore_kis_batch(
            archive, store=store, source_policy=source_policy, permit=permit, now=now
        )
        for archive in archives
    )
    if any(batch.raw.observed_at > observed_through for batch in batches):
        raise ValueError("batch observed after snapshot freeze")
    first_observation = min(batch.raw.observed_at for batch in batches)
    source = next(
        entry for entry in source_policy.sources if entry.source_id == permit.source_id
    )

    def put(payload: bytes, observed: datetime) -> StoredPayload:
        return store.put(
            payload,
            observed_at=observed,
            now=now,
            source_policy=source_policy,
            permit=permit,
        )

    universe_payload = put(universe.model_dump_json().encode(), first_observation)
    prices: list[NormalizedArtifact] = []
    for archive, batch in zip(archives, batches, strict=True):
        for bar in batch.normalization.bars:
            price = bar.price
            universe.require_member(price.instrument_id, at=price.session_close)
            normalized = put(price.model_dump_json().encode(), batch.raw.observed_at)
            record = PointInTimeRecord(
                source_id=permit.source_id,
                source_item_id=f"{batch.normalization.request.symbol}-{price.session:%Y%m%d}",
                revision="r-" + batch.raw.digest[7:],
                observed_at=batch.raw.observed_at,
                effective_at=price.session_close,
                instrument_id=price.instrument_id,
                rights_class=source.rights.classification,
                credibility_tier=CredibilityTier.PRIMARY,
                content_hash=price.content_digest(),
                raw_payload_digest=batch.raw.digest,
            )
            prices.append(
                NormalizedArtifact(record=record, payload=normalized, batch=archive)
            )
    prices.sort(
        key=lambda binding: (binding.record.source_item_id, binding.record.observed_at)
    )
    snapshot = DataSnapshot(
        snapshot_id=snapshot_id,
        observed_through=observed_through,
        created_at=now,
        universe_membership=UniverseMembershipManifest(
            manifest_id=universe.manifest_id,
            as_of=universe.as_of,
            digest=universe_payload.digest,
        ),
        records=tuple(binding.record for binding in prices),
    )
    groups: dict[str, list[PointInTimeRecord]] = {}
    orders: list[RevisionOrder] = []
    for record in snapshot.records:
        history = groups.setdefault(record.source_item_id, [])
        if history and history[-1].observed_at == record.observed_at:
            raise ValueError("same-time revisions are ambiguous")
        history.append(record)
        if len(history) < 2:
            continue
        evidence = put(
            json.dumps(
                [item.model_dump(mode="json") for item in history], sort_keys=True
            ).encode(),
            first_observation,
        )
        orders.append(
            RevisionOrder(
                source_id=record.source_id,
                source_item_id=record.source_item_id,
                revisions=tuple(item.revision for item in history),
                observed_at=record.observed_at,
                evidence=ArtifactRef(
                    artifact_id=f"revision-{len(orders)}",
                    version="1",
                    digest=evidence.digest,
                ),
            )
        )
    manifest = DatasetManifest(
        snapshot=snapshot,
        prices=tuple(prices),
        revision_orders=tuple(orders),
        archives=archives,
    )
    retained = put(manifest.model_dump_json().encode(), first_observation)
    return store.bind_manifest(
        PayloadManifest(name=snapshot_id, payload=retained),
        now=now,
        source_policy=source_policy,
        permit=permit,
    )


def restore_dataset(
    store: LocalPayloadStore,
    name: str,
    *,
    source_policy: SourceAllowlist,
    permit: StoragePermit,
    now: datetime,
) -> tuple[DatasetManifest, StoredArtifactResolver]:
    reference = store.resolve_manifest(
        name, now=now, source_policy=source_policy, permit=permit
    )
    payload = store.read(
        reference.payload, now=now, source_policy=source_policy, permit=permit
    )
    manifest = DatasetManifest.model_validate_json(payload)
    if (
        manifest.snapshot.snapshot_id != name
        or tuple(price.record for price in manifest.prices) != manifest.snapshot.records
    ):
        raise ValueError("dataset manifest binding mismatch")
    resolver = StoredArtifactResolver(
        store, manifest, source_policy=source_policy, permit=permit, now=now
    )
    resolver.read_universe_membership(manifest.snapshot.universe_membership)
    for binding in manifest.prices:
        resolver.read_normalized_payload(binding.record)
    for order in manifest.revision_orders:
        resolver.read_revision_evidence(order)
    return manifest, resolver
