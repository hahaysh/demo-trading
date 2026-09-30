import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ats.data.artifacts import ArtifactResolutionError, LocalArtifactResolver
from ats.data.asof import AsOfSelectionError, RevisionOrder
from ats.domain.data import DataSnapshot, PointInTimeRecord, UniverseMembershipManifest
from ats.domain.strategy import ArtifactRef

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
