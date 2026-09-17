"""The bounded investigating agent (P0.6).

Maturity: hardened.

One agent, one job: turn a `causiq.domain.Incident` into a citation-valid
`InvestigationRun` by driving a bounded conversation with a `ModelClient`
through the existing capability layer (`causiq.tools`). See
`causiq.agents.investigator` for the full loop and its invariants.
"""

from __future__ import annotations

from causiq.agents.investigator import Investigator

__all__ = ["Investigator"]
