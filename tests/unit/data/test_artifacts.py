import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ats.data.artifacts import ArtifactResolutionError, LocalArtifactResolver
from ats.data.asof import AsOfSelectionError, RevisionOrder
from ats.data.bundle import (
    DecisionInputBundle,
    SourceEligibilityError,
    UnscopedRecordPolicy,
)
from ats.data.bundle import (
    build_decision_inputs as build_inputs_with_policy,
)
from ats.data.replay import (
    InputReplayError,
    InputReplayRequest,
    InputReplayResult,
    replay_inputs,
)
from ats.data.requirements import (
    DataRequirementError,
    SourceDataRequirement,
    validate_data_requirements,
)
from ats.domain.data import DataSnapshot, PointInTimeRecord, UniverseMembershipManifest
from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist, SourceReviewState, load_policy
from ats.domain.strategy import ArtifactRef
from ats.domain.universe import UniverseMember, UniverseMembershipArtifact

FREEZE = datetime(2026, 9, 30, 7, tzinfo=UTC)


def _source_policy() -> SourceAllowlist:
    """Synthetic approval only; never activate a repository policy."""
    return SourceAllowlist.model_validate(
        {
            "metadata": {
                "policy_id": "test-source-policy",
                "version": "1.0.0",
                "status": "APPROVED",
                "approved_by": "test-reviewer",
                "approved_at": FREEZE - timedelta(days=10),
            },
            "sources": (
                {
                    "source_id": "test-source",
                    "category": "DISCLOSURE",
                    "enabled": True,
                    "legal_review": "APPROVED",
                    "rights": {"classification": "APPROVED_PUBLIC"},
                    "rate_limit_per_minute": 10,
                    "notes": "Synthetic local fixture source.",
                },
            ),
        }
    )


build_decision_inputs = partial(
    build_inputs_with_policy, source_policy=_source_policy()
)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _replay_request(root: Path) -> InputReplayRequest:
    early = FREEZE - timedelta(minutes=2)
    correction_at = FREEZE - timedelta(minutes=1)
    first = PointInTimeRecord.model_validate(
        {
            **_record(_store(root, b"original"), observed_at=early).model_dump(),
            "instrument_id": "krx-005930",
        }
    )
    corrected = PointInTimeRecord.model_validate(
        {
            **first.model_dump(),
            "revision": "rev-002",
            "observed_at": correction_at,
            "raw_payload_digest": _store(root, b"correction"),
        }
    )
    second = PointInTimeRecord.model_validate(
        {
            **first.model_dump(),
            "source_item_id": "second-item",
            "instrument_id": "krx-000660",
            "raw_payload_digest": _store(root, b"second instrument"),
        }
    )
    manifest = _universe(
        _member(), _member(instrument_id="krx-000660", effective_from=correction_at)
    )
    reference = _universe_reference(root, manifest)
    snapshot = DataSnapshot.model_validate(
        {
            **_snapshot(first, corrected, second).model_dump(),
            "universe_membership": reference,
        }
    )
    order = _order(
        _store(root, b"synthetic correction ordering"), observed_at=correction_at
    )
    return InputReplayRequest(
        snapshot=snapshot,
        source_policy=_source_policy(),
        cutoffs=(early, correction_at, FREEZE),
        revision_orders=(order,),
        data_requirements=(_requirement(max_age_seconds=120),),
    )


def test_replay_combines_late_revisions_membership_and_freshness(
    tmp_path: Path,
) -> None:
    request = _replay_request(tmp_path)
    before = request.model_dump_json()
    resolver = LocalArtifactResolver(tmp_path)
    result = replay_inputs(resolver, request)
    assert result.request_digest == evidence_digest(request)
    assert [bundle.cutoff for bundle in result.bundles] == list(request.cutoffs)
    assert [record.revision for record in result.bundles[0].records] == ["rev-001"]
    assert len(result.bundles[0].members) == 1
    assert result.bundles[0].revision_orders == ()
    assert len(result.bundles[1].members) == 2
    assert {record.revision for record in result.bundles[1].records} == {
        "rev-001",
        "rev-002",
    }
    assert result.bundles[1].revision_orders == request.revision_orders
    assert all(
        bundle.data_requirements == request.data_requirements
        for bundle in result.bundles
    )
    assert request.model_dump_json() == before
    assert replay_inputs(resolver, request).content_digest() == result.content_digest()
    assert InputReplayResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.__setattr__("bundles", ())


