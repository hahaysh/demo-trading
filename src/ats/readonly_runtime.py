"""Persistent exclusive ownership and credential-free read request accounting."""

import ctypes
import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ats.data.storage import LocalPayloadStore
from ats.domain.governance import evidence_digest
from ats.kis_readonly import ReadOnlyAccessPermit, ReadOnlyError


class ReadOnlyRuntime(LocalPayloadStore):
    @contextmanager
    def collector(
        self, permit: ReadOnlyAccessPermit, *, clock: Callable[[], datetime]
    ) -> Generator[str, None, None]:
        owner = str(uuid4())
        at = clock()
        if at.tzinfo is None or not permit.starts_at <= at < permit.expires_at:
            raise ReadOnlyError("collector authority expired")
        with self._connection(create=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS readonly_owners (slot TEXT PRIMARY KEY,owner TEXT NOT NULL,permit TEXT NOT NULL,acquired TEXT NOT NULL,pid INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS readonly_tokens (slot TEXT PRIMARY KEY,not_before TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS readonly_calls (permit TEXT PRIMARY KEY,count INTEGER NOT NULL,tokens INTEGER NOT NULL)"
            )
            if connection.execute(
                "SELECT 1 FROM readonly_owners WHERE slot=?", (permit.credential_ref,)
            ).fetchone():
                raise ReadOnlyError(
                    "collector already owned; explicit recovery required after crash"
                )
            connection.execute(
                "INSERT INTO readonly_owners VALUES (?,?,?,?,?)",
                (
                    permit.credential_ref,
                    owner,
                    evidence_digest(permit),
                    at.astimezone(UTC).isoformat(),
                    os.getpid(),
                ),
            )
        try:
            yield owner
        finally:
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM readonly_owners WHERE slot=? AND owner=?",
                    (permit.credential_ref, owner),
                )

    def check_owner(self, permit: ReadOnlyAccessPermit, owner: str) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT owner,permit FROM readonly_owners WHERE slot=?",
                (permit.credential_ref,),
            ).fetchone()
            if (
                row is None
                or row["owner"] != owner
                or row["permit"] != evidence_digest(permit)
            ):
                raise ReadOnlyError("collector ownership lost")

    def before_request(
        self, permit: ReadOnlyAccessPermit, owner: str, *, token: bool, at: datetime
    ) -> None:
        if at.tzinfo is None or not permit.starts_at <= at < permit.expires_at:
            raise ReadOnlyError("collector authority expired")
        digest = evidence_digest(permit)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT owner,permit FROM readonly_owners WHERE slot=?",
                (permit.credential_ref,),
            ).fetchone()
            if row is None or row["owner"] != owner or row["permit"] != digest:
                raise ReadOnlyError("collector ownership lost")
            calls = connection.execute(
                "SELECT count,tokens FROM readonly_calls WHERE permit=?", (digest,)
            ).fetchone()
            if calls is not None and (
                calls["count"] >= permit.max_calls
                or token
                and calls["tokens"] >= permit.max_token_requests
            ):
                raise ReadOnlyError("persistent read-only call budget exhausted")
            if token:
                cooldown = connection.execute(
                    "SELECT not_before FROM readonly_tokens WHERE slot=?",
                    (permit.credential_ref,),
                ).fetchone()
                if cooldown is not None and at < datetime.fromisoformat(
                    cooldown["not_before"]
                ):
                    raise ReadOnlyError("persistent token issuance hold active")
                connection.execute(
                    "INSERT INTO readonly_tokens VALUES (?,?) ON CONFLICT(slot) DO UPDATE SET not_before=excluded.not_before",
                    (
                        permit.credential_ref,
                        (at + timedelta(days=1)).astimezone(UTC).isoformat(),
                    ),
                )
            connection.execute(
                "INSERT INTO readonly_calls VALUES (?,1,?) ON CONFLICT(permit) DO UPDATE SET count=count+1,tokens=tokens+excluded.tokens",
                (digest, int(token)),
            )

    def recover_owner(
        self, permit: ReadOnlyAccessPermit, *, authorize: Callable[[], None]
    ) -> None:
        authorize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT pid FROM readonly_owners WHERE slot=? AND permit=?",
                (permit.credential_ref, evidence_digest(permit)),
            ).fetchone()
            if row is not None and not process_stopped(row["pid"]):
                raise ReadOnlyError("previous collector termination not verified")
            connection.execute(
                "DELETE FROM readonly_owners WHERE slot=? AND permit=?",
                (permit.credential_ref, evidence_digest(permit)),
            )


def process_stopped(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False
    from ctypes import wintypes

    library = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    library.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    library.OpenProcess.restype = wintypes.HANDLE
    library.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    library.WaitForSingleObject.restype = wintypes.DWORD
    library.CloseHandle.argtypes = [wintypes.HANDLE]
    library.CloseHandle.restype = wintypes.BOOL
    handle = library.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87
    try:
        return library.WaitForSingleObject(handle, 0) == 0
    finally:
        library.CloseHandle(handle)
