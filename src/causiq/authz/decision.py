"""The authorization decision function - the single gate for invariant I4.

Maturity: hardened.

Two independent gates, in order:

1. **Permission.** Does the acting identity hold the required capability?
2. **Approval.** If the action mutates state, is there an unexpired approval token
   whose scope matches this exact resource?

Holding `warehouse.write` is not sufficient to write. That is the whole point of
"read is default, write is granted": the permission says an identity *may* be
allowed to mutate; the approval says a human agreed to *this* mutation, now.

`authorize()` returns a decision rather than raising, because a denial is data.
The agent loop turns it into an `is_error` tool result the model can adapt to,
and the audit journal records it either way. `require_authorized()` is for
callers that genuinely cannot proceed.
"""

from __future__ import annotations

from datetime import datetime

from causiq.authz.models import (
    AgentIdentity,
    ApprovalToken,
    AuthorizationDecision,
    CapabilityRequest,
    DenialReason,
)
from causiq.errors import ToolAuthorizationError


def authorize(
    identity: AgentIdentity,
    request: CapabilityRequest,
    *,
    now: datetime,
    approval: ApprovalToken | None = None,
) -> AuthorizationDecision:
    """Decide whether `identity` may perform `request` at `now`.

    Never raises for a denial - see the module docstring.
    """

    def deny(reason: DenialReason, detail: str) -> AuthorizationDecision:
        return AuthorizationDecision(
            allowed=False,
            request=request,
            identity_id=identity.agent_id,
            reason=reason,
            detail=detail,
        )

    # Gate 1 - the identity must hold the capability.
    if not identity.has(request.capability):
        return deny(
            DenialReason.PERMISSION_NOT_GRANTED,
            f"identity does not hold {request.capability}",
        )

    # Gate 2 - mutating actions additionally require a human approval token.
    if request.mutating:
        if approval is None:
            return deny(
                DenialReason.APPROVAL_REQUIRED,
                f"{request.resource} mutates state and requires an approval token",
            )
        if approval.is_expired(now):
            return deny(
                DenialReason.APPROVAL_EXPIRED,
                f"approval {approval.approval_id} expired at {approval.expires_at.isoformat()}",
            )
        if not approval.covers(request.resource):
            return deny(
                DenialReason.APPROVAL_SCOPE_MISMATCH,
                f"approval {approval.approval_id} covers {approval.scope!r}, "
                f"not {request.resource!r}",
            )

    return AuthorizationDecision(
        allowed=True,
        request=request,
        identity_id=identity.agent_id,
    )


def require_authorized(
    identity: AgentIdentity,
    request: CapabilityRequest,
    *,
    now: datetime,
    approval: ApprovalToken | None = None,
) -> AuthorizationDecision:
    """As `authorize()`, but raise `ToolAuthorizationError` on denial.

    For callers with no sensible way to continue. The error is recoverable, so a
    caller that *does* have a way to continue can still catch it.
    """
    decision = authorize(identity, request, now=now, approval=approval)
    if not decision.allowed:
        msg = f"authorization denied for {request.resource}"
        raise ToolAuthorizationError(
            msg,
            identity=identity.agent_id,
            capability=str(request.capability),
            resource=request.resource,
            reason=str(decision.reason),
        )
    return decision
