"""Local synthetic read-only/shadow demo; never loads real credentials."""

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import SecretStr

from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.kis_readonly import (
    ProductionAccountObservation,
    ReadOnlyAccessPermit,
    ReadOnlyCredentials,
    ReadOnlyKisClient,
)
from ats.operator import (
    OperatorAuth,
    OperatorCommand,
    OperatorIdentity,
    OperatorStore,
    create_operator_app,
)
from ats.shadow import (
    ShadowContext,
    ShadowProposal,
    ShadowSessionSpec,
    ShadowStore,
    run_shadow_round,
    supervise_shadow,
)

KEY = "public-synthetic-shadow-fixture-not-a-real-credential-0000"


def create_shadow_app(
    operator: OperatorStore,
    store: ShadowStore,
    spec: ShadowSessionSpec,
    auth: OperatorAuth,
) -> FastAPI:
    if spec.mode == "PRODUCTION_READ_ONLY" and (
        auth.synthetic_only or auth.algorithm != "RS256"
    ):
        raise ValueError(
            "production observations require non-fixture operator identity"
        )

    def status() -> dict[str, Any]:
        rows = store.reports(at=datetime.now(UTC), spec=spec)
        selected = [row for row in rows if row["session"] == spec.session_id]
        return {
            "jobs": [],
            "trials": [],
            "validations": [],
            "health": [],
            "shadow": selected,
            "shadow_mode": spec.mode,
        }

    app = create_operator_app(operator, auth, status_provider=status)

    @app.post("/api/shadow/recover")
    def recover(
        request: OperatorCommand, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, object]:
        identity, claims = auth.verify(authorization)
        if (
            request.action != "RESUME_RESEARCH"
            or request.target != spec.session_id
            or request.target_digest != evidence_digest(spec)
        ):
            raise HTTPException(403, "Shadow 복구에 바인딩된 재개 승인이 필요합니다.")

        def approve() -> None:
            operator.command(request, identity, claims)

        try:
            store.recover(
                spec,
                at=datetime.now(UTC),
                guard=operator.require_research_enabled,
                authorize=approve,
            )
        except ValueError:
            raise HTTPException(
                409, "활성 작업 또는 세션 조건으로 복구가 차단됐습니다."
            ) from None
        return {
            "session": spec.session_id,
            "recovered": True,
            "execution_authorized": False,
        }

    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    return app


def _fixture_configuration(
    root: Path,
) -> tuple[ShadowSessionSpec, ReadOnlyAccessPermit, SourceAllowlist]:
    spec = ShadowSessionSpec.model_validate_json((root / "session.json").read_bytes())
    permit = ReadOnlyAccessPermit.model_validate_json(
        (root / "read-permit.json").read_bytes()
    )
    policy = SourceAllowlist.model_validate_json(
        (root / "source-policy.json").read_bytes()
    )
    if (
        spec.mode != "SYNTHETIC"
        or not permit.synthetic_only
        or evidence_digest(permit) != spec.read_permit_digest
    ):
        raise ValueError("fixture runner only accepts its pinned synthetic permit")
    return spec, permit, policy


def initialize_fixture(root: Path) -> ShadowSessionSpec:
    root.mkdir(parents=True, exist_ok=True)
    if (root / "session.json").exists():
        return _fixture_configuration(root)[0]
    now = datetime.now(UTC)
    metadata = {
        "policy_id": "synthetic-shadow-authority",
        "version": "1",
        "status": "APPROVED",
        "approved_by": "fixture-only",
        "approved_at": now - timedelta(seconds=1),
    }
    policy = SourceAllowlist.model_validate(
        {
            "metadata": metadata,
            "sources": [
                {
                    "source_id": "kis-market",
                    "category": "MARKET",
                    "enabled": True,
                    "legal_review": "APPROVED",
                    "rights": {"classification": "LICENSED", "retention_days": 1},
                    "rate_limit_per_minute": 60,
                    "notes": "Fabricated responses only; no real source permission",
                }
            ],
        }
    )
    permit = ReadOnlyAccessPermit.model_validate(
        {
            "metadata": metadata,
            "account_id": "fixture-account",
            "credential_ref": "fixture-slot",
            "binding_id": UUID(int=1),
            "source_policy_digest": evidence_digest(policy),
            "starts_at": now,
            "expires_at": now + timedelta(hours=1),
            "enabled": True,
            "operations": ["BALANCE", "CAPACITY"],
            "symbols": ["005930"],
            "synthetic_only": True,
            "rate_per_minute": 60,
        }
    )
    spec = ShadowSessionSpec(
        session_id="synthetic-shadow",
        account_id=permit.account_id,
        candidate_digest="sha256:" + "a" * 64,
        read_permit_digest=evidence_digest(permit),
        mode="SYNTHETIC",
        starts_at=now,
        expires_at=permit.expires_at,
        retention=permit.metadata,
        retention_until=now + timedelta(days=1),
        allow_persistence=True,
    )
    for name, model in (
        ("source-policy.json", policy),
        ("read-permit.json", permit),
        ("session.json", spec),
    ):
        with (root / name).open("x", encoding="utf-8") as stream:
            stream.write(model.model_dump_json(indent=2))
    operator = OperatorStore(root / "operator.sqlite3")
    operator.register_review(spec.session_id, evidence_digest(spec))
    actor = OperatorIdentity(subject="fixture-human", role="OPERATOR", human=True)
    for revision, action in enumerate(("HALT_RESEARCH", "RESUME_RESEARCH")):
        command = OperatorCommand.model_validate(
            {
                "request_id": uuid4(),
                "action": action,
                "target": spec.session_id,
                "target_digest": evidence_digest(spec),
                "expected_revision": revision,
                "reason": "Synthetic shadow setup; not operational permission",
            }
        )
        operator.command(
            command,
            actor,
            {
                "jti": str(uuid4()),
                "action": action,
                "target_digest": command.target_digest,
                "command_digest": evidence_digest(command),
            },
        )
    return spec


