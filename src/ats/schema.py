"""Deterministic JSON Schema rendering for public ATS contracts."""

from __future__ import annotations

import json

from ats.domain.data import DataSnapshot, MarketEvent
from ats.domain.execution import OrderIntent, RiskDecision
from ats.domain.governance import PromotionDecision
from ats.domain.prices import DailyPrice
from ats.domain.research import EvaluationResult, ExperimentRun
from ats.domain.strategy import StrategySpec
from ats.domain.universe import UniverseMembershipArtifact

DATA_SNAPSHOT_SCHEMA_ID = "urn:ats:schema:data-snapshot:v1"
MARKET_EVENT_SCHEMA_ID = "urn:ats:schema:market-event:v1"
STRATEGY_SCHEMA_ID = "urn:ats:schema:strategy:v1"
UNIVERSE_MEMBERSHIP_SCHEMA_ID = "urn:ats:schema:universe-membership:v1"


def daily_price_json_schema() -> dict[str, object]:
    schema: dict[str, object] = DailyPrice.model_json_schema(mode="validation")
    schema["$id"] = "urn:ats:schema:daily-price:v1"
    return schema


def render_daily_price_json_schema() -> str:
    return (
        json.dumps(
            daily_price_json_schema(), ensure_ascii=True, indent=2, sort_keys=True
        )
        + "\n"
    )


def universe_membership_json_schema() -> dict[str, object]:
    schema: dict[str, object] = UniverseMembershipArtifact.model_json_schema(
        mode="validation"
    )
    schema["$id"] = UNIVERSE_MEMBERSHIP_SCHEMA_ID
    return schema


def render_universe_membership_json_schema() -> str:
    return (
        json.dumps(
            universe_membership_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def execution_json_schemas() -> dict[str, dict[str, object]]:
    contracts = {"order-intent": OrderIntent, "risk-decision": RiskDecision}
    schemas: dict[str, dict[str, object]] = {}
    for name, model in contracts.items():
        schema: dict[str, object] = model.model_json_schema(mode="validation")
        schema["$id"] = f"urn:ats:schema:{name}:v1"
        schemas[f"{name}.v1.schema.json"] = schema
    return schemas


def render_execution_json_schemas() -> dict[str, str]:
    return {
        name: json.dumps(schema, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        for name, schema in execution_json_schemas().items()
    }


def research_json_schemas() -> dict[str, dict[str, object]]:
    contracts = {
        "experiment-run": ExperimentRun,
        "evaluation-result": EvaluationResult,
        "promotion-decision": PromotionDecision,
    }
    schemas: dict[str, dict[str, object]] = {}
    for name, model in contracts.items():
        schema: dict[str, object] = model.model_json_schema(mode="validation")
        schema["$id"] = f"urn:ats:schema:{name}:v1"
        schemas[f"{name}.v1.schema.json"] = schema
    return schemas


def render_research_json_schemas() -> dict[str, str]:
    return {
        name: json.dumps(schema, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        for name, schema in research_json_schemas().items()
    }


def market_event_json_schema() -> dict[str, object]:
    schema: dict[str, object] = MarketEvent.model_json_schema(mode="validation")
    schema["$id"] = MARKET_EVENT_SCHEMA_ID
    return schema


def render_market_event_json_schema() -> str:
    return (
        json.dumps(
            market_event_json_schema(), ensure_ascii=True, indent=2, sort_keys=True
        )
        + "\n"
    )


def data_snapshot_json_schema() -> dict[str, object]:
    schema: dict[str, object] = DataSnapshot.model_json_schema(mode="validation")
    schema["$id"] = DATA_SNAPSHOT_SCHEMA_ID
    return schema


def render_data_snapshot_json_schema() -> str:
    return (
        json.dumps(
            data_snapshot_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def strategy_json_schema() -> dict[str, object]:
    schema: dict[str, object] = StrategySpec.model_json_schema(mode="validation")
    schema["$id"] = STRATEGY_SCHEMA_ID
    return schema


def render_strategy_json_schema() -> str:
    return (
        json.dumps(
            strategy_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
