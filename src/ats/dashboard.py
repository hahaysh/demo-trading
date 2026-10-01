"""Export a read-only, offline dashboard from an existing synthetic report."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from ats.backtest.candidates import CandidateExperiment
from ats.backtest.native import NativeBacktestReport
from ats.domain.execution import RiskDecision
from ats.domain.strategy import FrozenModel, Sha256Digest


class RiskSmoke(FrozenModel):
    allow: RiskDecision
    kill_switch: RiskDecision


class DashboardReport(FrozenModel):
    mode: Literal["SYNTHETIC_OFFLINE_ONLY"]
    deployment_ready: Literal[False]
    promoted: Literal[False]
    baseline: NativeBacktestReport
    candidates: tuple[CandidateExperiment, ...]
    risk_smoke: RiskSmoke
    blockers: tuple[str, ...]


class QuoteEvidence(FrozenModel):
    source_id: Literal["kis-market"]
    observed_at: AwareDatetime
    raw_payload_digest: Sha256Digest
    policy_digest: Sha256Digest
    row_count: Annotated[int, Field(strict=True, ge=0, le=100)]
    coverage_verified: Literal[False]
    persisted: Literal[False]


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Dashboard input exceeds 4 MiB")
    return data


def _evidence(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    document = _read_bounded(path).decode("utf-8")
    section = document.split("## User-Reported Production Smoke", 1)
    if len(section) != 2:
        raise ValueError("Missing user-reported KIS evidence section")
    blocks = section[1].split("```json", 1)
    if len(blocks) != 2 or "```" not in blocks[1]:
        raise ValueError("Missing KIS evidence JSON")
    return QuoteEvidence.model_validate_json(blocks[1].split("```", 1)[0]).model_dump(
        mode="json"
    )


def export_dashboard(
    report_path: Path, output: Path, *, evidence_path: Path | None = None
) -> None:
    raw = _read_bounded(report_path)
    report = DashboardReport.model_validate_json(raw)
    runs = [report.baseline, *(candidate.report for candidate in report.candidates)]
    for run in runs:
        if not run.equity_curve:
            raise ValueError("Dashboard requires a nonempty equity curve")
        if not run.net_return.is_finite() or not run.max_drawdown.is_finite():
            raise ValueError("Dashboard metrics must be finite")
        if any(
            left.at >= right.at
            for left, right in zip(run.equity_curve, run.equity_curve[1:], strict=False)
        ):
            raise ValueError("Equity timestamps must increase")
    payload = {
        "report": report.model_dump(mode="json"),
        "report_name": report_path.name,
        "report_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "exported_at": datetime.now(UTC).isoformat(),
        "quote_evidence": _evidence(evidence_path),
    }
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False)
    encoded = (
        encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    template = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    page = template.replace("__ATS_REPORT_DATA__", encoded)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(page)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    try:
        export_dashboard(args.report, args.output, evidence_path=args.evidence)
    except (ValueError, OSError):
        print("Dashboard export failed: check report, evidence and a new output path.")
        return 1
    print(f"Offline dashboard created: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
