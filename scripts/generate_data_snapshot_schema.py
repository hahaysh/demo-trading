"""Generate or verify snapshot, event, and universe membership schemas."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ats.schema import (
    render_data_snapshot_json_schema,
    render_market_event_json_schema,
    render_universe_membership_json_schema,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "data" / "snapshot.v1.schema.json"


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
    schemas = {
        SCHEMA_PATH: render_data_snapshot_json_schema(),
        SCHEMA_PATH.with_name(
            "market-event.v1.schema.json"
        ): render_market_event_json_schema(),
        SCHEMA_PATH.with_name(
            "universe-membership.v1.schema.json"
        ): render_universe_membership_json_schema(),
    }
    failed = False
    for path, rendered in schemas.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                print(f"missing or stale generated schema: {path}", file=sys.stderr)
                failed = True
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8", newline="\n")
            print(f"wrote {path.relative_to(ROOT)}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
