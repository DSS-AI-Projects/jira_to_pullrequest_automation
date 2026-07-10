"""Export the Plan contract to schema/plan.schema.json (committed).

Run: `uv run python -m app.schemas.export`. The frontend generates its types
from the exported file; scripts/check.py re-runs both and fails on any diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.schemas.plan import Plan

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "plan.schema.json"


def plan_json_schema() -> dict[str, Any]:
    schema = Plan.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Plan"
    return schema


def render() -> str:
    return json.dumps(plan_json_schema(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
