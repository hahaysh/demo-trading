"""Authenticated local research operations; no broker or deployment credentials."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import jwt
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import Field, SecretStr, TypeAdapter

from ats.data.information_jobs import InformationStore
from ats.data.storage import LocalPayloadStore
from ats.domain.governance import PromotionDecision, PromotionOutcome, evidence_digest
from ats.domain.policy import PromotionPolicy
from ats.domain.research import EvaluationResult, ExperimentRun
from ats.domain.strategy import FrozenModel, Identifier, Sha256Digest, StrategySpec


class OperatorIdentity(FrozenModel):
    subject: str
    role: Literal["VIEWER", "RESEARCHER", "OPERATOR"]
    human: bool


class OperatorAuth(FrozenModel):
    issuer: str
    audience: str
    verification_key: SecretStr
    algorithm: Literal["RS256", "HS256"] = "RS256"
    synthetic_only: bool = False
    identities: tuple[OperatorIdentity, ...]

    def verify(
        self, authorization: str | None
    ) -> tuple[OperatorIdentity, dict[str, Any]]:
        if (
            authorization is None
            or not authorization.startswith("Bearer ")
            or len(authorization) > 8192
        ):
            raise HTTPException(401, "인증이 필요합니다.")
        if self.algorithm == "HS256" and not self.synthetic_only:
            raise HTTPException(503, "운영 인증 구성이 필요합니다.")
        try:
            claims = jwt.decode(
                authorization[7:],
                self.verification_key.get_secret_value(),
                algorithms=[self.algorithm],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["sub", "iss", "aud", "exp", "iat", "nbf", "jti"]},
            )
            if (
                claims["exp"] - claims["iat"] > 900
                or claims.get("actor_type") != "human"
                or claims.get("idtyp") == "app"
            ):
                raise ValueError("nonhuman or overlong token")
            identity = next(
                (item for item in self.identities if item.subject == claims["sub"]),
                None,
            )
            if identity is None or not identity.human:
                raise ValueError("unknown operator")
            return identity, claims
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
            raise HTTPException(401, "인증을 확인할 수 없습니다.") from None


class OperatorCommand(FrozenModel):
    request_id: UUID
    action: Literal[
        "HALT_RESEARCH",
        "RESUME_RESEARCH",
        "APPROVE_REVIEW",
        "REJECT_REVIEW",
        "PROMOTE",
        "ROLLBACK",
    ]
    target: Identifier
    target_digest: Sha256Digest
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    reason: Annotated[str, Field(min_length=3, max_length=1000)]


class PromotionBundle(FrozenModel):
    strategy: StrategySpec
    decision: PromotionDecision
    policy: PromotionPolicy
    evaluations: tuple[EvaluationResult, ...]
    runs: tuple[ExperimentRun, ...]
    verified_reports: tuple[Sha256Digest, ...]
    paper_sessions: tuple[str, ...]
    attestations: tuple[str, ...] = ()

    def check(self, actor: str, at: datetime) -> None:
        bundle = PromotionBundle.model_validate(self.model_dump())
        decision = bundle.decision
        if (
            decision.outcome is not PromotionOutcome.APPROVED
            or decision.approval is None
            or decision.approval.approver_id != actor
            or decision.decided_at > at
            or decision.review.strategy_digest != bundle.strategy.content_digest()
            or decision.review.strategy_version_id != bundle.strategy.version.version_id
        ):
            raise ValueError(
                "promotion is not bound to the authenticated human and strategy"
            )
        decision.validate_evidence(bundle.policy, bundle.evaluations, bundle.runs)
        if len(set(bundle.paper_sessions)) < max(
            20, bundle.policy.gates.min_paper_sessions
        ):
            raise ValueError("at least twenty verified paper sessions required")
        required = {gate.report.digest for gate in decision.review.gates}
        required.add(decision.approval.evidence.digest)
        if not required.issubset(set(bundle.verified_reports)):
            raise ValueError("gate and approval artifacts were not verified")
        for run in bundle.runs:
            if run.strategy_digest != bundle.strategy.content_digest():
                raise ValueError("engine evidence does not match strategy")


class OperatorStore(LocalPayloadStore):
    def __init__(
        self,
        database: Path,
        *,
        promotion_verifier: Callable[[PromotionBundle, datetime], None] | None = None,
    ) -> None:
        super().__init__(database)
        self._promotion_verifier = promotion_verifier

    def initialize(self) -> None:
        with self._connection(create=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS operator_state (id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS operator_audit (sequence INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, jti TEXT UNIQUE NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL, previous TEXT NOT NULL, digest TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS operator_alerts (sequence INTEGER PRIMARY KEY, severity TEXT NOT NULL, target TEXT NOT NULL, message TEXT NOT NULL, observed_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS champion_pointer (singleton INTEGER PRIMARY KEY CHECK(singleton=1), target TEXT NOT NULL, digest TEXT NOT NULL, revision INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS champion_history (sequence INTEGER PRIMARY KEY, target TEXT NOT NULL, digest TEXT NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL)"
            )

    def register_review(self, target: str, digest: str) -> None:
        self.initialize()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT digest FROM operator_state WHERE id=?", (target,)
            ).fetchone()
            if existing is not None and existing["digest"] != digest:
                raise ValueError("review target is immutable")
            connection.execute(
                "INSERT OR IGNORE INTO operator_state VALUES (?, ?, 'AWAITING_REVIEW', 0)",
                (target, digest),
            )

    def command(
        self,
        request: OperatorCommand,
        identity: OperatorIdentity,
        claims: dict[str, Any],
        *,
        promotion: PromotionBundle | None = None,
    ) -> dict[str, object]:
        request = OperatorCommand.model_validate(request.model_dump())
        if identity.role != "OPERATOR" or not identity.human:
            raise HTTPException(403, "운영자 권한이 필요합니다.")
        if (
            claims.get("action") != request.action
            or claims.get("target_digest") != request.target_digest
            or claims.get("command_digest") != evidence_digest(request)
        ):
            raise HTTPException(403, "승인 토큰의 작업 범위가 다릅니다.")
        selecting = request.action in ("PROMOTE", "ROLLBACK")
        if selecting and promotion is None:
            raise HTTPException(
                409,
                "검증·모의운영·승격 증거가 연결되지 않아 운영 전략 변경을 차단합니다.",
            )
        if selecting and promotion is not None:
            try:
                promotion.check(identity.subject, datetime.now(UTC))
                if request.target_digest != promotion.strategy.content_digest():
                    raise ValueError("promotion target mismatch")
                if self._promotion_verifier is None:
                    raise ValueError("independent signed evidence verifier required")
                self._promotion_verifier(promotion, datetime.now(UTC))
            except ValueError:
                raise HTTPException(409, "승격 증거를 검증할 수 없습니다.") from None
        self.initialize()
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM operator_audit WHERE request_id=? OR jti=?",
                (str(request.request_id), claims["jti"]),
            ).fetchone():
                raise HTTPException(409, "이미 처리한 명령입니다.")
            previous_digest = "0" * 64
            for audit in connection.execute(
                "SELECT body,previous,digest FROM operator_audit ORDER BY sequence"
            ):
                if (
                    audit["previous"] != previous_digest
                    or audit["digest"]
                    != hashlib.sha256(
                        (previous_digest + audit["body"]).encode()
                    ).hexdigest()
                ):
                    raise HTTPException(
                        409, "감사 기록 무결성 오류로 명령을 차단합니다."
                    )
                previous_digest = audit["digest"]
            row = connection.execute(
                "SELECT * FROM operator_state WHERE id=?", (request.target,)
            ).fetchone()
            if (
                row is None
                or row["digest"] != request.target_digest
                or row["revision"] != request.expected_revision
            ):
                raise HTTPException(409, "대상 또는 상태 버전이 변경됐습니다.")
            transitions = {
                "HALT_RESEARCH": "HALTED",
                "RESUME_RESEARCH": "RESEARCH_ENABLED",
                "APPROVE_REVIEW": "REVIEW_ACCEPTED_NOT_PROMOTED",
                "REJECT_REVIEW": "REJECTED",
                "PROMOTE": "SELECTED_NOT_ACTIVATED",
                "ROLLBACK": "SELECTED_NOT_ACTIVATED",
            }
            if selecting:
                pointer = connection.execute(
                    "SELECT * FROM champion_pointer WHERE singleton=1"
                ).fetchone()
                expected_champion = claims.get("expected_champion_digest")
                if expected_champion != (pointer["digest"] if pointer else None):
                    raise HTTPException(409, "운영 전략 선택이 변경됐습니다.")
                if (
                    request.action == "ROLLBACK"
                    and not connection.execute(
                        "SELECT 1 FROM champion_history WHERE digest=?",
                        (request.target_digest,),
                    ).fetchone()
                ):
                    raise HTTPException(
                        409, "이전 선택 이력이 없는 전략으로 롤백할 수 없습니다."
                    )
                revision = pointer["revision"] + 1 if pointer else 0
                connection.execute(
                    "INSERT INTO champion_pointer VALUES (1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET target=excluded.target,digest=excluded.digest,revision=excluded.revision",
                    (request.target, request.target_digest, revision),
                )
                connection.execute(
                    "INSERT INTO champion_history(target,digest,actor,request_id) VALUES (?,?,?,?)",
                    (
                        request.target,
                        request.target_digest,
                        identity.subject,
                        str(request.request_id),
                    ),
                )
            if request.action == "RESUME_RESEARCH" and row["status"] != "HALTED":
                raise HTTPException(409, "중단된 연구만 재개할 수 있습니다.")
            if (
                request.action in ("APPROVE_REVIEW", "REJECT_REVIEW")
                and row["status"] != "AWAITING_REVIEW"
            ):
                raise HTTPException(409, "검토 대기 상태가 아닙니다.")
            status = transitions[request.action]
            body = json.dumps(
                {
                    "command": request.model_dump(mode="json"),
                    "subject": identity.subject,
                    "at": datetime.now(UTC).isoformat(),
                    "status": status,
                },
                sort_keys=True,
            )
            previous_row = connection.execute(
                "SELECT digest FROM operator_audit ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous = previous_row["digest"] if previous_row else "0" * 64
            digest = hashlib.sha256((previous + body).encode()).hexdigest()
            connection.execute(
                "INSERT INTO operator_audit(request_id,jti,actor,body,previous,digest) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(request.request_id),
                    claims["jti"],
                    identity.subject,
                    body,
                    previous,
                    digest,
                ),
            )
            connection.execute(
                "UPDATE operator_state SET status=?,revision=revision+1 WHERE id=?",
                (status, request.target),
            )
            if request.action == "HALT_RESEARCH":
                connection.execute(
                    "INSERT INTO operator_alerts(severity,target,message,observed_at) VALUES ('WARNING', ?, ?, ?)",
                    (request.target, request.reason, datetime.now(UTC).isoformat()),
                )
        return {
            "target": request.target,
            "status": status,
            "revision": request.expected_revision + 1,
            "audit_digest": digest,
            "promoted": False,
        }

    def require_research_enabled(self, target: str) -> None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM operator_state WHERE id=?", (target,)
            ).fetchone()
            if row is None or row["status"] != "RESEARCH_ENABLED":
                raise ValueError("research campaign is not explicitly enabled")

    def snapshot(self) -> dict[str, Any]:
        self.initialize()
        with self._connection() as connection:
            previous = "0" * 64
            audits = connection.execute(
                "SELECT * FROM operator_audit ORDER BY sequence"
            ).fetchall()
            for row in audits:
                if (
                    row["previous"] != previous
                    or row["digest"]
                    != hashlib.sha256((previous + row["body"]).encode()).hexdigest()
                ):
                    raise ValueError("audit chain integrity failure")
                previous = row["digest"]
            return {
                "reviews": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM operator_state ORDER BY id"
                    )
                ],
                "alerts": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM operator_alerts ORDER BY sequence DESC LIMIT 100"
                    )
                ],
                "audit_count": len(audits),
                "audit_head": previous,
                "broker_execution_enabled": False,
            }


def create_operator_app(
    store: OperatorStore,
    auth: OperatorAuth,
    status_provider: Callable[[], dict[str, Any]] | None = None,
    promotion_provider: Callable[[str], PromotionBundle] | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=HTMLResponse)
    def home() -> HTMLResponse:
        return HTMLResponse(
            Path(__file__).with_name("operator.html").read_text(encoding="utf-8"),
            headers={
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/operator.js")
    def script() -> FileResponse:
        return FileResponse(
            Path(__file__).with_name("operator.js"), media_type="text/javascript"
        )

    @app.get("/api/status")
    def status(
        response: Response, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        identity, _ = auth.verify(authorization)
        result = store.snapshot()
        result["role"] = identity.role
        if status_provider:
            result["workflow"] = TypeAdapter(dict[str, Any]).validate_python(
                status_provider()
            )
        return result

    @app.post("/api/commands")
    def command(
        request: OperatorCommand, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, object]:
        identity, claims = auth.verify(authorization)
        if identity.role != "OPERATOR" or claims.get(
            "command_digest"
        ) != evidence_digest(request):
            raise HTTPException(403, "명령에 바인딩된 운영자 승인이 필요합니다.")
        promotion = (
            promotion_provider(request.target_digest)
            if request.action in ("PROMOTE", "ROLLBACK") and promotion_provider
            else None
        )
        return store.command(request, identity, claims, promotion=promotion)

    @app.post("/api/commands/prepare")
    def prepare(
        request: OperatorCommand, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        identity, _ = auth.verify(authorization)
        if identity.role != "OPERATOR":
            raise HTTPException(403, "운영자 권한이 필요합니다.")
        state = store.snapshot()
        if not any(
            item["id"] == request.target
            and item["digest"] == request.target_digest
            and item["revision"] == request.expected_revision
            for item in state["reviews"]
        ):
            raise HTTPException(409, "대상 상태가 변경됐습니다.")
        return {
            "command": request.model_dump(mode="json"),
            "command_digest": evidence_digest(request),
        }

    return app


def workflow_status(
    information: InformationStore, research: LocalPayloadStore
) -> dict[str, Any]:
    jobs = information.status() if information.database.exists() else ()
    trials: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    if research.database.exists():
        import sqlite3

        with sqlite3.connect(
            f"{research.database.as_uri()}?mode=ro", uri=True
        ) as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='research_trials'"
            ).fetchone():
                for row in connection.execute(
                    "SELECT record FROM research_trials ORDER BY campaign,sequence"
                ):
                    record = TypeAdapter(dict[str, Any]).validate_json(row[0])
                    trials.append(
                        {
                            key: record[key]
                            for key in (
                                "sequence",
                                "parameters",
                                "proposer_receipt",
                                "score",
                                "status",
                            )
                        }
                    )
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='research_validation'"
            ).fetchone():
                for row in connection.execute(
                    "SELECT campaign,state,calls,attempts,result FROM research_validation ORDER BY campaign"
                ):
                    result = (
                        TypeAdapter(dict[str, Any]).validate_json(row[4])
                        if row[4]
                        else {}
                    )
                    gate: dict[str, Any] = result.get("review_gate") or {}
                    checks = TypeAdapter(dict[str, bool]).validate_python(
                        gate.get("checks", {})
                    )
                    dsr: dict[str, Any] = result.get("dsr_diagnostic") or {}
                    dependence: dict[str, Any] = (
                        result.get("dependence_diagnostic") or {}
                    )
                    validations.append(
                        {
                            "campaign": row[0],
                            "state": row[1],
                            "reserved_engine_calls": row[2],
                            "attempts": row[3],
                            "corrected_lower_bound": result.get(
                                "corrected_lower_bound"
                            ),
                            "research_frozen": True,
                            "review_eligible": gate.get("eligible_for_review"),
                            "failed_checks": [
                                name
                                for name, passed in checks.items()
                                if passed is not True
                            ],
                            "dsr_status": dsr.get("status"),
                            "dependence_status": dependence.get("status"),
                        }
                    )
    return {
        "health": [
            item.model_dump(mode="json")
            for item in information.health(at=datetime.now(UTC))
        ]
        if information.database.exists()
        else [],
        "jobs": [
            {
                "run_id": run.run_id,
                "status": run.status,
                "attempts": run.attempts,
                "updated_at": run.updated_at.isoformat(),
            }
            for run in jobs
        ],
        "trials": trials,
        "validations": validations,
    }


def synthetic_app(root: Path, research_path: Path) -> FastAPI:
    from datetime import timedelta
    from uuid import uuid4

    from fastapi.middleware.trustedhost import TrustedHostMiddleware

    key = "public-synthetic-fixture-key-not-a-real-credential-0000"
    store = OperatorStore(root / "operator.sqlite3")
    digest = "sha256:" + hashlib.sha256(b"synthetic-rsi").hexdigest()
    store.register_review("synthetic-rsi", digest)
    auth = OperatorAuth.model_validate(
        {
            "issuer": "synthetic-issuer",
            "audience": "ats-operator",
            "verification_key": key,
            "algorithm": "HS256",
            "synthetic_only": True,
            "identities": [
                {"subject": "fixture-human", "role": "OPERATOR", "human": True},
                {"subject": "fixture-viewer", "role": "VIEWER", "human": True},
            ],
        }
    )
    app = create_operator_app(
        store,
        auth,
        lambda: workflow_status(
            InformationStore(root / "information.sqlite3"),
            LocalPayloadStore(research_path),
        ),
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    def issue(subject: str, extra: dict[str, Any]) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": subject,
                "iss": auth.issuer,
                "aud": auth.audience,
                "iat": now,
                "nbf": now,
                "exp": now + timedelta(minutes=5),
                "jti": str(uuid4()),
                "actor_type": "human",
                **extra,
            },
            key,
            algorithm="HS256",
        )

    @app.get("/fixture/session")
    def fixture_session(viewer: bool = False) -> dict[str, str]:
        return {"token": issue("fixture-viewer" if viewer else "fixture-human", {})}

    @app.post("/fixture/sign")
    def fixture_sign(command: OperatorCommand) -> dict[str, str]:
        return {
            "token": issue(
                "fixture-human",
                {
                    "action": command.action,
                    "target_digest": command.target_digest,
                    "command_digest": evidence_digest(command),
                },
            )
        }

    return app


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        description="Loopback-only synthetic operator demo; no real identity or broker access."
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--research", required=True)
    parser.add_argument("--port", type=int, default=8766)
    arguments = parser.parse_args()
    uvicorn.run(
        synthetic_app(Path(arguments.root), Path(arguments.research)),
        host="127.0.0.1",
        port=arguments.port,
    )
