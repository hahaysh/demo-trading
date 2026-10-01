import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ats.data.storage import LocalPayloadStore, StorageError, StoragePermit
from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist

NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def policy() -> SourceAllowlist:
    return SourceAllowlist.model_validate(
        {
            "metadata": {
                "policy_id": "synthetic-source-policy",
                "version": "1.0.0",
                "status": "APPROVED",
                "approved_by": "fixture-only",
                "approved_at": NOW - timedelta(days=2),
            },
            "sources": [
                {
                    "source_id": "test-source",
                    "category": "MARKET",
                    "enabled": True,
                    "legal_review": "APPROVED",
                    "rights": {
                        "classification": "LICENSED",
                        "retention_days": 2,
                    },
                    "rate_limit_per_minute": 1,
                    "notes": "Fabricated fixture; no production authorization.",
                }
            ],
        }
    )


@pytest.fixture
def permit(policy: SourceAllowlist) -> StoragePermit:
    return StoragePermit.model_validate(
        {
            "metadata": {
                "policy_id": "synthetic-storage-permit",
                "version": "1.0.0",
                "status": "APPROVED",
                "approved_by": "fixture-only",
                "approved_at": NOW - timedelta(days=1),
            },
            "source_id": "test-source",
            "source_policy_digest": evidence_digest(policy),
            "allow_persistence": True,
            "retention_days": 3,
            "expires_at": NOW + timedelta(days=10),
        }
    )


def test_round_trip_survives_restart_and_duplicate_does_not_extend_retention(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit
) -> None:
    database = tmp_path / "payloads.sqlite3"
    store = LocalPayloadStore(database)
    receipt = store.put(
        b"synthetic", observed_at=NOW, now=NOW, source_policy=policy, permit=permit
    )
    assert receipt.expires_at == NOW + timedelta(days=2)
    restarted = LocalPayloadStore(database)
    assert restarted.read(receipt, now=NOW, source_policy=policy, permit=permit) == (
        b"synthetic"
    )
    assert (
        restarted.put(
            b"synthetic",
            observed_at=NOW + timedelta(days=1),
            now=NOW + timedelta(days=1),
            source_policy=policy,
            permit=permit,
        )
        == receipt
    )


def test_separate_persistence_approval_is_required_before_io(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit
) -> None:
    database = tmp_path / "not-created.sqlite3"
    denied = permit.model_copy(update={"allow_persistence": False})
    with pytest.raises(StorageError):
        LocalPayloadStore(database).put(
            b"synthetic", observed_at=NOW, now=NOW, source_policy=policy, permit=denied
        )
    assert not database.exists()


def test_expiry_blocks_read_and_purge_removes_bytes(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit
) -> None:
    store = LocalPayloadStore(tmp_path / "payloads.sqlite3")
    receipt = store.put(
        b"synthetic", observed_at=NOW, now=NOW, source_policy=policy, permit=permit
    )
    with pytest.raises(StorageError):
        store.read(receipt, now=receipt.expires_at, source_policy=policy, permit=permit)
    assert store.purge_expired(now=receipt.expires_at) == 1
    assert store.purge_expired(now=receipt.expires_at) == 0
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payloads").fetchone()[0] == 0


def test_authorization_deadline_is_stricter_than_retention(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit
) -> None:
    permit = permit.model_copy(update={"expires_at": NOW + timedelta(hours=1)})
    receipt = LocalPayloadStore(tmp_path / "payloads.sqlite3").put(
        b"synthetic", observed_at=NOW, now=NOW, source_policy=policy, permit=permit
    )
    assert receipt.expires_at == permit.expires_at


def test_tampered_blob_and_receipt_are_rejected(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit
) -> None:
    store = LocalPayloadStore(tmp_path / "payloads.sqlite3")
    receipt = store.put(
        b"synthetic", observed_at=NOW, now=NOW, source_policy=policy, permit=permit
    )
    with pytest.raises(StorageError):
        store.read(
            receipt.model_copy(update={"expires_at": NOW + timedelta(days=9)}),
            now=NOW,
            source_policy=policy,
            permit=permit,
        )
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE payloads SET payload=?", (b"tampered",))
    with pytest.raises(StorageError):
        store.read(receipt, now=NOW, source_policy=policy, permit=permit)


def test_large_tampered_blob_cannot_be_returned(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit
) -> None:
    store = LocalPayloadStore(tmp_path / "payloads.sqlite3", max_bytes=10)
    receipt = store.put(
        b"synthetic", observed_at=NOW, now=NOW, source_policy=policy, permit=permit
    )
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE payloads SET payload=zeroblob(100000)")
    with pytest.raises(StorageError):
        store.read(receipt, now=NOW, source_policy=policy, permit=permit)


def test_naive_times_and_expired_duplicate_fail_before_data_changes(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit
) -> None:
    store = LocalPayloadStore(tmp_path / "payloads.sqlite3")
    with pytest.raises(StorageError):
        store.put(
            b"synthetic",
            observed_at=NOW.replace(tzinfo=None),
            now=NOW,
            source_policy=policy,
            permit=permit,
        )
    receipt = store.put(
        b"synthetic", observed_at=NOW, now=NOW, source_policy=policy, permit=permit
    )
    with pytest.raises(StorageError):
        store.put(
            b"synthetic",
            observed_at=receipt.expires_at,
            now=receipt.expires_at,
            source_policy=policy,
            permit=permit,
        )


@pytest.mark.parametrize("offset", [-1, 1])
def test_unapproved_or_future_observation_is_rejected(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit, offset: int
) -> None:
    observed = NOW + timedelta(days=offset * 10)
    with pytest.raises(StorageError):
        LocalPayloadStore(tmp_path / "payloads.sqlite3").put(
            b"synthetic",
            observed_at=observed,
            now=NOW,
            source_policy=policy,
            permit=permit,
        )


def test_policy_mismatch_and_revocation_block_access(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit
) -> None:
    store = LocalPayloadStore(tmp_path / "payloads.sqlite3")
    receipt = store.put(
        b"synthetic", observed_at=NOW, now=NOW, source_policy=policy, permit=permit
    )
    revoked = SourceAllowlist.model_validate(
        {
            **policy.model_dump(),
            "metadata": {"policy_id": "synthetic-source-policy", "version": "2.0.0"},
        }
    )
    with pytest.raises(StorageError):
        store.read(receipt, now=NOW, source_policy=revoked, permit=permit)


@pytest.mark.parametrize("payload", [b"", b"too large"])
def test_payload_size_is_bounded(
    tmp_path: Path, policy: SourceAllowlist, permit: StoragePermit, payload: bytes
) -> None:
    with pytest.raises(StorageError):
        LocalPayloadStore(tmp_path / "payloads.sqlite3", max_bytes=3).put(
            payload, observed_at=NOW, now=NOW, source_policy=policy, permit=permit
        )
