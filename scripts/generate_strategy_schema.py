"""Generate or verify the versioned StrategySpec JSON Schema."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ats.schema import render_strategy_json_schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "strategy" / "v1.schema.json"


class Arguments(argparse.Namespace):
    check: bool = False


def parse_args() -> Arguments:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the committed schema differs from the model.",
    )
    return parser.parse_args(namespace=Arguments())


def main() -> int:
    args = parse_args()
    rendered = render_strategy_json_schema()

    if args.check:
        if not SCHEMA_PATH.exists():
            print(f"missing generated schema: {SCHEMA_PATH}", file=sys.stderr)
            return 1
        if SCHEMA_PATH.read_text(encoding="utf-8") != rendered:
            print(
                "generated strategy schema is stale; rerun this script",
                file=sys.stderr,
            )
            return 1
        return 0

    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {SCHEMA_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