@pytest.mark.parametrize(
    "cutoffs",
    [
        (),
        (FREEZE, FREEZE),
        (FREEZE, FREEZE - timedelta(seconds=1)),
        (FREEZE + timedelta(seconds=1),),
        (datetime(2026, 9, 30),),
        (FREEZE, FREEZE.astimezone(timezone(timedelta(hours=9)))),
    ],
)
def test_replay_rejects_invalid_schedule_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cutoffs: tuple[datetime, ...]
) -> None:
    request = _replay_request(tmp_path)

    def no_read(resolver: LocalArtifactResolver, digest: str) -> bytes:
        pytest.fail("invalid replay schedule must not reach I/O")

    monkeypatch.setattr(LocalArtifactResolver, "read_digest", no_read)
    with pytest.raises(ValidationError):
        replay_inputs(
            LocalArtifactResolver(tmp_path),
            request.model_copy(update={"cutoffs": cutoffs}),
        )


def test_replay_normalizes_timezone_equivalent_schedules(tmp_path: Path) -> None:
    request = _replay_request(tmp_path)
    local = InputReplayRequest.model_validate(
        {
            **request.model_dump(),
            "cutoffs": tuple(
                cutoff.astimezone(timezone(timedelta(hours=9)))
                for cutoff in request.cutoffs
            ),
        }
    )
    assert local == request
    assert replay_inputs(LocalArtifactResolver(tmp_path), request) == replay_inputs(
        LocalArtifactResolver(tmp_path), local
    )


@pytest.mark.parametrize("failure", ["stale", "missing", "corrupt", "ambiguous"])
def test_replay_later_failure_has_cutoff_and_cause_without_partial_result(
    tmp_path: Path, failure: str
) -> None:
    request = _replay_request(tmp_path)
    payload = request.model_dump()
    expected_index = 1
    if failure == "stale":
        payload["data_requirements"] = (_requirement(max_age_seconds=90),)
        expected_index = 2
    elif failure == "ambiguous":
        payload["revision_orders"] = ()
    else:
        digest = request.snapshot.records[1].raw_payload_digest
        path = tmp_path / "sha256" / digest.removeprefix("sha256:")
        if failure == "missing":
            path.unlink()
        else:
            path.write_bytes(b"tampered correction")
    request = InputReplayRequest.model_validate(payload)
    resolver = LocalArtifactResolver(tmp_path)
    early_request = InputReplayRequest.model_validate(
        {**request.model_dump(), "cutoffs": request.cutoffs[:1]}
    )
    assert len(replay_inputs(resolver, early_request).bundles) == 1
    with pytest.raises(InputReplayError) as caught:
        replay_inputs(resolver, request)
    assert caught.value.index == expected_index
    assert caught.value.cutoff == request.cutoffs[expected_index]
    assert isinstance(caught.value.__cause__, ValueError)
    assert not hasattr(caught.value, "bundles")


def test_replay_missing_required_data_fails_at_first_cutoff(tmp_path: Path) -> None:
    request = _replay_request(tmp_path)
    request = InputReplayRequest.model_validate(
        {
            **request.model_dump(),
            "data_requirements": (
                _requirement(instrument_id="krx-000660", max_age_seconds=120),
            ),
        }
    )
    with pytest.raises(InputReplayError) as caught:
        replay_inputs(LocalArtifactResolver(tmp_path), request)
    assert caught.value.index == 0
    assert "missing required data" in str(caught.value.__cause__)


def test_replay_rejects_inactive_or_late_policy_and_duplicate_requirements(
    tmp_path: Path,
) -> None:
    request = _replay_request(tmp_path)
    for metadata in [
        {**request.source_policy.metadata.model_dump(), "approved_at": FREEZE},
        {
            **request.source_policy.metadata.model_dump(),
            "status": "DRAFT",
            "approved_at": None,
            "approved_by": None,
        },
    ]:
        policy = SourceAllowlist.model_validate(
            {**request.source_policy.model_dump(), "metadata": metadata}
        )
        with pytest.raises(ValidationError, match="first cutoff"):
            InputReplayRequest.model_validate(
                {**request.model_dump(), "source_policy": policy}
            )
    with pytest.raises(ValidationError, match="unique"):
        InputReplayRequest.model_validate(
            {
                **request.model_dump(),
                "data_requirements": (_requirement(), _requirement()),
            }
        )