def run_fixture_round(root: Path) -> None:
    spec, permit, policy = _fixture_configuration(root)
    operator = OperatorStore(root / "operator.sqlite3")

    def credentials(reference: str) -> ReadOnlyCredentials:
        return ReadOnlyCredentials(
            account_id=spec.account_id,
            credential_ref=reference,
            binding_id=permit.binding_id,
            app_key=SecretStr("synthetic-app"),
            app_secret=SecretStr("synthetic-secret"),
            account_number=SecretStr("12345678"),
            product_code="01",
        )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-token",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                    "access_token_token_expired": (
                        datetime.now(UTC) + timedelta(hours=9, days=1)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
        if request.headers["tr_id"] == "TTTC8908R":
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "ord_psbl_cash": "9000",
                        "nrcvb_buy_amt": "8000",
                        "nrcvb_buy_qty": "80",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [
                    {
                        "pdno": "005930",
                        "hldg_qty": "2",
                        "ord_psbl_qty": "2",
                        "evlu_amt": "200",
                    }
                ],
                "output2": [
                    {
                        "dnca_tot_amt": "10000",
                        "tot_evlu_amt": "10250",
                        "scts_evlu_amt": "200",
                        "fncg_amt": "0",
                    }
                ],
            },
        )

    client = ReadOnlyKisClient(
        lambda: permit,
        lambda: policy,
        credentials,
        account_id=spec.account_id,
        transport=httpx.MockTransport(respond),
        guard=lambda: operator.require_research_enabled(spec.session_id),
    )

    def collect() -> tuple[ProductionAccountObservation, list[dict[str, str | int]]]:
        return client.account_observation(
            symbol="005930", price=Decimal(100)
        ), client.audit

    def propose(
        observation: ProductionAccountObservation,
    ) -> tuple[tuple[ShadowProposal, ...], tuple[ShadowContext, ...]]:
        now = datetime.now(UTC)
        proposal = ShadowProposal(
            proposal_id=uuid4(),
            account_id=spec.account_id,
            candidate_digest=spec.candidate_digest,
            observation_digest=evidence_digest(observation),
            created_at=now,
            symbol="005930",
            side="BUY",
            quantity=5,
            limit_price=Decimal(100),
            reason="Synthetic overlay; unknown operational evidence remains",
        )
        context = ShadowContext(
            candidate_digest=spec.candidate_digest,
            account_id=spec.account_id,
            symbol=proposal.symbol,
            observation_digest=proposal.observation_digest,
            observed_at=now,
            valid_until=now + timedelta(seconds=60),
            evidence_digest="sha256:" + "b" * 64,
        )
        return (proposal,), (context,)

    try:
        run_shadow_round(
            ShadowStore(root / "shadow.sqlite3"),
            spec,
            "fixture-round-001",
            collect=collect,
            propose=propose,
            guard=operator.require_research_enabled,
        )
    finally:
        client.close()


def synthetic_shadow_app(root: Path) -> FastAPI:
    spec = initialize_fixture(root)
    auth = OperatorAuth.model_validate(
        {
            "issuer": "synthetic-shadow",
            "audience": "ats-shadow",
            "algorithm": "HS256",
            "verification_key": KEY,
            "synthetic_only": True,
            "identities": [
                {"subject": "fixture-human", "role": "OPERATOR", "human": True},
                {"subject": "fixture-viewer", "role": "VIEWER", "human": True},
            ],
        }
    )
    app = create_shadow_app(
        OperatorStore(root / "operator.sqlite3"),
        ShadowStore(root / "shadow.sqlite3"),
        spec,
        auth,
    )

    def issue(viewer: bool, extra: dict[str, Any]) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": "fixture-viewer" if viewer else "fixture-human",
                "iss": auth.issuer,
                "aud": auth.audience,
                "iat": now,
                "nbf": now,
                "exp": now + timedelta(minutes=5),
                "jti": str(uuid4()),
                "actor_type": "human",
                **extra,
            },
            KEY,
            algorithm="HS256",
        )

    @app.get("/fixture/session")
    def fixture_session(viewer: bool = False) -> dict[str, str]:
        return {"token": issue(viewer, {})}

    @app.post("/fixture/sign")
    def fixture_sign(command: OperatorCommand) -> dict[str, str]:
        if command.action not in ("HALT_RESEARCH", "RESUME_RESEARCH"):
            raise ValueError("shadow fixture only signs stop/resume")
        return {
            "token": issue(
                False,
                {
                    "action": command.action,
                    "target_digest": command.target_digest,
                    "command_digest": evidence_digest(command),
                },
            )
        }

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    initialize_fixture(args.root)
    supervise_shadow(partial(run_fixture_round, args.root))
    if args.serve:
        uvicorn.run(synthetic_shadow_app(args.root), host="127.0.0.1", port=args.port)
    else:
        print(
            {
                "mode": "SYNTHETIC",
                "reports": len(
                    ShadowStore(args.root / "shadow.sqlite3").reports(
                        at=datetime.now(UTC)
                    )
                ),
                "real_broker_requests": 0,
                "execution_authorized": False,
            }
        )
