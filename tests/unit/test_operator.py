import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ats.domain.governance import evidence_digest
from ats.evidence import AttestedArtifact, EvidenceAuthority, SignedPromotionVerifier
from ats.operator import (
    OperatorAuth,
    OperatorCommand,
    OperatorStore,
    create_operator_app,
    synthetic_app,
)

KEY = "public-synthetic-fixture-key-not-a-real-credential-0000"
DIGEST = "sha256:" + "a" * 64


def test_independent_attestation_checks_bytes_scope_time_and_signature() -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=UTC)
    payload = b"synthetic broker session, not real evidence"
    raw_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    document = AttestedArtifact(
        kind="PAPER_SESSION",
        mode="SYNTHETIC",
        strategy_digest=DIGEST,
        policy_digest=DIGEST,
        account_alias="fixture-paper",
        session=now.date(),
        started_at=now - timedelta(hours=3),
        completed_at=now - timedelta(hours=1),
        reconciliation_complete=True,
        source_artifacts=(raw_digest,),
    )
    encoded = document.model_dump_json().encode()
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    artifacts = {digest: encoded, raw_digest: payload}
    authority = EvidenceAuthority.model_validate(
        {
            "key_id": "fixture-observer",
            "issuer": "fixture-issuer",
            "subject": "independent-observer",
            "kind": "PAPER_SESSION",
            "verification_key": KEY,
            "algorithm": "HS256",
        }
    )
    verifier = SignedPromotionVerifier(
        (authority,),
        artifacts.__getitem__,
        account_alias="fixture-paper",
        synthetic_only=True,
    )
    claims = {
        "iss": authority.issuer,
        "sub": authority.subject,
        "aud": "ats-promotion-evidence",
        "jti": "fixture-attestation",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(now.timestamp()) + 300,
        "artifact_digest": digest,
    }
    signed = jwt.encode(
        claims, KEY, algorithm="HS256", headers={"kid": authority.key_id}
    )
    assert (
        verifier.verify_artifact(
            signed, strategy_digest=DIGEST, policy_digest=DIGEST, at=now
        )[2]
        == document
    )
    with pytest.raises(ValueError, match="synthetic-only"):
        SignedPromotionVerifier(
            (authority,), artifacts.__getitem__, account_alias="fixture-paper"
        )
    with pytest.raises(ValueError, match="scope"):
        verifier.verify_artifact(
            signed, strategy_digest="sha256:" + "b" * 64, policy_digest=DIGEST, at=now
        )
    with pytest.raises(ValueError, match="expired"):
        verifier.verify_artifact(
            signed,
            strategy_digest=DIGEST,
            policy_digest=DIGEST,
            at=now + timedelta(minutes=6),
        )
    forged = jwt.encode(
        claims, KEY + "wrong", algorithm="HS256", headers={"kid": authority.key_id}
    )
    with pytest.raises(ValueError):
        verifier.verify_artifact(
            forged, strategy_digest=DIGEST, policy_digest=DIGEST, at=now
        )
    artifacts[raw_digest] = b"changed bytes"
    with pytest.raises(ValueError, match="bytes"):
        verifier.verify_artifact(
            signed, strategy_digest=DIGEST, policy_digest=DIGEST, at=now
        )


def token(subject: str = "human", **claims: object) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "iss": "synthetic-issuer",
            "aud": "ats-operator",
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
            "jti": str(uuid4()),
            "actor_type": "human",
            **claims,
        },
        KEY,
        algorithm="HS256",
    )


def test_rsa_operator_auth_rejects_algorithm_substitution() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    auth = OperatorAuth.model_validate(
        {
            "issuer": "synthetic-rsa-issuer",
            "audience": "synthetic-rsa-audience",
            "verification_key": public,
            "algorithm": "RS256",
            "synthetic_only": True,
            "identities": [
                {"subject": "fixture-human", "role": "VIEWER", "human": True}
            ],
        }
    )
    now = datetime.now(UTC)
    claims = {
        "sub": "fixture-human",
        "iss": auth.issuer,
        "aud": auth.audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "jti": "synthetic-rsa-test",
        "actor_type": "human",
    }
    signed = jwt.encode(claims, private, algorithm="RS256")
    assert auth.verify("Bearer " + signed)[0].role == "VIEWER"
    substituted = jwt.encode(claims, KEY, algorithm="HS256")
    with pytest.raises(HTTPException) as denied:
        auth.verify("Bearer " + substituted)
    assert denied.value.status_code == 401