def test_replay_result_rejects_reordered_or_mixed_input_bundles(tmp_path: Path) -> None:
    result = replay_inputs(LocalArtifactResolver(tmp_path), _replay_request(tmp_path))
    with pytest.raises(ValidationError, match="strictly increasing"):
        InputReplayResult(
            request_digest=result.request_digest,
            bundles=tuple(reversed(result.bundles)),
        )
    last = DecisionInputBundle.model_validate(
        {**result.bundles[-1].model_dump(), "data_requirements": ()}
    )
    with pytest.raises(ValidationError, match="share pinned"):
        InputReplayResult(
            request_digest=result.request_digest, bundles=(*result.bundles[:-1], last)
        )


def test_replay_receipt_requires_complete_schedule_and_exact_request(
    tmp_path: Path,
) -> None:
    request = _replay_request(tmp_path)
    result = replay_inputs(LocalArtifactResolver(tmp_path), request)
    result.validate_against_request(request)
    partial_result = InputReplayResult(
        request_digest=result.request_digest, bundles=result.bundles[:1]
    )
    with pytest.raises(ValueError, match="exact requested cutoffs"):
        partial_result.validate_against_request(request)
    changed_request = InputReplayRequest.model_validate(
        {**request.model_dump(), "data_requirements": ()}
    )
    with pytest.raises(ValueError, match="request digest"):
        result.validate_against_request(changed_request)
    changed_bundles = tuple(
        DecisionInputBundle.model_validate(
            {**bundle.model_dump(), "data_requirements": ()}
        )
        for bundle in result.bundles
    )
    changed_result = InputReplayResult(
        request_digest=result.request_digest, bundles=changed_bundles
    )
    with pytest.raises(ValueError, match="requested provenance"):
        changed_result.validate_against_request(request)


def test_replay_hash_changes_when_requirements_or_schedule_change(
    tmp_path: Path,
) -> None:
    request = _replay_request(tmp_path)
    resolver = LocalArtifactResolver(tmp_path)
    result = replay_inputs(resolver, request)
    relaxed = InputReplayRequest.model_validate(
        {
            **request.model_dump(),
            "data_requirements": (_requirement(max_age_seconds=121),),
        }
    )
    shorter = InputReplayRequest.model_validate(
        {**request.model_dump(), "cutoffs": request.cutoffs[:2]}
    )
    assert replay_inputs(resolver, relaxed).content_digest() != result.content_digest()
    assert replay_inputs(resolver, shorter).content_digest() != result.content_digest()


def _requirement(**changes: object) -> SourceDataRequirement:
    return SourceDataRequirement.model_validate(
        {
            "requirement_id": "test-freshness",
            "source_id": "test-source",
            "min_records": 1,
            "freshness_basis": "OBSERVED",
            "max_age_seconds": 60,
            **changes,
        }
    )


def test_requirement_boundary_and_provenance(tmp_path: Path) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    records = tuple(
        PointInTimeRecord.model_validate(
            {**item.model_dump(), "observed_at": FREEZE - timedelta(seconds=60)}
        )
        for item in snapshot.records
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": records}
    )
    resolver = LocalArtifactResolver(tmp_path)
    bundle = build_decision_inputs(
        resolver, snapshot, at=FREEZE, data_requirements=(_requirement(),)
    )
    assert bundle.data_requirements == (_requirement(),)
    assert DecisionInputBundle.model_validate_json(bundle.model_dump_json()) == bundle
    relaxed = build_decision_inputs(
        resolver,
        snapshot,
        at=FREEZE,
        data_requirements=(_requirement(max_age_seconds=61),),
    )
    assert bundle.content_digest() != relaxed.content_digest()
    with pytest.raises(ValidationError, match="stale required data"):
        build_decision_inputs(
            resolver,
            snapshot,
            at=FREEZE,
            data_requirements=(_requirement(max_age_seconds=59),),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"min_records": 2},
        {"source_item_id": "outside"},
        {"instrument_id": "krx-000660"},
        {"source_item_id": "absent"},
        {"source_item_id": "unscoped"},
    ],
)
def test_requirements_count_only_admitted_inputs(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="missing required data"):
        build_decision_inputs(
            LocalArtifactResolver(tmp_path),
            _bundle_snapshot(tmp_path),
            at=FREEZE,
            data_requirements=(_requirement(**changes),),
        )


