"""Explicit owner-run, memory-only observation; no broker mutation routes."""

import argparse
import hashlib
import json
import multiprocessing
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import partial
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Annotated, Any

import httpx
from pydantic import Field, SecretStr

from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.domain.prices import SEOUL
from ats.domain.strategy import FrozenModel, Sha256Digest, StrategySpec
from ats.kis_readonly import (
    ReadOnlyAccessPermit,
    ReadOnlyCredentials,
    ReadOnlyError,
    ReadOnlyKisClient,
    WindowsCredentialProvider,
)
from ats.operator import OperatorAuth
from ats.readonly_owner import (
    OwnerAuthority,
    protected_prompt,
    provision_credentials,
    require_owner_storage,
)
from ats.readonly_runtime import ReadOnlyRuntime


class ReadOnceRequest(FrozenModel):
    permit: ReadOnlyAccessPermit
    source_policy: SourceAllowlist
    candidate_digest: Sha256Digest
    candidate_artifact_digest: Sha256Digest
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9]{6}$")]
    session: date

    def verify(self, candidate: bytes, at: datetime) -> None:
        if (
            at.tzinfo is None
            or self.session != at.astimezone(SEOUL).date()
            or self.symbol not in self.permit.symbols
            or evidence_digest(self.source_policy) != self.permit.source_policy_digest
        ):
            raise ReadOnlyError("read-once scope mismatch")
        if (
            "sha256:" + hashlib.sha256(candidate).hexdigest()
            != self.candidate_artifact_digest
            or StrategySpec.model_validate_json(candidate).content_digest()
            != self.candidate_digest
        ):
            raise ReadOnlyError("selected candidate artifact mismatch")
        if (
            self.permit.retain_observations
            or self.permit.max_token_requests != 1
            or not {"QUOTE", "BALANCE", "CAPACITY", "OPEN_ORDERS", "HISTORY"}.issubset(
                self.permit.operations
            )
        ):
            raise ReadOnlyError(
                "memory-only one-token complete observation scope required"
            )


def run_read_once(
    request: ReadOnceRequest,
    candidate: bytes,
    *,
    authorize: Callable[[], None],
    runtime: ReadOnlyRuntime,
    credentials: Callable[[str], ReadOnlyCredentials],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    transport: httpx.MockTransport | None = None,
) -> dict[str, Any]:
    request = ReadOnceRequest.model_validate(request.model_dump())
    request.verify(candidate, clock())
    authorize()
    with runtime.collector(request.permit, clock=clock) as owner:
        client = ReadOnlyKisClient(
            lambda: request.permit,
            lambda: request.source_policy,
            credentials,
            account_id=request.permit.account_id,
            transport=transport,
            allow_network=transport is None,
            clock=clock,
            guard=lambda: runtime.check_owner(request.permit, owner),
            before_request=lambda permit, token: runtime.before_request(
                permit, owner, token=token, at=clock()
            ),
        )
        try:
            observation = client.observation_bundle(request.symbol, request.session)
            return {
                "mode": observation.mode,
                "request_digest": evidence_digest(request),
                "candidate_digest": request.candidate_digest,
                "http_calls": len(client.audit),
                "transport_audit": client.audit,
                "uncertainties": observation.uncertainties,
                "reservation_crosscheck": observation.reservation_crosscheck,
                "payload_persisted": False,
                "execution_authorized": False,
                "broker_orders_sent": 0,
                "actual_paper_sessions": 0,
            }
        finally:
            client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Owner-only memory observation; default is public preflight"
    )
    parser.add_argument(
        "action", choices=("preflight", "provision", "read-once", "recover-collector")
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--auth", type=Path)
    parser.add_argument("--control", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        request = ReadOnceRequest.model_validate_json(args.request.read_bytes())
        candidate = args.candidate.read_bytes()
        request.verify(candidate, datetime.now(UTC))
        if args.action == "preflight":
            print(
                json.dumps(
                    {
                        "request_digest": evidence_digest(request),
                        "candidate_digest": request.candidate_digest,
                        "operations": request.permit.operations,
                        "max_calls": request.permit.max_calls,
                        "max_tokens": request.permit.max_token_requests,
                        "network_started": False,
                    }
                )
            )
            return
        if (
            not args.execute
            or args.auth is None
            or args.control is None
            or not sys.stdin.isatty()
        ):
            raise ReadOnlyError(
                "explicit execute, trusted auth, control store and owner terminal required"
            )
        auth = OperatorAuth.model_validate_json(args.auth.read_bytes())
        require_owner_storage(args.control)
        token = SecretStr(protected_prompt("Scoped owner approval token: "))
        authority = OwnerAuthority(args.control)
        if args.action == "provision":
            provision_credentials(
                request.permit,
                authorize=lambda: authority.consume(
                    auth,
                    token,
                    action="PROVISION_CREDENTIALS",
                    target_digest=evidence_digest(request.permit),
                ),
                prompt=protected_prompt,
            )
            print("Protected slot provisioned; no network request made.")
        elif args.action == "recover-collector":
            ReadOnlyRuntime(args.control).recover_owner(
                request.permit,
                authorize=lambda: authority.consume(
                    auth,
                    token,
                    action="RECOVER_COLLECTOR",
                    target_digest=evidence_digest(request),
                ),
            )
        else:
            authority.consume(
                auth, token, action="READ_ONCE", target_digest=evidence_digest(request)
            )
            result = supervise_owner_result(
                partial(owner_read_worker, request, candidate, args.control)
            )
            print(json.dumps(result))
    except Exception:
        print(
            "Owner operation rejected or failed; no secret details emitted.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def owner_read_worker(
    request: ReadOnceRequest, candidate: bytes, control: Path
) -> dict[str, Any]:
    import logging

    logging.disable(logging.CRITICAL)
    return run_read_once(
        request,
        candidate,
        authorize=lambda: None,
        runtime=ReadOnlyRuntime(control),
        credentials=WindowsCredentialProvider(),
    )


def _send_owner_result(
    worker: Callable[[], dict[str, Any]], output: Connection
) -> None:
    try:
        raw = json.dumps(worker()).encode()
        if len(raw) > 1048576:
            raise ValueError("owner result size")
        output.send_bytes(raw)
    except Exception:
        raise SystemExit(1) from None
    finally:
        output.close()


def supervise_owner_result(
    worker: Callable[[], dict[str, Any]], *, timeout_seconds: float = 120
) -> dict[str, Any]:
    if not 0 < timeout_seconds <= 120:
        raise ValueError("bounded owner deadline required")
    from pydantic import TypeAdapter

    context = multiprocessing.get_context("spawn")
    incoming, outgoing = context.Pipe(duplex=False)
    process = context.Process(target=_send_owner_result, args=(worker, outgoing))
    process.start()
    outgoing.close()
    try:
        if not incoming.poll(timeout_seconds):
            raise ValueError("owner supervisor deadline exceeded")
        result = TypeAdapter(dict[str, Any]).validate_json(incoming.recv_bytes(1048576))
        process.join(5)
        if process.exitcode != 0:
            raise ValueError("owner worker did not finish")
        return result
    except Exception:
        raise ValueError(
            "owner observation failed or timed out; explicit recovery required"
        ) from None
    finally:
        incoming.close()
        if process.is_alive():
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
        process.close()


if __name__ == "__main__":
    main()
