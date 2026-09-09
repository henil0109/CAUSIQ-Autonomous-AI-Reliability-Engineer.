"""The tool executor - the security boundary of the entire system.

Maturity: hardened.

Everything the model wants to do to the outside world passes through
`ToolExecutor.execute`. There is exactly one such path, which is the point: a
single function to read in review, and a single place where identity, permission,
approval, validation, evidence, and audit are all enforced together.

The order of the gates matters and is not arbitrary:

    resolve -> authorize -> validate -> execute -> record evidence -> audit

**Resolve before anything else**, because an unregistered name has no capability
to check - it is not an authorization failure, it is a nonexistent tool.

**Authorize before validating arguments.** An unauthorized caller learns nothing
about the tool's schema, and we spend no work on a call that was never going to
run. It also means the authorization decision can never depend on argument
content, which keeps it auditable as a function of identity alone.

**Evidence only on success.** A denied, malformed, failed, or timed-out call
retrieved no fact, so it records no evidence - it would be a fact about Causiq,
not about the incident. Those outcomes are recorded in the audit journal instead.
This is what keeps invariant I1 meaningful: everything in the ledger is something
a tool actually returned.

**Recoverable failures return; fatal ones raise.** A denial, a bad argument, a
tool crash, a timeout - each becomes a `ToolResult` with `is_error=True` that the
model reads and adapts to, because that adaptation is the entire point of an
investigating agent. Only `BudgetExceededError` propagates, because exhausting the
budget must stop the run rather than inform the model (invariant I5).

Known limitation, stated plainly: the timeout uses a worker thread and
`Future.result(timeout=...)`. Python cannot forcibly kill a thread, so a tool that
ignores its deadline is *abandoned*, not cancelled - the executor stops waiting
and returns a TIMEOUT result while the thread runs on. Real cancellation has to
come from the tool's own client (in P0.5, DuckDB's statement timeout). The
executor's timeout bounds how long a run waits, not how long a query runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from causiq.audit import AuditJournal
from causiq.authz import (
    AgentIdentity,
    ApprovalToken,
    AuthorizationDecision,
    CapabilityRequest,
    authorize,
)
from causiq.clock import Clock
from causiq.domain import AuditEventType, BudgetTracker, EvidenceRequest
from causiq.errors import (
    ToolInputValidationError,
    UnknownToolError,
)
from causiq.evidence import EvidenceLedger
from causiq.ids import EvidenceId
from causiq.obs import NoOpTracer, SpanName, Tracer
from causiq.tools.base import Tool, ToolInput, ToolOutput
from causiq.tools.registry import ToolRegistry


class ToolResultStatus(StrEnum):
    """Every way a tool call can end.

    A closed vocabulary because these are audited and counted: the Phase 4
    tool-use evaluator scores an agent partly on how often it lands somewhere
    other than OK, and a free-text status would make that ungradeable.
    """

    OK = "ok"
    UNKNOWN_TOOL = "unknown_tool"
    DENIED = "denied"
    INVALID_INPUT = "invalid_input"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ToolRequest(BaseModel):
    """A model's intent to call a tool. Not yet a call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Correlates with the model's `tool_use` block so results can be matched back.
    tool_use_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """The structured outcome of one tool call.

    `content` is what the model is shown. On success it is an envelope carrying
    the evidence id, because the model cannot cite evidence it was never told the
    id of - and an analysis whose citations do not resolve is rejected (I1). The
    envelope is the link between the capability layer and the citation validator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_use_id: str
    tool_name: str
    status: ToolResultStatus
    is_error: bool
    content: str
    #: Set only when status is OK. Nothing else produces evidence.
    evidence_id: EvidenceId | None = None
    duration_seconds: float | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ToolResultStatus.OK


def _success_envelope(evidence_id: EvidenceId, output: ToolOutput) -> str:
    """Render a successful result for the model.

    Deliberately plain text with the evidence id first: it is what the model must
    copy into its citations, it is cheap in tokens, and it reads unambiguously in
    an audit journal.
    """
    header = (
        f"evidence_id: {evidence_id}\n"
        f"content_type: {output.content_type}\n"
        f"truncated: {str(output.truncated).lower()}\n"
    )
    return f"{header}\n{output.content}"


class ToolExecutor:
    """Executes tool requests under identity, permission, approval and budget.

    One instance per investigation run: it is bound to that run's identity,
    ledger, and audit journal, so a request cannot be executed against the wrong
    run's ledger by accident.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        identity: AgentIdentity,
        ledger: EvidenceLedger,
        journal: AuditJournal,
        clock: Clock,
        tracer: Tracer | None = None,
        budget: BudgetTracker | None = None,
        approvals: Sequence[ApprovalToken] = (),
    ) -> None:
        if ledger.run_id != journal.run_id:
            msg = "ledger and audit journal belong to different runs"
            raise ValueError(msg)
        self._registry = registry
        self._identity = identity
        self._ledger = ledger
        self._journal = journal
        self._clock = clock
        self._tracer: Tracer = tracer if tracer is not None else NoOpTracer()
        self._budget = budget
        self._approvals = tuple(approvals)

    # -- the single entry point --------------------------------------------- #

    def execute(self, request: ToolRequest) -> ToolResult:
        """Run one tool request through every gate.

        Returns a structured result for every recoverable outcome. Raises only
        `BudgetExceededError`, which must end the run rather than be handed back
        to the model.
        """
        # Invariant I5. Charged before any work, so the ceiling bounds what is
        # spent rather than reporting what was overspent. Deliberately outside the
        # try/except below: this one propagates.
        if self._budget is not None:
            self._budget.begin_tool_call()

        # Gate 0 - is this even a tool?
        try:
            tool = self._registry.require(request.tool_name)
        except UnknownToolError as exc:
            return self._reject(
                request,
                status=ToolResultStatus.UNKNOWN_TOOL,
                event=AuditEventType.TOOL_UNKNOWN,
                content=(
                    f"Unknown tool {request.tool_name!r}. "
                    f"Available tools: {', '.join(self._registry.names()) or '(none)'}."
                ),
                detail={"available": list(self._registry.names())},
                exc=exc,
            )

        # Gate 1 and 2 - permission, then human approval for mutating actions.
        decision = self._authorize(tool)
        if not decision.allowed:
            return self._reject(
                request,
                status=ToolResultStatus.DENIED,
                event=AuditEventType.TOOL_DENIED,
                content=(
                    f"Authorization denied for {tool.spec.name!r}: "
                    f"{decision.reason}. This call was not executed."
                ),
                detail={
                    "reason": str(decision.reason),
                    "capability": str(tool.spec.permission),
                    "mutating": tool.spec.mutating,
                },
            )
        self._journal.record(
            AuditEventType.TOOL_AUTHORIZED,
            actor=self._identity.agent_id,
            tool=tool.spec.name,
            capability=str(tool.spec.permission),
            mutating=tool.spec.mutating,
            risk=str(tool.spec.risk),
        )

        # Gate 3 - arguments must satisfy the declared schema.
        try:
            payload = self._validate(tool, request)
        except ToolInputValidationError as exc:
            return self._reject(
                request,
                status=ToolResultStatus.INVALID_INPUT,
                event=AuditEventType.TOOL_INPUT_REJECTED,
                content=f"Invalid arguments for {tool.spec.name!r}: {exc.context.get('problems')}",
                detail={"problems": exc.context.get("problems")},
                exc=exc,
            )

        # Execution, then evidence, then audit.
        return self._run(tool, request, payload)

    # -- gates -------------------------------------------------------------- #

    def _authorize(self, tool: Tool) -> AuthorizationDecision:
        """Delegate to `causiq.authz`. Never re-implement the rules here.

        The executor's job is to *ask*, record, and obey. Duplicating the decision
        logic would create two rule sets that can drift apart, and the one that
        drifts is always the one nobody is testing.
        """
        capability_request = CapabilityRequest(
            capability=tool.spec.permission,
            resource=tool.spec.name,
            mutating=tool.spec.mutating,
        )
        with self._tracer.span(SpanName.TOOL_AUTHORIZE, tool=tool.spec.name):
            return authorize(
                self._identity,
                capability_request,
                now=self._clock.now(),
                approval=self._select_approval(tool.spec.name),
            )

    def _select_approval(self, resource: str) -> ApprovalToken | None:
        """Pick the approval token to present for `resource`.

        Prefers a token whose scope covers the resource. If none does but tokens
        exist, the first is presented anyway so `authorize` can report the honest
        reason - a scope mismatch - rather than the misleading "no approval at
        all". Every denial reason stays reachable through this path.
        """
        for token in self._approvals:
            if token.covers(resource):
                return token
        return self._approvals[0] if self._approvals else None

    def _validate(self, tool: Tool, request: ToolRequest) -> ToolInput:
        """Validate arguments against the tool's declared input model."""
        try:
            return tool.input_model.model_validate(request.arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
                for error in exc.errors()
            )
            msg = "tool arguments failed schema validation"
            raise ToolInputValidationError(msg, tool=tool.spec.name, problems=problems) from exc

    # -- execution ---------------------------------------------------------- #

    def _run(self, tool: Tool, request: ToolRequest, payload: ToolInput) -> ToolResult:
        """Execute under timeout, then record evidence and audit the outcome."""
        started = self._clock.monotonic()
        with self._tracer.span(SpanName.TOOL_EXECUTE, tool=tool.spec.name) as span:
            try:
                output = self._call_with_timeout(tool, payload)
            except FutureTimeoutError:
                return self._reject(
                    request,
                    status=ToolResultStatus.TIMEOUT,
                    event=AuditEventType.TOOL_FAILED,
                    content=(
                        f"{tool.spec.name!r} exceeded its {tool.spec.timeout_seconds}s timeout "
                        f"and was abandoned. No result was recorded."
                    ),
                    detail={
                        "timeout_seconds": tool.spec.timeout_seconds,
                        "outcome": "timeout",
                    },
                )
            except Exception as exc:
                return self._reject(
                    request,
                    status=ToolResultStatus.FAILED,
                    event=AuditEventType.TOOL_FAILED,
                    content=f"{tool.spec.name!r} failed: {type(exc).__name__}: {exc}",
                    detail={"error_type": type(exc).__name__, "outcome": "failed"},
                )
            span.set_attribute("truncated", output.truncated)

        duration = self._clock.monotonic() - started

        # Success is the only path that produces evidence (invariant I1).
        with self._tracer.span(SpanName.EVIDENCE_RECORD, tool=tool.spec.name):
            evidence = self._ledger.record(
                source=tool.spec.source,
                agent_id=self._identity.agent_id,
                request=EvidenceRequest(
                    tool_name=tool.spec.name,
                    arguments=payload.model_dump(mode="json", exclude={"reason"}),
                    reason=payload.reason,
                ),
                content=output.content,
                content_type=output.content_type,
                truncated=output.truncated,
                collected_at=self._clock.now(),
            )

        self._journal.record(
            AuditEventType.TOOL_EXECUTED,
            actor=self._identity.agent_id,
            tool=tool.spec.name,
            duration_seconds=duration,
            truncated=output.truncated,
            result_bytes=len(output.content),
        )
        self._journal.record(
            AuditEventType.EVIDENCE_RECORDED,
            actor=self._identity.agent_id,
            tool=tool.spec.name,
            evidence_id=str(evidence.evidence_id),
            digest=evidence.digest,
        )

        return ToolResult(
            tool_use_id=request.tool_use_id,
            tool_name=tool.spec.name,
            status=ToolResultStatus.OK,
            is_error=False,
            content=_success_envelope(evidence.evidence_id, output),
            evidence_id=evidence.evidence_id,
            duration_seconds=duration,
        )

    def _call_with_timeout(self, tool: Tool, payload: ToolInput) -> ToolOutput:
        """Run `tool.run` on a worker thread, bounded by the tool's timeout.

        `shutdown(wait=False)` is deliberate: on timeout we must not block waiting
        for the thread we just gave up on. See the module docstring for what this
        does and does not guarantee.
        """
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{tool.spec.name}")
        try:
            future = pool.submit(tool.run, payload)
            return future.result(timeout=tool.spec.timeout_seconds)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # -- rejection ---------------------------------------------------------- #

    def _reject(
        self,
        request: ToolRequest,
        *,
        status: ToolResultStatus,
        event: AuditEventType,
        content: str,
        detail: dict[str, Any] | None = None,
        exc: Exception | None = None,
    ) -> ToolResult:
        """Audit a non-success outcome and return it as an error result.

        Every rejection is audited, and none of them records evidence. Both halves
        matter: the audit journal must show what was refused and why (invariant
        I7), and the ledger must contain only facts a tool actually returned
        (invariant I1).
        """
        del exc  # captured in `detail` by the caller where it is meaningful
        self._journal.record(
            event,
            actor=self._identity.agent_id,
            tool=request.tool_name,
            tool_use_id=request.tool_use_id,
            status=str(status),
            **(detail or {}),
        )
        return ToolResult(
            tool_use_id=request.tool_use_id,
            tool_name=request.tool_name,
            status=status,
            is_error=True,
            content=content,
            evidence_id=None,
        )
