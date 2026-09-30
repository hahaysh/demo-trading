import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ats.data.artifacts import ArtifactResolutionError, LocalArtifactResolver
from ats.data.asof import AsOfSelectionError, RevisionOrder
from ats.data.bundle import (
    DecisionInputBundle,
    UnscopedRecordPolicy,
    build_decision_inputs,
)
from ats.domain.data import DataSnapshot, PointInTimeRecord, UniverseMembershipManifest
from ats.domain.strategy import ArtifactRef
from ats.domain.universe import UniverseMember, UniverseMembershipArtifact

FREEZE = datetime(2026, 9, 30, 7, tzinfo=UTC)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _store(root: Path, payload: bytes) -> str:
    digest = _digest(payload)
    path = root / "sha256" / digest.removeprefix("sha256:")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return digest


@pytest.mark.parametrize("payload", [b"", b"synthetic disclosure\r\n", b"\x00\xff\xfe"])
def test_reads_exact_bytes_without_modifying_store(
    tmp_path: Path, payload: bytes
) -> None:
    digest = _store(tmp_path, payload)
    resolver = LocalArtifactResolver(tmp_path)
    reference = ArtifactRef(artifact_id="raw-source", version="1.0.0", digest=digest)
    before = sorted(tmp_path.rglob("*"))
    assert resolver.read_digest(digest) == payload
    assert resolver.read_artifact(reference) == payload
    assert sorted(tmp_path.rglob("*")) == before


def test_missing_and_tampered_artifacts_fail_without_fallback(tmp_path: Path) -> None:
    resolver = LocalArtifactResolver(tmp_path)
    digest = _digest(b"expected")
    with pytest.raises(ArtifactResolutionError, match="missing or unreadable"):
        resolver.read_digest(digest)
    _store(tmp_path, b"unrelated older artifact")
    path = tmp_path / "sha256" / digest.removeprefix("sha256:")
    path.write_bytes(b"corrupted")
    with pytest.raises(ArtifactResolutionError, match="digest mismatch"):
        resolver.read_digest(digest)
    assert path.read_bytes() == b"corrupted"


@pytest.mark.parametrize(
    "digest",
    [
        "../outside",
        "sha256:../outside",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 64 + "\n",
        "https://example.invalid/blob",
        "C:\\secret",
        "sha512:" + "a" * 64,
    ],
)
def test_digest_cannot_supply_a_path(tmp_path: Path, digest: str) -> None:
    with pytest.raises(ArtifactResolutionError, match="invalid SHA-256"):
        LocalArtifactResolver(tmp_path).read_digest(digest)


def test_size_limit_boundary_and_no_truncation(tmp_path: Path) -> None:
    digest = _store(tmp_path, b"four")
    assert LocalArtifactResolver(tmp_path, max_bytes=4).read_digest(digest) == b"four"
    with pytest.raises(ArtifactResolutionError, match="byte limit"):
        LocalArtifactResolver(tmp_path, max_bytes=3).read_digest(digest)


def test_bounded_read_rejects_growth_after_size_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _store(tmp_path, b"larger than allowed")

    def understated_size(descriptor: int) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFREG, st_size=0)

    monkeypatch.setattr(os, "fstat", understated_size)
    with pytest.raises(ArtifactResolutionError, match="byte limit"):
        LocalArtifactResolver(tmp_path, max_bytes=4).read_digest(digest)


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_size_limit(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        LocalArtifactResolver(tmp_path, max_bytes=limit)


def test_no_cache_hides_later_corruption(tmp_path: Path) -> None:
    digest = _store(tmp_path, b"original")
    resolver = LocalArtifactResolver(tmp_path)
    assert resolver.read_digest(digest) == b"original"
    (tmp_path / "sha256" / digest.removeprefix("sha256:")).write_bytes(b"changed")
    with pytest.raises(ArtifactResolutionError, match="digest mismatch"):
        resolver.read_digest(digest)


def test_directories_and_unavailable_roots_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArtifactResolutionError, match="root"):
        LocalArtifactResolver(tmp_path / "absent")
    regular = tmp_path / "file"
    regular.write_bytes(b"data")
    with pytest.raises(ArtifactResolutionError, match="directory"):
        LocalArtifactResolver(regular)
    digest = _digest(b"directory")
    (tmp_path / "sha256" / digest.removeprefix("sha256:")).mkdir(parents=True)
    with pytest.raises(ArtifactResolutionError, match="regular file"):
        LocalArtifactResolver(tmp_path).read_digest(digest)


