"""Agent identity, permissions, and approval tokens - invariant I4.

Maturity: hardened.

These live in their own package rather than under `tools/` (where Engineering
Contract 4.3 originally sketched them) because authorization is not a property of
tools. It is a property of identities and capabilities, and the tool executor in
P0.4 will be one of its *callers*, not its owner. Keeping the dependency pointing
that way means the authorization rules can be tested - as they are in P0.3 -
before any tool exists.

The security posture worth stating out loud: an `AgentIdentity` is constructed at
run start and is frozen. Nothing the model reads - a log line, a commit message,
a row of query output - can add a permission to it, because permissions are
checked in Python against an object fixed before the run began.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from causiq.domain.enums import AgentRole
from causiq.ids import ApprovalId


class Permission(StrEnum):
    """Capabilities an identity may hold.

    Namespaced `resource.action` so the read/write split is visible at a glance.
    Phase 0 grants only reads; `WAREHOUSE_WRITE` exists so the mutation gate can
    be proven to deny before any mutating tool is written (AC-8).
    """

    WAREHOUSE_READ = "warehouse.read"
    WAREHOUSE_WRITE = "warehouse.write"
    ARTIFACTS_READ = "artifacts.read"


class DenialReason(StrEnum):
    """Why an authorization decision came back negative.

    A closed vocabulary rather than a message string: denials are audited, and the
    Phase 4 evaluator needs to count them by kind.
    """

    PERMISSION_NOT_GRANTED = "permission_not_granted"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_SCOPE_MISMATCH = "approval_scope_mismatch"


class AgentIdentity(BaseModel):
    """Who is acting. Immutable for the life of a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(min_length=1)
    role: AgentRole
    permissions: frozenset[Permission] = frozenset()

    def has(self, permission: Permission) -> bool:
        """Whether this identity holds `permission`."""
        return permission in self.permissions


class ApprovalToken(BaseModel):
    """A human's grant for one mutating action, bounded in scope and time.

    Phase 0 builds and tests the token and the gate that requires it; the workflow
    that *issues* one is Phase 5. That split is deliberate - the gate is the
    security property, and it should exist before there is anything to gate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: ApprovalId
    granted_by: str = Field(min_length=1)
    #: The resource this approval covers, e.g. a tool name. Exact match only.
    scope: str = Field(min_length=1)
    granted_at: datetime
    expires_at: datetime

    @field_validator("granted_at", "expires_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "approval timestamps must be timezone-aware"
            raise ValueError(msg)
        return value

    def is_expired(self, now: datetime) -> bool:
        """Whether the token has passed its expiry at `now`."""
        return now >= self.expires_at

    def covers(self, resource: str) -> bool:
        """Whether this token's scope covers `resource`.

        Exact match, not a prefix or glob. A broad approval is an approval nobody
        read carefully.
        """
        return self.scope == resource


class CapabilityRequest(BaseModel):
    """A request to do something that requires authorization.

    Deliberately not tool-shaped: it names a capability, a resource, and whether
    the action mutates state. The P0.4 tool executor will build one of these from
    a tool definition, but so could any other future caller.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: Permission
    #: What is being acted on - the tool name, in the tool executor's case.
    resource: str = Field(min_length=1)
    #: Mutating requests need an approval token as well as the permission (I4).
    mutating: bool = False


class AuthorizationDecision(BaseModel):
    """The outcome of an authorization check. Always recorded, allowed or not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    request: CapabilityRequest
    identity_id: str = Field(min_length=1)
    reason: DenialReason | None = None
    detail: str | None = None
