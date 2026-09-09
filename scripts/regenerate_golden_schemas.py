"""Regenerate the committed golden JSON Schemas.

Run this only when a schema change is intended, and say so in the commit message.
The golden files exist so that an *unintended* schema change fails the build
(ADR-0005); regenerating them without reading the diff defeats the purpose.

    uv run python scripts/regenerate_golden_schemas.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.contract.test_schema_stability import (  # noqa: E402
    GOLDEN_DIR,
    PUBLISHED_MODELS,
)


def main() -> int:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name, model in sorted(PUBLISHED_MODELS.items()):
        path = GOLDEN_DIR / f"{name}.schema.json"
        schema = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text(schema, encoding="utf-8")
        status = "unchanged" if existing == schema else "WRITTEN"
        sys.stdout.write(f"{status:>9}  {path.relative_to(REPO_ROOT).as_posix()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