def test_copied_reference_is_revalidated(tmp_path: Path) -> None:
    reference = ArtifactRef(
        artifact_id="raw-source", version="1.0.0", digest=_digest(b"data")
    )
    with pytest.raises(ValidationError):
        LocalArtifactResolver(tmp_path).read_artifact(
            reference.model_copy(update={"digest": "../outside"})
        )


@pytest.mark.parametrize("component", ["directory", "file"])
def test_static_symlink_is_rejected(tmp_path: Path, component: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    digest = _store(outside, b"external")
    target = outside / "sha256"
    link = root / "sha256"
    if component == "file":
        link.mkdir()
        link = link / digest.removeprefix("sha256:")
        target = target / digest.removeprefix("sha256:")
    try:
        link.symlink_to(target, target_is_directory=component == "directory")
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(ArtifactResolutionError, match="links and reparse"):
        LocalArtifactResolver(root).read_digest(digest)


def test_reparse_attribute_is_rejected_without_windows_link_privilege(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = LocalArtifactResolver(tmp_path)

    class ReparseInfo:
        st_mode = stat.S_IFDIR
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

    def reparse_stat(path: Path) -> object:
        return ReparseInfo()

    monkeypatch.setattr(Path, "lstat", reparse_stat)
    with pytest.raises(ArtifactResolutionError, match="links and reparse"):
        resolver.read_digest(_digest(b"data"))


def _record(
    digest: str, *, revision: str = "rev-001", observed_at: datetime = FREEZE
) -> PointInTimeRecord:
    return PointInTimeRecord.model_validate(
        {
            "source_id": "test-source",
            "source_item_id": "test-item",
            "revision": revision,
            "observed_at": observed_at,
            "rights_class": "APPROVED_PUBLIC",
            "credibility_tier": "PRIMARY",
            "content_hash": _digest(b"separate normalized content"),
            "raw_payload_digest": digest,
        }
    )


def _snapshot(*records: PointInTimeRecord) -> DataSnapshot:
    return DataSnapshot(
        snapshot_id="test-snapshot",
        observed_through=FREEZE,
        created_at=FREEZE,
        universe_membership=UniverseMembershipManifest(
            manifest_id="test-universe", as_of=FREEZE, digest=_digest(b"universe")
        ),
        records=records,
    )


def _order(digest: str, *, observed_at: datetime = FREEZE) -> RevisionOrder:
    return RevisionOrder(
        source_id="test-source",
        source_item_id="test-item",
        revisions=("rev-001", "rev-002"),
        observed_at=observed_at,
        evidence=ArtifactRef(
            artifact_id="revision-evidence", version="1.0.0", digest=digest
        ),
    )


def test_verified_selection_checks_raw_bytes_and_revision_evidence(
    tmp_path: Path,
) -> None:
    earlier = _record(_store(tmp_path, b"first raw revision"))
    latest = _record(_store(tmp_path, b"second raw revision"), revision="rev-002")
    order = _order(_store(tmp_path, b"synthetic revision-order report"))
    snapshot = _snapshot(earlier, latest)
    original_digest = snapshot.content_digest()
    resolver = LocalArtifactResolver(tmp_path)
    assert resolver.read_raw_payload(earlier) == b"first raw revision"
    assert resolver.read_revision_evidence(order) == b"synthetic revision-order report"
    assert resolver.select_verified_records_as_of(
        snapshot, at=FREEZE, revision_orders=(order,)
    ) == (latest,)
    assert snapshot.content_digest() == original_digest


@pytest.mark.parametrize("target", ["earlier", "latest", "evidence"])
@pytest.mark.parametrize("failure", ["missing", "tampered"])
def test_any_visible_missing_or_corrupt_artifact_blocks_selection(
    tmp_path: Path,
    target: str,
    failure: str,
) -> None:
    digests = {
        name: _store(tmp_path, name.encode("ascii"))
        for name in ("earlier", "latest", "evidence")
    }
    snapshot = _snapshot(
        _record(digests["earlier"]), _record(digests["latest"], revision="rev-002")
    )
    order = _order(digests["evidence"])
    path = tmp_path / "sha256" / digests[target].removeprefix("sha256:")
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"wrong bytes")
    with pytest.raises(ArtifactResolutionError):
        LocalArtifactResolver(tmp_path).select_verified_records_as_of(
            snapshot, at=FREEZE, revision_orders=(order,)
        )


def test_future_missing_artifacts_do_not_change_past_selection(tmp_path: Path) -> None:
    cutoff = FREEZE - timedelta(minutes=1)
    earlier = _record(_store(tmp_path, b"visible"), observed_at=cutoff)
    later = _record(_digest(b"not stored yet"), revision="rev-002")
    order = _order(_digest(b"future order evidence also missing"))
    resolver = LocalArtifactResolver(tmp_path)
    assert resolver.select_verified_records_as_of(
        _snapshot(earlier, later), at=cutoff, revision_orders=(order,)
    ) == (earlier,)
    with pytest.raises(ArtifactResolutionError):
        resolver.select_verified_records_as_of(
            _snapshot(earlier, later), at=FREEZE, revision_orders=(order,)
        )


def test_selection_errors_precede_artifact_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_read(resolver: LocalArtifactResolver, digest: str) -> bytes:
        pytest.fail("invalid selection must not reach artifact reads")

    monkeypatch.setattr(LocalArtifactResolver, "read_digest", unexpected_read)
    resolver = LocalArtifactResolver(tmp_path)
    snapshot = _snapshot(
        _record(_digest(b"one")), _record(_digest(b"two"), revision="rev-002")
    )
    with pytest.raises(AsOfSelectionError, match="ambiguous"):
        resolver.select_verified_records_as_of(snapshot, at=FREEZE)


def test_unreadable_artifact_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _store(tmp_path, b"protected")

    def deny_open(*args: object, **kwargs: object) -> object:
        raise PermissionError("synthetic access denied")

    monkeypatch.setattr(Path, "open", deny_open)
    with pytest.raises(ArtifactResolutionError, match="missing or unreadable"):
        LocalArtifactResolver(tmp_path).read_digest(digest)


def _member(**changes: object) -> UniverseMember:
    return UniverseMember.model_validate(
        {
            "instrument_id": "krx-005930",
            "asset_class": "EQUITY",
            "membership_basis": "KOSPI_200",
            "observed_at": FREEZE - timedelta(days=2),
            "effective_from": FREEZE - timedelta(days=1),
            "effective_until": FREEZE + timedelta(days=1),
            "evidence": ArtifactRef(
                artifact_id="membership-source",
                version="1.0.0",
                digest=_digest(b"membership evidence"),
            ),
            **changes,
        }
    )


def _universe(*members: UniverseMember) -> UniverseMembershipArtifact:
    return UniverseMembershipArtifact(
        manifest_id="test-universe", as_of=FREEZE, members=members
    )


def test_membership_separates_knowledge_from_economic_effect() -> None:
    future = _member(effective_from=FREEZE + timedelta(hours=1), effective_until=None)
    assert _universe(future).members_at(FREEZE) == ()
    late = _member(observed_at=FREEZE)
    artifact = _universe(late)
    with pytest.raises(ValueError, match="not a known effective"):
        artifact.require_member(
            late.instrument_id, at=FREEZE - timedelta(microseconds=1)
        )
    assert artifact.require_member(late.instrument_id, at=FREEZE) == late


def test_membership_start_is_inclusive_and_end_is_exclusive() -> None:
    member = _member(effective_from=FREEZE - timedelta(hours=1), effective_until=FREEZE)
    artifact = _universe(member)
    assert artifact.members_at(member.effective_from) == (member,)
    assert artifact.members_at(FREEZE - timedelta(microseconds=1)) == (member,)
    assert artifact.members_at(FREEZE) == ()
    next_member = _member(
        observed_at=FREEZE, effective_from=FREEZE, effective_until=None
    )
    assert _universe(next_member, member).members_at(FREEZE) == (next_member,)


@pytest.mark.parametrize(
    "changes",
    [
        {"effective_until": FREEZE - timedelta(days=1)},
        {"effective_until": FREEZE - timedelta(days=2)},
        {"observed_at": datetime(2026, 9, 30)},
        {"membership_basis": "ETF_ALLOWLIST"},
        {"asset_class": "ETF"},
        {"asset_class": "FUTURE"},
    ],
)
def test_invalid_membership_declarations_are_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _member(**changes)


@pytest.mark.parametrize("open_ended", [True, False])
def test_overlapping_memberships_fail_closed(open_ended: bool) -> None:
    first = _member(effective_until=None if open_ended else FREEZE)
    overlapping = _member(effective_from=FREEZE - timedelta(hours=1))
    with pytest.raises(ValidationError, match="overlapping"):
        _universe(first, overlapping)
    with pytest.raises(ValidationError, match="overlapping"):
        _universe(first, first)


def test_manifest_rejects_future_observations_and_cutoff_extrapolation() -> None:
    with pytest.raises(ValidationError, match="observation exceeds"):
        _universe(_member(observed_at=FREEZE + timedelta(seconds=1)))
    for cutoff in (datetime(2026, 9, 30), FREEZE + timedelta(seconds=1)):
        with pytest.raises(ValueError):
            _universe(_member()).members_at(cutoff)


def test_membership_round_trip_sorting_and_timezone_equivalence() -> None:
    equity = _member()
    etf = _member(
        instrument_id="krx-069500", asset_class="ETF", membership_basis="ETF_ALLOWLIST"
    )
    artifact = _universe(etf, equity)
    rebuilt = UniverseMembershipArtifact.model_validate_json(artifact.model_dump_json())
    assert rebuilt == artifact
    assert rebuilt.members_at(FREEZE) == (equity, etf)
    assert artifact.members_at(FREEZE.astimezone(timezone(timedelta(hours=9)))) == (
        equity,
        etf,
    )
    with pytest.raises(ValidationError):
        artifact.members[0].__setattr__("instrument_id", "modified")
    with pytest.raises(ValueError, match="not a known effective"):
        _universe().require_member(equity.instrument_id, at=FREEZE)


def test_membership_selection_revalidates_unchecked_copies() -> None:
    member = _member()
    artifact = _universe(member).model_copy(update={"members": (member, member)})
    with pytest.raises(ValidationError, match="overlapping"):
        artifact.members_at(FREEZE)


def _universe_reference(
    root: Path, artifact: UniverseMembershipArtifact
) -> UniverseMembershipManifest:
    return UniverseMembershipManifest(
        manifest_id=artifact.manifest_id,
        as_of=artifact.as_of,
        digest=_store(root, artifact.model_dump_json().encode("utf-8")),
    )


def test_resolver_binds_verified_membership_bytes_to_snapshot(tmp_path: Path) -> None:
    member = _member()
    artifact = _universe(member)
    reference = _universe_reference(tmp_path, artifact)
    snapshot = DataSnapshot.model_validate(
        {**_snapshot().model_dump(), "universe_membership": reference}
    )
    resolver = LocalArtifactResolver(tmp_path)
    assert resolver.read_universe_membership(reference) == artifact
    before = snapshot.content_digest()
    assert resolver.select_universe_members_as_of(snapshot, at=FREEZE) == (member,)
    assert (
        resolver.select_universe_members_as_of(
            snapshot, at=member.observed_at - timedelta(seconds=1)
        )
        == ()
    )
    assert snapshot.content_digest() == before


@pytest.mark.parametrize("field", ["manifest_id", "as_of"])
def test_universe_reference_metadata_must_match_payload(
    tmp_path: Path, field: str
) -> None:
    reference = _universe_reference(tmp_path, _universe(_member()))
    changed = UniverseMembershipManifest.model_validate(
        {
            **reference.model_dump(),
            field: "different-universe"
            if field == "manifest_id"
            else FREEZE - timedelta(seconds=1),
        }
    )
    with pytest.raises(ArtifactResolutionError, match="reference mismatch"):
        LocalArtifactResolver(tmp_path).read_universe_membership(changed)


@pytest.mark.parametrize("failure", ["missing", "tampered"])
def test_universe_resolver_rejects_missing_or_changed_bytes(
    tmp_path: Path, failure: str
) -> None:
    reference = _universe_reference(tmp_path, _universe(_member()))
    path = tmp_path / "sha256" / reference.digest.removeprefix("sha256:")
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"{}")
    with pytest.raises(ArtifactResolutionError):
        LocalArtifactResolver(tmp_path).read_universe_membership(reference)