def test_operator_auth_roles_replay_and_halt_audit(tmp_path: Path) -> None:
    store = OperatorStore(tmp_path / "operator.sqlite3")
    store.register_review("candidate-1", DIGEST)
    with pytest.raises(ValueError, match="explicitly enabled"):
        store.require_research_enabled("missing")
    with pytest.raises(ValueError, match="explicitly enabled"):
        store.require_research_enabled("candidate-1")
    auth = OperatorAuth.model_validate(
        {
            "issuer": "synthetic-issuer",
            "audience": "ats-operator",
            "verification_key": KEY,
            "algorithm": "HS256",
            "synthetic_only": True,
            "identities": [
                {"subject": "human", "role": "OPERATOR", "human": True},
                {"subject": "viewer", "role": "VIEWER", "human": True},
            ],
        }
    )
    client = cast(httpx.Client, TestClient(create_operator_app(store, auth)))
    assert isinstance(client, httpx.Client)
    assert client.get("/api/status").status_code == 401
    assert (
        client.get(
            "/api/status", headers={"Authorization": "Bearer " + token(aud="wrong")}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/status",
            headers={"Authorization": "Bearer " + token(actor_type="agent")},
        ).status_code
        == 401
    )
    body = {
        "request_id": str(uuid4()),
        "action": "HALT_RESEARCH",
        "target": "candidate-1",
        "target_digest": DIGEST,
        "expected_revision": 0,
        "reason": "Synthetic halt test",
    }
    claims = {
        "action": "HALT_RESEARCH",
        "target_digest": DIGEST,
        "command_digest": evidence_digest(OperatorCommand.model_validate(body)),
    }
    assert (
        client.post(
            "/api/commands",
            json=body,
            headers={"Authorization": "Bearer " + token("viewer", **claims)},
        ).status_code
        == 403
    )
    header = {"Authorization": "Bearer " + token(**claims)}
    prepared = client.post(
        "/api/commands/prepare",
        json=body,
        headers={"Authorization": "Bearer " + token()},
    )
    assert prepared.status_code == 200
    assert prepared.json()["command_digest"] == claims["command_digest"]
    assert client.post("/api/commands", json=body, headers=header).status_code == 200
    assert client.post("/api/commands", json=body, headers=header).status_code == 409
    altered = {**body, "reason": "Changed after authorization"}
    assert client.post("/api/commands", json=altered, headers=header).status_code == 403
    result = client.get(
        "/api/status", headers={"Authorization": "Bearer " + token()}
    ).json()
    assert result["reviews"][0]["status"] == "HALTED" and result["audit_count"] == 1
    assert result["alerts"] and result["broker_execution_enabled"] is False
    with pytest.raises(ValueError, match="explicitly enabled"):
        OperatorStore(store.database).require_research_enabled("candidate-1")
    promotion = {
        **body,
        "request_id": str(uuid4()),
        "action": "PROMOTE",
        "expected_revision": 1,
    }
    assert (
        client.post(
            "/api/commands",
            json=promotion,
            headers={
                "Authorization": "Bearer "
                + token(
                    action="PROMOTE",
                    target_digest=DIGEST,
                    command_digest=evidence_digest(
                        OperatorCommand.model_validate(promotion)
                    ),
                )
            },
        ).status_code
        == 409
    )
    resume = {
        **body,
        "request_id": str(uuid4()),
        "action": "RESUME_RESEARCH",
        "expected_revision": 1,
    }
    assert (
        client.post(
            "/api/commands",
            json=resume,
            headers={
                "Authorization": "Bearer "
                + token(
                    action="RESUME_RESEARCH",
                    target_digest=DIGEST,
                    command_digest=evidence_digest(
                        OperatorCommand.model_validate(resume)
                    ),
                )
            },
        ).status_code
        == 200
    )
    OperatorStore(store.database).require_research_enabled("candidate-1")
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE operator_audit SET body='tampered' WHERE sequence=1")
    halt = {**body, "request_id": str(uuid4()), "expected_revision": 2}
    response = client.post(
        "/api/commands",
        json=halt,
        headers={
            "Authorization": "Bearer "
            + token(
                action="HALT_RESEARCH",
                target_digest=DIGEST,
                command_digest=evidence_digest(OperatorCommand.model_validate(halt)),
            )
        },
    )
    assert response.status_code == 409 and "무결성" in response.json()["detail"]


def test_synthetic_operator_page_preparation_and_expired_token(tmp_path: Path) -> None:
    client = cast(
        httpx.Client,
        TestClient(synthetic_app(tmp_path, tmp_path / "absent-research.sqlite3")),
    )
    assert isinstance(client, httpx.Client)
    assert client.get("/").status_code == 200
    assert "default-src 'none'" in client.get("/").headers["content-security-policy"]
    assert client.get("/operator.js").status_code == 200
    assert (
        client.get(
            "/api/status",
            headers={
                "Authorization": "Bearer "
                + token(exp=datetime.now(UTC) - timedelta(seconds=1))
            },
        ).status_code
        == 401
    )
    bearer = client.get("/fixture/session").json()["token"]
    result = client.get("/api/status", headers={"Authorization": "Bearer " + bearer})
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.json()["workflow"] == {
        "jobs": [],
        "trials": [],
        "validations": [],
        "health": [],
    }
    assert (
        client.get(
            "/fixture/session", headers={"host": "untrusted.example"}
        ).status_code
        == 400
    )


def test_operator_distinguishes_execution_success_from_review_gate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "research.sqlite3"
    evidence = {
        "review_gate": {
            "eligible_for_review": False,
            "checks": {"sample_size": False, "paired_engines": True},
        },
        "dsr_diagnostic": {"status": "INSUFFICIENT_EVIDENCE"},
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE research_validation (campaign TEXT,state TEXT,calls INTEGER,attempts INTEGER,result TEXT)"
        )
        connection.execute(
            "INSERT INTO research_validation VALUES ('fixture','SUCCEEDED',8,1,?)",
            (json.dumps(evidence),),
        )
    client = cast(httpx.Client, TestClient(synthetic_app(tmp_path, database)))
    bearer = client.get("/fixture/session").json()["token"]
    result = client.get(
        "/api/status", headers={"Authorization": "Bearer " + bearer}
    ).json()["workflow"]["validations"][0]
    assert result["state"] == "SUCCEEDED" and result["review_eligible"] is False
    assert result["failed_checks"] == ["sample_size"]
    assert result["dsr_status"] == "INSUFFICIENT_EVIDENCE"