@pytest.mark.parametrize("basis", ["PUBLISHED", "EFFECTIVE"])
@pytest.mark.parametrize(
    "timestamp", [None, FREEZE + timedelta(seconds=1), FREEZE - timedelta(days=10)]
)
def test_recent_observation_cannot_hide_missing_future_or_stale_data_time(
    tmp_path: Path,
    basis: str,
    timestamp: datetime | None,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    field = "published_at" if basis == "PUBLISHED" else "effective_at"
    if basis == "PUBLISHED" and timestamp is not None and timestamp > FREEZE:
        with pytest.raises(ValidationError):
            PointInTimeRecord.model_validate(
                {**snapshot.records[0].model_dump(), field: timestamp}
            )
        return
    records = tuple(
        PointInTimeRecord.model_validate({**record.model_dump(), field: timestamp})
        for record in snapshot.records
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": records}
    )
    with pytest.raises(
        ValidationError, match="freshness timestamp|stale required data"
    ):
        build_decision_inputs(
            LocalArtifactResolver(tmp_path),
            snapshot,
            at=FREEZE,
            data_requirements=(_requirement(freshness_basis=basis),),
        )


@pytest.mark.parametrize("basis", ["OBSERVED", "PUBLISHED", "EFFECTIVE"])
def test_explicit_timestamp_basis_accepts_fresh_data(
    tmp_path: Path, basis: str
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    records = tuple(
        PointInTimeRecord.model_validate(
            {**record.model_dump(), "published_at": FREEZE, "effective_at": FREEZE}
        )
        for record in snapshot.records
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": records}
    )
    result = build_decision_inputs(
        LocalArtifactResolver(tmp_path),
        snapshot,
        at=FREEZE,
        data_requirements=(_requirement(freshness_basis=basis, max_age_seconds=0),),
    )
    assert len(result.records) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"max_age_seconds": -1},
        {"max_age_seconds": True},
        {"max_age_seconds": 1.5},
        {"min_records": 0},
        {"min_records": True},
        {"freshness_basis": "AUTO"},
    ],
)
def test_invalid_data_requirements_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _requirement(**changes)


def test_requirement_sources_and_duplicates_are_checked_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _bundle_snapshot(tmp_path)

    def no_read(resolver: LocalArtifactResolver, digest: str) -> bytes:
        pytest.fail("invalid requirements must not reach I/O")

    monkeypatch.setattr(LocalArtifactResolver, "read_digest", no_read)
    with pytest.raises(SourceEligibilityError):
        build_decision_inputs(
            LocalArtifactResolver(tmp_path),
            snapshot,
            at=FREEZE,
            data_requirements=(_requirement(source_id="unlisted"),),
        )
    with pytest.raises(ValueError, match="unique"):
        build_decision_inputs(
            LocalArtifactResolver(tmp_path),
            snapshot,
            at=FREEZE,
            data_requirements=(_requirement(), _requirement()),
        )


