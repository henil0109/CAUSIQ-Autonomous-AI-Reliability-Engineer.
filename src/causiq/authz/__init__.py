"""Agent identity and authorization - invariant I4.

Maturity: hardened.
"""

from __future__ import annotations

from causiq.authz.decision import authorize, require_authorized
from causiq.authz.models import (
    AgentIdentity,
    ApprovalToken,
    AuthorizationDecision,
    CapabilityRequest,
    DenialReason,
    Permission,
)

__all__ = [
    "AgentIdentity",
    "ApprovalToken",
    "AuthorizationDecision",
    "CapabilityRequest",
    "DenialReason",
    "Permission",
    "authorize",
    "require_authorized",
]
