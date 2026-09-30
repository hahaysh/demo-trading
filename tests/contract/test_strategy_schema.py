import json
from pathlib import Path

from ats.schema import STRATEGY_SCHEMA_ID, strategy_json_schema

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "strategy" / "v1.schema.json"


def test_committed_strategy_schema_matches_model() -> None:
    committed: object = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert committed == strategy_json_schema()


def test_strategy_schema_has_stable_identity_and_closed_root() -> None:
    schema = strategy_json_schema()

    assert schema["$id"] == STRATEGY_SCHEMA_ID
    assert schema["additionalProperties"] is False
    assert schema["title"] == "StrategySpec"
