"""Schema stability.

The JSON Schema of these models is a published interface: it is what constrains
Claude's structured output (ADR-0005), what the audit journal stores, and what the
Phase 4 evaluator reads. A change to it is a change to behaviour.

These tests do not prevent schema changes - they make them deliberate. When one
fails, look at the diff, decide whether the change is intended, and regenerate:

    uv run python scripts/regenerate_golden_schemas.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from causiq.domain import Analysis, AuditEntry, Evidence, Incident, InvestigationRun

pytestmark = pytest.mark.contract

GOLDEN_DIR = Path(__file__).parent / "golden"

#: Models whose schema is an external interface.
PUBLISHED_MODELS: dict[str, type[BaseModel]] = {
    "analysis": Analysis,
    "audit_entry": AuditEntry,
    "evidence": Evidence,
    "incident": Incident,
    "investigation_run": InvestigationRun,
}


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


@pytest.mark.parametrize("name", sorted(PUBLISHED_MODELS))
def test_schema_matches_the_committed_golden(name: str) -> None:
    golden_path = GOLDEN_DIR / f"{name}.schema.json"
    assert golden_path.exists(), (
        f"missing golden schema for {name}; run scripts/regenerate_golden_schemas.py"
    )
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    assert _schema(PUBLISHED_MODELS[name]) == expected, (
        f"{name} schema changed. If intended, regenerate the golden file and say so "
        f"in the commit message - this is a behaviour change, not a refactor."
    )


def test_no_orphaned_golden_files() -> None:
    """A golden file for a model that no longer exists is stale documentation."""
    on_disk = {path.name.removesuffix(".schema.json") for path in GOLDEN_DIR.glob("*.schema.json")}
    assert on_disk == set(PUBLISHED_MODELS)


def test_claim_citations_are_required_by_schema() -> None:
    """The structural half of invariant I1, asserted at the schema level.

    This is what the model is actually constrained by, so it is worth checking
    directly rather than only through Python validation.
    """
    schema = _schema(Analysis)
    claim = schema["$defs"]["Claim"]
    assert set(claim["required"]) == {"statement", "citations"}
    assert claim["properties"]["citations"]["minItems"] == 1


def test_published_models_forbid_unknown_fields() -> None:
    """`extra="forbid"` means a renamed field fails loudly instead of vanishing."""
    for name, model in PUBLISHED_MODELS.items():
        assert _schema(model).get("additionalProperties") is False, name


def test_analysis_outcome_vocabulary_is_closed() -> None:
    schema = _schema(Analysis)
    assert set(schema["$defs"]["AnalysisOutcome"]["enum"]) == {"completed", "inconclusive"}
