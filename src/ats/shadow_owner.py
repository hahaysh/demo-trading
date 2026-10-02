"""Owner-driven fixed observation lifecycle; no OS registration or auto-extension."""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Self

from pydantic import SecretStr, model_validator

from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.domain.prices import SEOUL
from ats.domain.strategy import FrozenModel, StrategySpec
from ats.kis_readonly import ReadOnlyAccessPermit, WindowsCredentialProvider
from ats.operator import OperatorAuth
from ats.readonly_owner import (
    OwnerAction,
    OwnerAuthority,
    protected_prompt,
    require_owner_storage,
)
from ats.readonly_runtime import ReadOnlyRuntime
from ats.readonly_smoke import supervise_owner_result
from ats.shadow_campaign import FiveDayPlan, FiveDayStore, run_observation_day


class FiveDayExecution(FrozenModel):
    plan: FiveDayPlan
    permit: ReadOnlyAccessPermit
    source_policy: SourceAllowlist
    symbol: str

    @model_validator(mode="after")
    def bind_scope(self) -> Self:
        if (
            evidence_digest(self.permit) != self.plan.session.read_permit_digest
            or evidence_digest(self.source_policy) != self.permit.source_policy_digest
            or self.symbol not in self.permit.symbols
            or not self.permit.retain_observations
            or self.permit.account_id != self.plan.session.account_id
            or self.permit.synthetic_only != (self.plan.session.mode == "SYNTHETIC")
            or self.permit.starts_at != self.plan.session.starts_at
            or self.permit.expires_at != self.plan.session.expires_at
        ):
            raise ValueError("five-day execution authority mismatch")
        if (
            not {"QUOTE", "BALANCE", "CAPACITY", "OPEN_ORDERS", "HISTORY"}.issubset(
                self.permit.operations
            )
            or self.permit.max_token_requests != 5
        ):
            raise ValueError(
                "five daily token attempts and complete observation routes required"
            )
        return self


def observe_worker(
    bundle: FiveDayExecution,
    candidate: bytes,
    program: bytes,
    calendar: bytes,
    ledger: Path,
    control: Path,
) -> dict[str, Any]:
    import logging

    logging.disable(logging.CRITICAL)
    authority = OwnerAuthority(control)
    store = FiveDayStore(ledger)

    def guard(target: str) -> None:
        if target != bundle.plan.session.session_id:
            raise ValueError("observation target mismatch")
        authority.require_approval(
            action="OBSERVE_FIVE_DAYS", target_digest=evidence_digest(bundle)
        )

    day = datetime.now(UTC).astimezone(SEOUL).date()
    report = run_observation_day(
        bundle.plan,
        day=day,
        candidate=candidate,
        program=program,
        calendar_evidence=calendar,
        permit=bundle.permit,
        source_policy=bundle.source_policy,
        store=store,
        runtime=ReadOnlyRuntime(control),
        credentials=WindowsCredentialProvider(),
        operator_guard=guard,
        clock=lambda: datetime.now(UTC),
        symbol=bundle.symbol,
    )
    return {
        "session": day.isoformat(),
        "state": "SUCCEEDED" if report else "ALREADY_RECORDED",
        "broker_orders_sent": 0,
        "execution_authorized": False,
        "actual_paper_sessions": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fixed five-session owner workflow; no implicit collection"
    )
    parser.add_argument(
        "action",
        choices=(
            "preflight",
            "register",
            "observe",
            "halt",
            "resume",
            "recover-collector",
            "final",
            "withdraw",
        ),
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--program", type=Path, required=True)
    parser.add_argument("--calendar-evidence", type=Path, required=True)
    parser.add_argument("--auth", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--control", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--show-report", action="store_true")
    args = parser.parse_args()
    try:
        bundle = FiveDayExecution.model_validate_json(args.bundle.read_bytes())
        candidate = args.candidate.read_bytes()
        program = args.program.read_bytes()
        calendar = args.calendar_evidence.read_bytes()
        bundle.plan.verify_artifacts(candidate, calendar)
        strategy = StrategySpec.model_validate_json(candidate)
        if (
            "sha256:" + hashlib.sha256(program).hexdigest()
            != strategy.code_artifact.digest
        ):
            raise ValueError("selected executable mismatch")
        if args.action == "preflight":
            print(
                json.dumps(
                    {
                        "approval_target": evidence_digest(bundle),
                        "candidate_digest": strategy.content_digest(),
                        "windows": [
                            item.model_dump(mode="json") for item in bundle.plan.windows
                        ],
                        "max_calls": bundle.permit.max_calls,
                        "network_started": False,
                    }
                )
            )
            return
        if (
            not args.execute
            or args.auth is None
            or args.ledger is None
            or args.control is None
            or not sys.stdin.isatty()
        ):
            raise ValueError(
                "explicit owner terminal execution and protected configuration required"
            )
        auth = OperatorAuth.model_validate_json(args.auth.read_bytes())
        require_owner_storage(
            args.control, args.ledger, session_id=bundle.plan.session.session_id
        )
        if (
            auth.synthetic_only
            or auth.algorithm != "RS256"
            or bundle.plan.session.mode != "PRODUCTION_READ_ONLY"
        ):
            raise ValueError(
                "production owner command requires real RS256 configuration"
            )
        token = SecretStr(protected_prompt("Owner access or scoped approval token: "))
        identity, _ = auth.verify("Bearer " + token.get_secret_value())
        if identity.role != "OPERATOR":
            raise ValueError("operator identity required")
        authority = OwnerAuthority(args.control)
        store = FiveDayStore(args.ledger)
        now = datetime.now(UTC)

        def approve(action: OwnerAction) -> None:
            authority.consume(
                auth, token, action=action, target_digest=evidence_digest(bundle)
            )

        if args.action == "register":
            approve("OBSERVE_FIVE_DAYS")
            store.register_plan(
                bundle.plan, candidate=candidate, calendar_evidence=calendar, at=now
            )
        else:
            authority.require_approval(
                action="OBSERVE_FIVE_DAYS", target_digest=evidence_digest(bundle)
            )
            store.sweep_retention(bundle.plan, at=now)
            if args.action == "observe":
                print(
                    json.dumps(
                        supervise_owner_result(
                            partial(
                                observe_worker,
                                bundle,
                                candidate,
                                program,
                                calendar,
                                args.ledger,
                                args.control,
                            )
                        )
                    )
                )
            elif args.action == "halt":
                store.halt_plan(
                    bundle.plan, authorize=lambda: approve("HALT_OBSERVATION")
                )
            elif args.action == "resume":
                store.recover(
                    bundle.plan.session,
                    at=now,
                    guard=lambda target: None,
                    authorize=lambda: approve("RESUME_OBSERVATION"),
                )
            elif args.action == "recover-collector":
                ReadOnlyRuntime(args.control).recover_owner(
                    bundle.permit, authorize=lambda: approve("RECOVER_COLLECTOR")
                )
            elif args.action == "withdraw":
                store.withdraw_plan(
                    bundle.plan, authorize=lambda: approve("WITHDRAW_OBSERVATION")
                )
            else:
                report = store.final_report(bundle.plan, at=now)
                if args.show_report:
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                    return
                print(
                    json.dumps(
                        {
                            "status": report["status"],
                            "verdict": report["verdict"],
                            "report_in_approved_ledger": True,
                            "execution_authorized": False,
                        }
                    )
                )
    except Exception:
        print(
            "Observation command rejected or failed; no account details emitted.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