@pytest.mark.parametrize("payload", [b"not json", b"{}", b"[]", b"\xff"])
def test_digest_valid_but_invalid_membership_json_is_rejected(
    tmp_path: Path, payload: bytes
) -> None:
    reference = UniverseMembershipManifest(
        manifest_id="test-universe", as_of=FREEZE, digest=_store(tmp_path, payload)
    )
    with pytest.raises(ValidationError):
        LocalArtifactResolver(tmp_path).read_universe_membership(reference)


def test_universe_cannot_extrapolate_past_manifest_or_snapshot(tmp_path: Path) -> None:
    artifact = UniverseMembershipArtifact.model_validate(
        {**_universe(_member()).model_dump(), "as_of": FREEZE - timedelta(hours=1)}
    )
    reference = _universe_reference(tmp_path, artifact)
    snapshot = DataSnapshot.model_validate(
        {**_snapshot().model_dump(), "universe_membership": reference}
    )
    resolver = LocalArtifactResolver(tmp_path)
    with pytest.raises(ValueError, match="manifest horizon"):
        resolver.select_universe_members_as_of(snapshot, at=FREEZE)
    with pytest.raises(ArtifactResolutionError, match="snapshot freeze"):
        resolver.select_universe_members_as_of(
            snapshot, at=FREEZE + timedelta(seconds=1)
        )
    with pytest.raises(ArtifactResolutionError, match="timezone-aware"):
        resolver.select_universe_members_as_of(snapshot, at=datetime(2026, 9, 30))


