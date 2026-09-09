"""Authorization - invariant I4, "read is default, write is granted".

AC-7 (permission denial is recoverable) and AC-8 (a mutating action is denied
without an approval token, even when the permission is held) both live here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from causiq.authz import (
    AgentIdentity,
    ApprovalToken,
    CapabilityRequest,
    DenialReason,
    Permission,
    authorize,
    require_authorized,
)
from causiq.errors import ToolAuthorizationError
from causiq.ids import ApprovalId
from tests.conftest import T0

pytestmark = pytest.mark.unit

READ = CapabilityRequest(capability=Permission.WAREHOUSE_READ, resource="query_warehouse")
WRITE = CapabilityRequest(
    capability=Permission.WAREHOUSE_WRITE,
    resource="query_warehouse",
    mutating=True,
)


def test_read_is_allowed_for_a_read_identity(identity: AgentIdentity) -> None:
    decision = authorize(identity, READ, now=T0)
    assert decision.allowed
    assert decision.reason is None
    assert decision.identity_id == identity.agent_id


def test_missing_permission_is_denied(identity: AgentIdentity) -> None:
    """AC-7. The investigator holds no write permission."""
    decision = authorize(identity, WRITE, now=T0)
    assert not decision.allowed
    assert decision.reason is DenialReason.PERMISSION_NOT_GRANTED
    assert decision.detail is not None


def test_denial_is_recoverable_not_fatal(identity: AgentIdentity) -> None:
    """AC-7. A denial is data returned to the model, not a crash.

    The run continues so the agent can adapt - that adaptation is the whole point
    of an investigating agent.
    """
    with pytest.raises(ToolAuthorizationError) as caught:
        require_authorized(identity, WRITE, now=T0)
    assert caught.value.recoverable is True
    assert caught.value.context["reason"] == DenialReason.PERMISSION_NOT_GRANTED


def test_mutating_action_denied_without_approval(write_identity: AgentIdentity) -> None:
    """AC-8. Holding the permission is not sufficient to mutate.

    This is the Phase 5 human-in-the-loop gate, proven in Phase 0 before any
    mutating tool exists. `write_identity` genuinely holds `warehouse.write`.
    """
    assert write_identity.has(Permission.WAREHOUSE_WRITE)
    decision = authorize(write_identity, WRITE, now=T0)
    assert not decision.allowed
    assert decision.reason is DenialReason.APPROVAL_REQUIRED


def test_mutating_action_allowed_with_valid_approval(
    write_identity: AgentIdentity,
    approval: ApprovalToken,
) -> None:
    decision = authorize(write_identity, WRITE, now=T0, approval=approval)
    assert decision.allowed
    assert decision.reason is None


def test_expired_approval_is_denied(
    write_identity: AgentIdentity,
    approval: ApprovalToken,
) -> None:
    later = T0 + timedelta(hours=2)
    decision = authorize(write_identity, WRITE, now=later, approval=approval)
    assert not decision.allowed
    assert decision.reason is DenialReason.APPROVAL_EXPIRED


def test_approval_for_a_different_resource_is_denied(write_identity: AgentIdentity) -> None:
    """Scope is exact-match. A broad approval is one nobody read carefully."""
    elsewhere = ApprovalToken(
        approval_id=ApprovalId("apr_0002"),
        granted_by="henil",
        scope="some_other_tool",
        granted_at=T0,
        expires_at=T0 + timedelta(minutes=30),
    )
    decision = authorize(write_identity, WRITE, now=T0, approval=elsewhere)
    assert not decision.allowed
    assert decision.reason is DenialReason.APPROVAL_SCOPE_MISMATCH


def test_approval_does_not_substitute_for_a_missing_permission(
    identity: AgentIdentity,
    approval: ApprovalToken,
) -> None:
    """Gate order matters: permission first, approval second.

    A human approval must not be able to grant a capability the identity was
    never given - otherwise approval becomes a privilege-escalation path.
    """
    decision = authorize(identity, WRITE, now=T0, approval=approval)
    assert not decision.allowed
    assert decision.reason is DenialReason.PERMISSION_NOT_GRANTED


def test_approval_is_not_required_for_a_read(
    write_identity: AgentIdentity,
) -> None:
    decision = authorize(write_identity, READ, now=T0)
    assert decision.allowed


def test_identity_is_immutable(identity: AgentIdentity) -> None:
    """Nothing the model reads can add a permission mid-run."""
    with pytest.raises(ValueError, match=r"frozen|immutable"):
        identity.permissions = frozenset(Permission)


def test_require_authorized_returns_the_decision_when_allowed(identity: AgentIdentity) -> None:
    decision = require_authorized(identity, READ, now=T0)
    assert decision.allowed


def test_approval_token_helpers(approval: ApprovalToken) -> None:
    assert not approval.is_expired(T0)
    assert approval.is_expired(T0 + timedelta(hours=1))
    assert approval.covers("query_warehouse")
    assert not approval.covers("other_tool")


def test_approval_timestamps_must_be_timezone_aware() -> None:
    """A naive expiry makes "expired" ambiguous, which is unacceptable on a
    security gate."""
    from datetime import datetime

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="timezone-aware"):
        ApprovalToken(
            approval_id=ApprovalId("apr_0003"),
            granted_by="henil",
            scope="query_warehouse",
            granted_at=datetime(2026, 9, 7, 12, 0),  # noqa: DTZ001 - the point of the test
            expires_at=T0 + timedelta(minutes=30),
        )
