"""Offline verification of independently signed promotion evidence."""

import hashlib
import math
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated, Literal

import jwt
from pydantic import AwareDatetime, Field, SecretStr

from ats.domain.governance import evidence_digest
from ats.domain.prices import SEOUL
from ats.domain.strategy import FrozenModel, Identifier, Sha256Digest

if TYPE_CHECKING:
    from ats.operator import PromotionBundle


class EvidenceAuthority(FrozenModel):
    key_id: Identifier
    issuer: str
    subject: str
    kind: Literal["REPORT", "PAPER_SESSION"]
    verification_key: SecretStr
    algorithm: Literal["RS256", "HS256"] = "RS256"


class AttestedArtifact(FrozenModel):
    kind: Literal["REPORT", "PAPER_SESSION"]
    mode: Literal["SYNTHETIC", "KIS_PAPER"]
    strategy_digest: Sha256Digest
    policy_digest: Sha256Digest
    account_alias: Identifier | None = None
    session: date | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime
    reconciliation_complete: bool
    source_artifacts: Annotated[
        tuple[Sha256Digest, ...], Field(min_length=1, max_length=100)
    ]


class SignedPromotionVerifier:
    def __init__(
        self,
        authorities: tuple[EvidenceAuthority, ...],
        read_artifact: Callable[[str], bytes],
        *,
        account_alias: str,
        synthetic_only: bool = False,
    ) -> None:
        if not authorities or len({item.key_id for item in authorities}) != len(
            authorities
        ):
            raise ValueError("unique independent evidence authorities required")
        if (
            any(item.algorithm == "HS256" for item in authorities)
            and not synthetic_only
        ):
            raise ValueError("symmetric test signatures are synthetic-only")
        self.authorities = {item.key_id: item for item in authorities}
        self.read_artifact = read_artifact
        self.account_alias = account_alias
        self.synthetic_only = synthetic_only

    def _bytes(self, digest: str) -> bytes:
        payload = self.read_artifact(digest)
        if (
            not 0 < len(payload) <= 1024 * 1024
            or "sha256:" + hashlib.sha256(payload).hexdigest() != digest
        ):
            raise ValueError("evidence artifact bytes do not match digest")
        return payload

    def verify_artifact(
        self,
        token: str,
        *,
        strategy_digest: str,
        policy_digest: str,
        at: datetime,
    ) -> tuple[str, str, AttestedArtifact]:
        if at.tzinfo is None or at.utcoffset() is None or not 0 < len(token) <= 16384:
            raise ValueError("bounded attestation and aware verification time required")
        try:
            authority = self.authorities[jwt.get_unverified_header(token)["kid"]]
            claims = jwt.decode(
                token,
                authority.verification_key.get_secret_value(),
                algorithms=[authority.algorithm],
                issuer=authority.issuer,
                audience="ats-promotion-evidence",
                options={
                    "require": [
                        "iss",
                        "sub",
                        "aud",
                        "jti",
                        "iat",
                        "nbf",
                        "exp",
                        "artifact_digest",
                    ],
                    "verify_iat": False,
                    "verify_nbf": False,
                    "verify_exp": False,
                },
            )
            if (
                claims["sub"] != authority.subject
                or not isinstance(claims["jti"], str)
                or not claims["jti"]
            ):
                raise ValueError("untrusted evidence subject")
            for name in ("iat", "nbf", "exp"):
                if type(claims[name]) not in (int, float) or not math.isfinite(
                    claims[name]
                ):
                    raise ValueError("invalid attestation time")
            if (
                not claims["nbf"] <= at.timestamp() < claims["exp"]
                or not claims["iat"] <= at.timestamp()
                or not 0 < claims["exp"] - claims["iat"] <= 86400
            ):
                raise ValueError("expired, future or overlong evidence attestation")
            digest = claims["artifact_digest"]
            if not isinstance(digest, str):
                raise ValueError("invalid evidence digest")
            document = AttestedArtifact.model_validate_json(self._bytes(digest))
            if (
                document.kind != authority.kind
                or document.mode
                != ("SYNTHETIC" if self.synthetic_only else "KIS_PAPER")
                or document.strategy_digest != strategy_digest
                or document.policy_digest != policy_digest
                or not document.started_at <= document.completed_at <= at
                or document.completed_at.timestamp() > claims["iat"]
                or not document.reconciliation_complete
            ):
                raise ValueError("evidence scope, period or reconciliation mismatch")
            if document.kind == "PAPER_SESSION" and (
                document.account_alias != self.account_alias
                or document.session is None
                or document.started_at.astimezone(SEOUL).date() != document.session
                or document.completed_at.astimezone(SEOUL).date() != document.session
            ):
                raise ValueError("paper account or session mismatch")
            if len(set(document.source_artifacts)) != len(document.source_artifacts):
                raise ValueError("duplicate source artifacts")
            for source in document.source_artifacts:
                self._bytes(source)
            return claims["jti"], digest, document
        except (jwt.InvalidTokenError, KeyError, TypeError, OSError) as error:
            raise ValueError("independent evidence verification failed") from error

    def __call__(self, bundle: "PromotionBundle", at: datetime) -> None:
        if not 1 <= len(bundle.attestations) <= 512:
            raise ValueError("independently signed attestations required")
        reports: set[str] = set()
        report_sources: set[str] = set()
        sessions: set[str] = set()
        identifiers: set[str] = set()
        paper_sources: set[str] = set()
        for token in bundle.attestations:
            identifier, digest, document = self.verify_artifact(
                token,
                strategy_digest=bundle.strategy.content_digest(),
                policy_digest=evidence_digest(bundle.policy),
                at=at,
            )
            if document.completed_at > bundle.decision.review.reviewed_at:
                raise ValueError("evidence was completed after the human review")
            if identifier in identifiers:
                raise ValueError("replayed evidence attestation")
            identifiers.add(identifier)
            if document.kind == "REPORT":
                if digest in reports:
                    raise ValueError("duplicate report attestation")
                reports.add(digest)
                report_sources.update(document.source_artifacts)
            else:
                if document.started_at < bundle.strategy.version.created_at:
                    raise ValueError("paper evidence precedes strategy creation")
                session = str(document.session)
                if session in sessions or paper_sources.intersection(
                    document.source_artifacts
                ):
                    raise ValueError(
                        "duplicate paper session or reused broker evidence"
                    )
                sessions.add(session)
                paper_sources.update(document.source_artifacts)
        if reports != set(bundle.verified_reports) or sessions != set(
            bundle.paper_sessions
        ):
            raise ValueError("claimed reports or sessions differ from signed evidence")
        if len(sessions) < max(20, bundle.policy.gates.min_paper_sessions):
            raise ValueError("insufficient independently attested paper sessions")
        required_sources = {
            "sha256:" + hashlib.sha256(item.model_dump_json().encode()).hexdigest()
            for item in (*bundle.runs, *bundle.evaluations)
        }
        if not required_sources.issubset(report_sources):
            raise ValueError(
                "signed reports do not bind current run and evaluation bytes"
            )