def _bundle_snapshot(
    root: Path, members: tuple[UniverseMember, ...] | None = None
) -> DataSnapshot:
    records: list[PointInTimeRecord] = []
    for item, instrument in [
        ("inside", "krx-005930"),
        ("outside", "krx-000660"),
        ("unscoped", None),
    ]:
        records.append(
            PointInTimeRecord.model_validate(
                {
                    **_record(_store(root, item.encode())).model_dump(),
                    "source_item_id": item,
                    "instrument_id": instrument,
                }
            )
        )
    reference = _universe_reference(
        root, _universe(*(members if members is not None else (_member(),)))
    )
    return DataSnapshot.model_validate(
        {**_snapshot(*records).model_dump(), "universe_membership": reference}
    )


def test_bundle_combines_verified_inputs_and_explicit_exclusions(
    tmp_path: Path,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    before = snapshot.model_dump_json()
    bundle = build_decision_inputs(LocalArtifactResolver(tmp_path), snapshot, at=FREEZE)
    assert [record.source_item_id for record in bundle.records] == ["inside"]
    assert [(record.source_item_id, record.reason) for record in bundle.excluded] == [
        ("outside", "OUTSIDE_UNIVERSE"),
        ("unscoped", "UNSCOPED"),
    ]
    assert bundle.snapshot.digest == snapshot.content_digest()
    assert bundle.universe == snapshot.universe_membership
    assert bundle.members == (_member(),)
    assert snapshot.model_dump_json() == before
    rebuilt = DecisionInputBundle.model_validate_json(bundle.model_dump_json())
    assert rebuilt == bundle
    assert rebuilt.content_digest() == bundle.content_digest()
    with pytest.raises(ValidationError):
        bundle.__setattr__("records", ())


def test_unscoped_inclusion_is_explicit_and_changes_bundle_identity(
    tmp_path: Path,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    resolver = LocalArtifactResolver(tmp_path)
    default = build_decision_inputs(resolver, snapshot, at=FREEZE)
    included = build_decision_inputs(
        resolver, snapshot, at=FREEZE, unscoped_policy=UnscopedRecordPolicy.INCLUDE
    )
    assert [record.source_item_id for record in included.records] == [
        "inside",
        "unscoped",
    ]
    assert len(included.excluded) == 1
    assert included.content_digest() != default.content_digest()


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_at": FREEZE},
        {"effective_from": FREEZE, "effective_until": None},
        {"effective_until": FREEZE - timedelta(hours=1)},
    ],
)
def test_ineligible_membership_cannot_admit_instrument_record(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    cutoff = FREEZE - timedelta(minutes=1)
    snapshot = _bundle_snapshot(tmp_path, (_member(**changes),))
    records = tuple(
        PointInTimeRecord.model_validate({**record.model_dump(), "observed_at": cutoff})
        for record in snapshot.records
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": records}
    )
    bundle = build_decision_inputs(LocalArtifactResolver(tmp_path), snapshot, at=cutoff)
    assert bundle.records == ()
    assert any(
        item.source_item_id == "inside" and item.reason == "OUTSIDE_UNIVERSE"
        for item in bundle.excluded
    )


def test_empty_universe_does_not_admit_instruments(tmp_path: Path) -> None:
    bundle = build_decision_inputs(
        LocalArtifactResolver(tmp_path), _bundle_snapshot(tmp_path, ()), at=FREEZE
    )
    assert bundle.members == bundle.records == ()
    assert len(bundle.excluded) == 3


@pytest.mark.parametrize("target", ["inside", "outside", "unscoped", "universe"])
def test_corrupt_visible_inputs_cannot_be_hidden_by_filtering(
    tmp_path: Path, target: str
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    digest = (
        snapshot.universe_membership.digest
        if target == "universe"
        else next(
            record.raw_payload_digest
            for record in snapshot.records
            if record.source_item_id == target
        )
    )
    (tmp_path / "sha256" / digest.removeprefix("sha256:")).write_bytes(b"tampered")
    with pytest.raises(ArtifactResolutionError):
        build_decision_inputs(LocalArtifactResolver(tmp_path), snapshot, at=FREEZE)


def test_bundle_binds_applied_revision_order_and_omits_future_order(
    tmp_path: Path,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    original = snapshot.records[0]
    latest = PointInTimeRecord.model_validate(
        {
            **original.model_dump(),
            "revision": "rev-002",
            "raw_payload_digest": _store(tmp_path, b"updated"),
        }
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": (*snapshot.records, latest)}
    )
    order = RevisionOrder.model_validate(
        {
            **_order(_store(tmp_path, b"order evidence")).model_dump(),
            "source_item_id": original.source_item_id,
        }
    )
    future_order = RevisionOrder.model_validate(
        {
            **order.model_dump(),
            "source_item_id": "future-item",
            "observed_at": FREEZE + timedelta(days=1),
            "evidence": ArtifactRef(
                artifact_id="future-evidence", version="1", digest=_digest(b"absent")
            ),
        }
    )
    resolver = LocalArtifactResolver(tmp_path)
    with pytest.raises(AsOfSelectionError):
        build_decision_inputs(resolver, snapshot, at=FREEZE)
    bundle = build_decision_inputs(
        resolver, snapshot, at=FREEZE, revision_orders=(future_order, order)
    )
    assert bundle.records == (latest,)
    assert bundle.revision_orders == (order,)
    changed_order = RevisionOrder.model_validate(
        {**order.model_dump(), "revisions": ("rev-002", "rev-001")}
    )
    changed = build_decision_inputs(
        resolver, snapshot, at=FREEZE, revision_orders=(changed_order,)
    )
    assert changed.records == (original,)
    assert changed.content_digest() != bundle.content_digest()


def test_bundle_determinism_and_normalized_cutoff(tmp_path: Path) -> None:
    resolver = LocalArtifactResolver(tmp_path)
    snapshot = _bundle_snapshot(tmp_path)
    first = build_decision_inputs(resolver, snapshot, at=FREEZE)
    second = build_decision_inputs(
        resolver, snapshot, at=FREEZE.astimezone(timezone(timedelta(hours=9)))
    )
    assert first.content_digest() == second.content_digest()
    assert first == second


def test_bundle_rejects_unscoped_and_outside_records_on_construction(
    tmp_path: Path,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    bundle = build_decision_inputs(LocalArtifactResolver(tmp_path), snapshot, at=FREEZE)
    for record in snapshot.records[1:]:
        payload = {**bundle.model_dump(), "records": (record,), "excluded": ()}
        with pytest.raises(ValidationError):
            DecisionInputBundle.model_validate(payload)


def test_bundle_rejects_invalid_cutoff_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _bundle_snapshot(tmp_path)

    def unexpected_read(resolver: LocalArtifactResolver, digest: str) -> bytes:
        pytest.fail("invalid cutoff must not perform reads")

    monkeypatch.setattr(LocalArtifactResolver, "read_digest", unexpected_read)
    for cutoff in (datetime(2026, 9, 30), FREEZE + timedelta(seconds=1)):
        with pytest.raises(ValueError):
            build_decision_inputs(LocalArtifactResolver(tmp_path), snapshot, at=cutoff)


def test_bundle_does_not_read_future_revision_bytes(tmp_path: Path) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    cutoff = FREEZE - timedelta(minutes=1)
    visible = tuple(
        PointInTimeRecord.model_validate({**record.model_dump(), "observed_at": cutoff})
        for record in snapshot.records
    )
    later = PointInTimeRecord.model_validate(
        {
            **snapshot.records[0].model_dump(),
            "revision": "rev-002",
            "raw_payload_digest": _digest(b"future missing bytes"),
        }
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": (*visible, later)}
    )
    resolver = LocalArtifactResolver(tmp_path)
    assert build_decision_inputs(resolver, snapshot, at=cutoff).records == (visible[0],)
    with pytest.raises(AsOfSelectionError, match="ambiguous"):
        build_decision_inputs(resolver, snapshot, at=FREEZE)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_member",
        "duplicate_record",
        "duplicate_exclusion",
        "late_record",
        "late_member",
        "late_order",
        "late_cutoff",
    ],
)
def test_bundle_rejects_invalid_structural_receipts(tmp_path: Path, case: str) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    bundle = build_decision_inputs(LocalArtifactResolver(tmp_path), snapshot, at=FREEZE)
    payload = bundle.model_dump()
    if case == "duplicate_member":
        payload["members"] = (*bundle.members, bundle.members[0])
    elif case == "duplicate_record":
        payload["records"] = (*bundle.records, bundle.records[0])
    elif case == "duplicate_exclusion":
        payload["excluded"] = (*bundle.excluded, bundle.excluded[0])
    elif case == "late_record":
        payload["records"] = (
            {
                **bundle.records[0].model_dump(),
                "observed_at": FREEZE + timedelta(seconds=1),
            },
        )
    elif case == "late_member":
        payload["members"] = (
            {
                **bundle.members[0].model_dump(),
                "observed_at": FREEZE + timedelta(seconds=1),
            },
        )
    elif case == "late_order":
        payload["revision_orders"] = (
            _order(_digest(b"order"), observed_at=FREEZE + timedelta(seconds=1)),
        )
    else:
        payload["cutoff"] = FREEZE + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        DecisionInputBundle.model_validate(payload)
