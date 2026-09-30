import json
from pathlib import Path

import pytest

from ats.schema import (
    DATA_SNAPSHOT_SCHEMA_ID,
    MARKET_EVENT_SCHEMA_ID,
    data_snapshot_json_schema,
    market_event_json_schema,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "data" / "snapshot.v1.schema.json"


def test_committed_data_snapshot_schema_matches_model() -> None:
    committed: object = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert committed == data_snapshot_json_schema()


def test_data_snapshot_schema_has_stable_identity_and_closed_root() -> None:
    schema = data_snapshot_json_schema()

    assert schema["$id"] == DATA_SNAPSHOT_SCHEMA_ID
    assert schema["additionalProperties"] is False
    assert schema["title"] == "DataSnapshot"


def test_committed_market_event_schema_matches_model() -> None:
    path = SCHEMA_PATH.with_name("market-event.v1.schema.json")
    committed: object = json.loads(path.read_text(encoding="utf-8"))
    schema = market_event_json_schema()
    assert committed == schema
    assert schema["$id"] == MARKET_EVENT_SCHEMA_ID
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("state", ["missing", "stale"])
def test_data_generator_detects_event_schema_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    import runpy

    namespace = runpy.run_path(str(ROOT / "scripts/generate_data_snapshot_schema.py"))
    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(main.__globals__, "SCHEMA_PATH", tmp_path / "snapshot.json")
    monkeypatch.setattr("sys.argv", ["generate_data_snapshot_schema.py"])
    assert main() == 0
    event_path = tmp_path / "market-event.v1.schema.json"
    if state == "missing":
        event_path.unlink()
    else:
        event_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["generate_data_snapshot_schema.py", "--check"])
    assert main() == 1
