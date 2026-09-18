"""AC-18: the architecture diagrams must exist and must not be empty.

Not a content check - a Mermaid diagram's fidelity to the real code is a
review concern, not something a unit test can verify - only the acceptance
criterion's literal, checkable half: the files exist under `docs/architecture/`
and are non-trivial.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ARCHITECTURE_DIR = _REPO_ROOT / "docs" / "architecture"


@pytest.mark.parametrize("name", ["execution-flow.md", "data-flow.md"])
def test_required_architecture_document_exists_and_is_substantial(name: str) -> None:
    path = _ARCHITECTURE_DIR / name
    assert path.is_file(), f"missing required architecture document: {path}"
    text = path.read_text(encoding="utf-8")
    assert len(text) > 200
    assert "```mermaid" in text
