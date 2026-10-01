import json
from pathlib import Path

import pytest

from ats.dashboard import export_dashboard
from ats.demo import run_demo


@pytest.fixture
def report_path(tmp_path: Path) -> Path:
    run_demo(tmp_path / "run")
    return tmp_path / "run" / "report.json"


def test_export_renders_existing_report_without_modifying_it(tmp_path: Path) -> None:
    run_demo(tmp_path / "run")
    report = tmp_path / "run" / "report.json"
    original = report.read_bytes()
    output = tmp_path / "dashboard.html"
    export_dashboard(report, output)
    page = output.read_text(encoding="utf-8")
    assert "__ATS_REPORT_DATA__" not in page
    assert "connect-src 'none'" in page
    assert "SYNTHETIC_OFFLINE_ONLY" in page
    assert 'lang="ko"' in page
    assert "실주문 비활성" in page
    assert report.read_bytes() == original
    embedded = page.split('<script id="report-data" type="application/json">', 1)[
        1
    ].split("</script>", 1)[0]
    assert json.loads(embedded)["report"] == json.loads(original)
    with pytest.raises(FileExistsError):
        export_dashboard(report, output)


def test_report_text_cannot_escape_json_script(tmp_path: Path) -> None:
    report = run_demo(tmp_path / "run")
    report["blockers"] = ["</script><script>alert(1)</script>"]
    path = tmp_path / "run" / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    output = tmp_path / "dashboard.html"
    export_dashboard(path, output)
    page = output.read_text(encoding="utf-8")
    assert "</script><script>alert(1)" not in page
    assert "\\u003c/script\\u003e" in page


def test_invalid_report_creates_no_output(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text('{"mode":"LIVE"}', encoding="utf-8")
    output = tmp_path / "dashboard.html"
    with pytest.raises(ValueError):
        export_dashboard(path, output)
    assert not output.exists()


def test_quote_evidence_is_optional_and_exact(
    report_path: Path, tmp_path: Path
) -> None:
    evidence = {
        "source_id": "kis-market",
        "observed_at": "2026-09-30T17:00:21Z",
        "raw_payload_digest": "sha256:" + "a" * 64,
        "policy_digest": "sha256:" + "b" * 64,
        "row_count": 1,
        "coverage_verified": False,
        "persisted": False,
    }
    document = tmp_path / "evidence.md"
    document.write_text(
        "## User-Reported Production Smoke\n```json\n"
        + json.dumps(evidence)
        + "\n```\n",
        encoding="utf-8",
    )
    output = tmp_path / "dashboard.html"
    export_dashboard(report_path, output, evidence_path=document)
    embedded = (
        output.read_text(encoding="utf-8")
        .split('<script id="report-data" type="application/json">', 1)[1]
        .split("</script>", 1)[0]
    )
    assert json.loads(embedded)["quote_evidence"] == evidence


@pytest.mark.parametrize("field,value", [("equity_curve", []), ("net_return", "NaN")])
def test_unusable_chart_data_is_rejected(
    report_path: Path, tmp_path: Path, field: str, value: object
) -> None:
    report = json.loads(report_path.read_text())
    report["baseline"][field] = value
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        export_dashboard(report_path, tmp_path / "dashboard.html")


def test_invalid_evidence_does_not_create_output(
    report_path: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence.md"
    evidence.write_text("No evidence", encoding="utf-8")
    output = tmp_path / "dashboard.html"
    with pytest.raises(ValueError):
        export_dashboard(report_path, output, evidence_path=evidence)
    assert not output.exists()
