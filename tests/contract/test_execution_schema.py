import json
import runpy
from pathlib import Path

import pytest

from ats.schema import execution_json_schemas

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "name",
    [
        "order-intent",
        "risk-decision",
        "read-only-access-permit",
        "account-observation",
        "shadow-proposal",
        "shadow-session-report",
        "read-once-request",
        "five-day-observation",
    ],
)
def test_execution_schema_matches_model(name: str) -> None:
    filename = f"{name}.v1.schema.json"
    committed: object = json.loads(
        (ROOT / "schemas" / "execution" / filename).read_text(encoding="utf-8")
    )
    schema = execution_json_schemas()[filename]
    assert committed == schema
    assert schema["$id"] == f"urn:ats:schema:{name}:v1"
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "name",
    [
        "order-intent",
        "risk-decision",
        "read-only-access-permit",
        "account-observation",
        "shadow-proposal",
        "shadow-session-report",
        "read-once-request",
        "five-day-observation",
    ],
)
@pytest.mark.parametrize("state", ["missing", "stale"])
def test_execution_generator_detects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, state: str
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts/generate_execution_schemas.py"))
    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "SCHEMA_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["generate_execution_schemas.py"])
    assert main() == 0
    monkeypatch.setattr("sys.argv", ["generate_execution_schemas.py", "--check"])
    assert main() == 0
    path = tmp_path / f"{name}.v1.schema.json"
    if state == "missing":
        path.unlink()
    else:
        path.write_text("{}", encoding="utf-8")
    assert main() == 1