def test_one_fresh_record_cannot_mask_another_stale_matching_record(
    tmp_path: Path,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    stale = PointInTimeRecord.model_validate(
        {
            **snapshot.records[0].model_dump(),
            "source_item_id": "stale-item",
            "observed_at": FREEZE - timedelta(hours=1),
        }
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": (*snapshot.records, stale)}
    )
    with pytest.raises(ValidationError, match="stale required data"):
        build_decision_inputs(
            LocalArtifactResolver(tmp_path),
            snapshot,
            at=FREEZE,
            data_requirements=(_requirement(),),
        )
    assert (
        len(
            build_decision_inputs(
                LocalArtifactResolver(tmp_path),
                snapshot,
                at=FREEZE,
                data_requirements=(_requirement(source_item_id="inside"),),
            ).records
        )
        == 2
    )


def test_bundle_receipt_rechecks_requirements_on_reconstruction(tmp_path: Path) -> None:
    bundle = build_decision_inputs(
        LocalArtifactResolver(tmp_path),
        _bundle_snapshot(tmp_path),
        at=FREEZE,
        data_requirements=(_requirement(),),
    )
    with pytest.raises(ValidationError, match="missing required data"):
        DecisionInputBundle.model_validate({**bundle.model_dump(), "records": ()})
    with pytest.raises(ValidationError):
        build_decision_inputs(
            LocalArtifactResolver(tmp_path),
            _bundle_snapshot(tmp_path),
            at=FREEZE,
            data_requirements=(
                _requirement().model_copy(update={"max_age_seconds": -1}),
            ),
        )


def test_age_boundary_is_exact_and_timezone_independent() -> None:
    record = _record(_digest(b"data"), observed_at=FREEZE - timedelta(seconds=60))
    local_cutoff = FREEZE.astimezone(timezone(timedelta(hours=9)))
    validate_data_requirements((_requirement(),), (record,), at=local_cutoff)
    with pytest.raises(DataRequirementError, match="stale"):
        validate_data_requirements(
            (_requirement(),), (record,), at=FREEZE + timedelta(microseconds=1)
        )
    with pytest.raises(DataRequirementError, match="timezone-aware"):
        validate_data_requirements(
            (_requirement(),), (record,), at=datetime(2026, 9, 30)
        )


def test_future_records_cannot_fulfil_required_availability(tmp_path: Path) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    with pytest.raises(ValidationError, match="missing required data"):
        build_decision_inputs(
            LocalArtifactResolver(tmp_path),
            snapshot,
            at=FREEZE - timedelta(seconds=1),
            data_requirements=(_requirement(),),
        )


def test_observation_and_effective_age_can_be_required_independently(
    tmp_path: Path,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    records = tuple(
        PointInTimeRecord.model_validate(
            {**record.model_dump(), "effective_at": FREEZE - timedelta(days=1)}
        )
        for record in snapshot.records
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": records}
    )
    requirements = (
        _requirement(),
        _requirement(requirement_id="economic-age", freshness_basis="EFFECTIVE"),
    )
    with pytest.raises(ValidationError, match="economic-age"):
        build_decision_inputs(
            LocalArtifactResolver(tmp_path),
            snapshot,
            at=FREEZE,
            data_requirements=requirements,
        )


def test_empty_and_unscoped_data_requirements_are_explicit(tmp_path: Path) -> None:
    snapshot = _bundle_snapshot(tmp_path, ())
    resolver = LocalArtifactResolver(tmp_path)
    with pytest.raises(ValidationError, match="missing required data"):
        build_decision_inputs(
            resolver, snapshot, at=FREEZE, data_requirements=(_requirement(),)
        )
    result = build_decision_inputs(
        resolver,
        snapshot,
        at=FREEZE,
        unscoped_policy=UnscopedRecordPolicy.INCLUDE,
        data_requirements=(_requirement(source_item_id="unscoped"),),
    )
    assert result.records[0].source_item_id == "unscoped"


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


@pytest.mark.parametrize("status", ["DRAFT", "RETIRED"])
def test_source_gate_blocks_inactive_policy_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    base = _source_policy()
    policy = SourceAllowlist.model_validate(
        {
            **base.model_dump(),
            "metadata": {
                **base.metadata.model_dump(),
                "status": status,
                "approved_at": None,
                "approved_by": None,
            },
        }
    )

    def no_read(resolver: LocalArtifactResolver, digest: str) -> bytes:
        pytest.fail("source rejection must precede all artifact reads")

    monkeypatch.setattr(LocalArtifactResolver, "read_digest", no_read)
    with pytest.raises(SourceEligibilityError, match="approved by the cutoff"):
        build_inputs_with_policy(
            LocalArtifactResolver(tmp_path), snapshot, at=FREEZE, source_policy=policy
        )


@pytest.mark.parametrize(
    "case", ["unlisted", "disabled", "pending", "rejected", "late_approval"]
)
def test_source_gate_blocks_ineligible_sources_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    base = _source_policy()
    payload = base.model_dump()
    if case == "late_approval":
        payload["metadata"] = {
            **base.metadata.model_dump(),
            "approved_at": FREEZE + timedelta(seconds=1),
        }
    elif case == "unlisted":
        payload["sources"] = ()
    else:
        source = {**base.sources[0].model_dump(), "enabled": False}
        if case in ("pending", "rejected"):
            source["legal_review"] = case.upper()
        payload["sources"] = (source,)
    policy = SourceAllowlist.model_validate(payload)

    def no_read(resolver: LocalArtifactResolver, digest: str) -> bytes:
        pytest.fail("source rejection must precede all artifact reads")

    monkeypatch.setattr(LocalArtifactResolver, "read_digest", no_read)
    with pytest.raises(SourceEligibilityError):
        build_inputs_with_policy(
            LocalArtifactResolver(tmp_path), snapshot, at=FREEZE, source_policy=policy
        )


@pytest.mark.parametrize("item", ["inside", "outside", "unscoped"])
@pytest.mark.parametrize("rights", ["PENDING_REVIEW", "LICENSED"])
def test_source_gate_checks_rights_even_for_excluded_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    item: str,
    rights: str,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    records = tuple(
        PointInTimeRecord.model_validate(
            {**record.model_dump(), "rights_class": rights}
        )
        if record.source_item_id == item
        else record
        for record in snapshot.records
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": records}
    )

    def no_read(resolver: LocalArtifactResolver, digest: str) -> bytes:
        pytest.fail("rights rejection must precede artifact reads")

    monkeypatch.setattr(LocalArtifactResolver, "read_digest", no_read)
    with pytest.raises(SourceEligibilityError, match="rights"):
        build_inputs_with_policy(
            LocalArtifactResolver(tmp_path),
            snapshot,
            at=FREEZE,
            source_policy=_source_policy(),
        )


def test_source_policy_is_bound_and_resolved_by_exact_digest(tmp_path: Path) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    policy = _source_policy()
    resolver = LocalArtifactResolver(tmp_path)
    bundle = build_inputs_with_policy(
        resolver, snapshot, at=FREEZE, source_policy=policy
    )
    assert bundle.source_policy.digest == evidence_digest(policy)
    assert bundle.source_policy.policy_id == policy.metadata.policy_id
    assert bundle.source_policy.version == policy.metadata.version
    bundle.validate_source_policy(policy)
    changed = SourceAllowlist.model_validate(
        {
            **policy.model_dump(),
            "sources": (
                {**policy.sources[0].model_dump(), "rate_limit_per_minute": 11},
            ),
        }
    )
    with pytest.raises(SourceEligibilityError, match="reference mismatch"):
        bundle.validate_source_policy(changed)
    rebuilt = build_inputs_with_policy(
        resolver, snapshot, at=FREEZE, source_policy=changed
    )
    assert rebuilt.content_digest() != bundle.content_digest()


def test_future_unlisted_source_does_not_block_past_bundle(tmp_path: Path) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    cutoff = FREEZE - timedelta(minutes=1)
    early = tuple(
        PointInTimeRecord.model_validate({**record.model_dump(), "observed_at": cutoff})
        for record in snapshot.records
    )
    later = PointInTimeRecord.model_validate(
        {
            **snapshot.records[0].model_dump(),
            "source_id": "unlisted-future",
            "raw_payload_digest": _digest(b"missing future data"),
        }
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": (*early, later)}
    )
    resolver = LocalArtifactResolver(tmp_path)
    assert build_inputs_with_policy(
        resolver, snapshot, at=cutoff, source_policy=_source_policy()
    ).records == (early[0],)
    with pytest.raises(SourceEligibilityError, match="not eligible"):
        build_inputs_with_policy(
            resolver, snapshot, at=FREEZE, source_policy=_source_policy()
        )


def test_source_gate_revalidates_unchecked_policy_copies(tmp_path: Path) -> None:
    policy = _source_policy()
    invalid = policy.model_copy(
        update={
            "sources": (
                policy.sources[0].model_copy(
                    update={"legal_review": SourceReviewState.PENDING}
                ),
            )
        }
    )
    with pytest.raises(ValidationError, match="approved legal review"):
        build_inputs_with_policy(
            LocalArtifactResolver(tmp_path),
            _bundle_snapshot(tmp_path),
            at=FREEZE,
            source_policy=invalid,
        )


def test_repository_source_policy_stays_draft_and_cannot_build_bundle(
    tmp_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[3] / "config" / "source-allowlist.yaml"
    original = path.read_bytes()
    policy = load_policy(path, SourceAllowlist)
    with pytest.raises(SourceEligibilityError, match="approved by the cutoff"):
        build_inputs_with_policy(
            LocalArtifactResolver(tmp_path),
            _bundle_snapshot(tmp_path),
            at=FREEZE,
            source_policy=policy,
        )
    assert path.read_bytes() == original


def test_source_policy_approval_boundary_is_inclusive(tmp_path: Path) -> None:
    base = _source_policy()
    policy = SourceAllowlist.model_validate(
        {
            **base.model_dump(),
            "metadata": {**base.metadata.model_dump(), "approved_at": FREEZE},
        }
    )
    bundle = build_inputs_with_policy(
        LocalArtifactResolver(tmp_path),
        _bundle_snapshot(tmp_path),
        at=FREEZE,
        source_policy=policy,
    )
    bundle.validate_source_policy(policy)


def test_superseded_revision_rights_cannot_be_hidden_by_latest_revision(
    tmp_path: Path,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    original = snapshot.records[0]
    older = PointInTimeRecord.model_validate(
        {**original.model_dump(), "rights_class": "PENDING_REVIEW"}
    )
    newer = PointInTimeRecord.model_validate(
        {**original.model_dump(), "revision": "rev-002"}
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": (older, newer)}
    )
    with pytest.raises(SourceEligibilityError, match="rights"):
        build_inputs_with_policy(
            LocalArtifactResolver(tmp_path),
            snapshot,
            at=FREEZE,
            source_policy=_source_policy(),
        )


def test_unlisted_revision_order_is_rejected_before_evidence_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _bundle_snapshot(tmp_path)
    order = RevisionOrder.model_validate(
        {**_order(_digest(b"absent")).model_dump(), "source_id": "unlisted-source"}
    )

    def no_read(resolver: LocalArtifactResolver, digest: str) -> bytes:
        pytest.fail("unlisted order must fail before evidence I/O")

    monkeypatch.setattr(LocalArtifactResolver, "read_digest", no_read)
    with pytest.raises(SourceEligibilityError, match="not eligible"):
        build_inputs_with_policy(
            LocalArtifactResolver(tmp_path),
            snapshot,
            at=FREEZE,
            source_policy=_source_policy(),
            revision_orders=(order,),
        )


def test_empty_input_still_requires_approved_policy(tmp_path: Path) -> None:
    draft = SourceAllowlist.model_validate(
        {"metadata": {"policy_id": "test-draft", "version": "1"}, "sources": ()}
    )
    with pytest.raises(SourceEligibilityError):
        build_inputs_with_policy(
            LocalArtifactResolver(tmp_path), _snapshot(), at=FREEZE, source_policy=draft
        )


@pytest.mark.parametrize("field", ["policy_id", "version", "digest"])
def test_bundle_rejects_changed_source_policy_reference(
    tmp_path: Path, field: str
) -> None:
    policy = _source_policy()
    bundle = build_inputs_with_policy(
        LocalArtifactResolver(tmp_path),
        _bundle_snapshot(tmp_path),
        at=FREEZE,
        source_policy=policy,
    )
    value = _digest(b"tampered policy reference") if field == "digest" else "changed"
    altered = DecisionInputBundle.model_validate(
        {
            **bundle.model_dump(),
            "source_policy": {**bundle.source_policy.model_dump(), field: value},
        }
    )
    with pytest.raises(SourceEligibilityError, match="reference mismatch"):
        altered.validate_source_policy(policy)


def test_bundle_policy_resolution_rechecks_record_eligibility(tmp_path: Path) -> None:
    policy = _source_policy()
    bundle = build_inputs_with_policy(
        LocalArtifactResolver(tmp_path),
        _bundle_snapshot(tmp_path),
        at=FREEZE,
        source_policy=policy,
    )
    altered = DecisionInputBundle.model_validate(
        {
            **bundle.model_dump(),
            "records": (
                {**bundle.records[0].model_dump(), "rights_class": "PENDING_REVIEW"},
            ),
        }
    )
    with pytest.raises(SourceEligibilityError, match="rights"):
        altered.validate_source_policy(policy)
    with pytest.raises(ValidationError, match="source_policy"):
        DecisionInputBundle.model_validate(bundle.model_dump(exclude={"source_policy"}))


def test_matching_licensed_source_and_records_are_allowed(tmp_path: Path) -> None:
    base = _source_policy()
    policy = SourceAllowlist.model_validate(
        {
            **base.model_dump(),
            "sources": (
                {
                    **base.sources[0].model_dump(),
                    "rights": {"classification": "LICENSED"},
                },
            ),
        }
    )
    snapshot = _bundle_snapshot(tmp_path)
    records = tuple(
        PointInTimeRecord.model_validate(
            {**record.model_dump(), "rights_class": "LICENSED"}
        )
        for record in snapshot.records
    )
    snapshot = DataSnapshot.model_validate(
        {**snapshot.model_dump(), "records": records}
    )
    bundle = build_inputs_with_policy(
        LocalArtifactResolver(tmp_path), snapshot, at=FREEZE, source_policy=policy
    )
    assert bundle.records == (records[0],)
    bundle.validate_source_policy(policy)
