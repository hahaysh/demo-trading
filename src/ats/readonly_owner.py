"""Owner-invoked authorization and protected provisioning; no implicit discovery."""

import ctypes
import getpass
import json
import os
import re
import sys
import warnings
from collections.abc import Callable
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import SecretStr

from ats.data.storage import LocalPayloadStore
from ats.domain.governance import evidence_digest
from ats.kis_readonly import ReadOnlyAccessPermit, ReadOnlyCredentials, ReadOnlyError
from ats.operator import OperatorAuth

OwnerAction = Literal[
    "PROVISION_CREDENTIALS",
    "READ_ONCE",
    "OBSERVE_FIVE_DAYS",
    "WITHDRAW_OBSERVATION",
    "RECOVER_COLLECTOR",
    "HALT_OBSERVATION",
    "RESUME_OBSERVATION",
]


def protected_prompt(label: str) -> str:
    if not sys.stdin.isatty():
        raise ReadOnlyError("private interactive terminal required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(label)


class OwnerAuthority(LocalPayloadStore):
    def require_approval(self, *, action: OwnerAction, target_digest: str) -> None:
        with self._connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM readonly_approvals WHERE action=? AND target=?",
                (action, target_digest),
            ).fetchone():
                raise ReadOnlyError("registered owner approval missing")

    def consume(
        self,
        auth: OperatorAuth,
        token: SecretStr,
        *,
        action: OwnerAction,
        target_digest: str,
        synthetic: bool = False,
    ) -> None:
        if not synthetic and (auth.synthetic_only or auth.algorithm != "RS256"):
            raise ReadOnlyError("real operations require non-fixture RS256 authority")
        identity, claims = auth.verify("Bearer " + token.get_secret_value())
        if (
            identity.role != "OPERATOR"
            or claims.get("action") != action
            or claims.get("target_digest") != target_digest
        ):
            raise ReadOnlyError("owner authorization scope mismatch")
        with self._connection(create=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS readonly_approvals (issuer TEXT NOT NULL,jti TEXT NOT NULL,subject TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,used_at TEXT NOT NULL,PRIMARY KEY(issuer,jti))"
            )
            if connection.execute(
                "SELECT 1 FROM readonly_approvals WHERE issuer=? AND jti=?",
                (auth.issuer, claims["jti"]),
            ).fetchone():
                raise ReadOnlyError("owner approval already consumed")
            connection.execute(
                "INSERT INTO readonly_approvals VALUES (?,?,?,?,?,?)",
                (
                    auth.issuer,
                    claims["jti"],
                    identity.subject,
                    action,
                    target_digest,
                    datetime.now(UTC).isoformat(),
                ),
            )


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def write_windows_credential(target: str, payload: bytes) -> None:
    if (
        sys.platform != "win32"
        or not re.fullmatch(r"ATS/KIS/[a-zA-Z0-9_.-]{3,64}", target)
        or not 0 < len(payload) <= 2560
    ):
        raise ReadOnlyError("invalid Windows protected credential target")
    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    library.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    library.CredReadW.restype = wintypes.BOOL
    library.CredFree.argtypes = [ctypes.c_void_p]
    library.CredFree.restype = None
    existing = ctypes.POINTER(_Credential)()
    if library.CredReadW(target, 1, 0, ctypes.byref(existing)):
        library.CredFree(existing)
        raise ReadOnlyError("protected slot exists; rotation is not permitted")
    if ctypes.get_last_error() != 1168:
        raise ReadOnlyError("protected slot availability could not be verified")
    blob = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    credential = _Credential()
    credential.Type = 1
    credential.TargetName = target
    credential.CredentialBlobSize = len(payload)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2
    library.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    library.CredWriteW.restype = wintypes.BOOL
    try:
        if not library.CredWriteW(ctypes.byref(credential), 0):
            raise ReadOnlyError("protected credential write failed")
    finally:
        ctypes.memset(ctypes.addressof(blob), 0, len(payload))


def provision_credentials(
    permit: ReadOnlyAccessPermit,
    *,
    authorize: Callable[[], None],
    prompt: Callable[[str], str],
    write: Callable[[str, bytes], None] = write_windows_credential,
) -> None:
    authorize()
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,64}", permit.credential_ref):
        raise ReadOnlyError("invalid protected reference")
    try:
        values = {
            "account_id": permit.account_id,
            "credential_ref": permit.credential_ref,
            "binding_id": str(permit.binding_id),
            "app_key": prompt("App Key: "),
            "app_secret": prompt("App Secret: "),
            "account_number": prompt("Account (8 digits): "),
            "product_code": prompt("Product code (2 digits): "),
        }
        record = ReadOnlyCredentials.model_validate(values)
        if not re.fullmatch(
            r"[0-9]{8}", record.account_number.get_secret_value()
        ) or any(
            not secret.get_secret_value()
            or not all(
                33 <= ord(character) <= 126 for character in secret.get_secret_value()
            )
            for secret in (record.app_key, record.app_secret)
        ):
            raise ValueError("invalid credential format")
        payload = json.dumps(values).encode()
        if len(payload) > 2560:
            raise ValueError("protected credential record too large")
        write("ATS/KIS/" + permit.credential_ref, payload)
    except Exception:
        raise ReadOnlyError(
            "protected provisioning failed; no credential details logged"
        ) from None


def permit_target(permit: ReadOnlyAccessPermit) -> str:
    return evidence_digest(permit)


def require_owner_storage(
    control: Path, ledger: Path | None = None, *, session_id: str | None = None
) -> None:
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        raise ReadOnlyError("Windows owner profile required")
    root = Path(os.environ["LOCALAPPDATA"]) / "GovernedATS"
    if control.resolve() != (root / "readonly-control.sqlite3").resolve():
        raise ReadOnlyError("one fixed owner control store required")
    if ledger is not None and (
        session_id is None
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", session_id)
        or ledger.resolve()
        != (root / "observations" / (session_id + ".sqlite3")).resolve()
    ):
        raise ReadOnlyError("approved owner-local observation store required")
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise ReadOnlyError("protected storage links are forbidden")
