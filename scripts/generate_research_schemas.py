"""Generate or verify experiment, evaluation, and promotion decision schemas."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ats.schema import render_research_json_schemas

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas" / "research"


class Arguments(argparse.Namespace):
    check: bool = False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when a committed research schema is missing or stale.",
    )
    args = parser.parse_args(namespace=Arguments())
    failed = False
    for name, rendered in render_research_json_schemas().items():
        path = SCHEMA_DIR / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                print(f"missing or stale generated schema: {path}", file=sys.stderr)
                failed = True
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8", newline="\n")
            print(f"wrote {path}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
