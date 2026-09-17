"""The bounded investigating agent (P0.6).

Maturity: hardened.

This is where every earlier phase's boundary finally gets exercised together:

    Incident -> Investigator -> ModelClient -> tool_use -> Registry ->
    Executor -> AuthZ -> query_warehouse -> Evidence Ledger -> tool result
    back to the model -> ... -> structured Analysis -> citation validation ->
    InvestigationRun

The agent itself is deliberately thin. It does not talk to DuckDB, does not
touch the evidence ledger directly, does not decide authorization, and does
not validate SQL - every one of those already has an owner from an earlier
phase, and this module is not permitted to duplicate or bypass any of them
(invariant I3; Engineering Contract 13). Concretely, the only things this
class does that no earlier module already did are: hold the conversation
together, enforce the budget between turns, and translate the model's two
possible decisions (call a tool, or conclude) into calls on the executor and
the citation validator.

Every turn is bounded by one `causiq.domain.budget.Budget` - the same object
that already limits tool calls inside `ToolExecutor` - so "maximum model
turns" and "maximum tool calls" are two views of one shared ceiling, not two
separate mechanisms invented for this phase.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import JsonValue

from causiq.audit import AuditJournal, AuditSink
from causiq.authz import AgentIdentity, ApprovalToken
from causiq.clock import Clock
from causiq.domain import (
    Analysis,
    AnalysisOutcome,
    AuditEventType,
    Budget,
    BudgetTracker,
    Incident,
    InvestigationRun,
    RunState,
)
from causiq.errors import (
    BudgetExceededError,
    ModelContractError,
    ModelError,
    ModelRefusalError,
)
from causiq.evidence import EvidenceLedger, validate_citations
from causiq.ids import IdGenerator, RunId
from causiq.llm import load_default_system_prompt
from causiq.llm.ports import (
    ConversationMessage,
    MessageRole,
    ModelClient,
    ModelTurn,
    TextBlock,
    ToolResultBlock,
)
from causiq.obs import NoOpTracer, SpanName, Tracer
from causiq.tools import ToolExecutor, ToolRegistry, ToolRequest


class _Terminated(Exception):  # noqa: N818 - a control-flow signal, deliberately not an *Error
    """Internal control-flow signal: the loop must stop with this outcome.

    Never escapes `Investigator.investigate` - caught at the top level and
    turned into a terminal `InvestigationRun`. Deliberately not part of the
    `causiq.errors` taxonomy: it is not an error condition the rest of the
    system needs to classify or recover from, only a way to unwind the loop
    from wherever a stop condition was discovered, without threading a
    return-or-raise decision through every branch by hand.
    """

    def __init__(self, state: RunState, reason: str, analysis: Analysis | None = None) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.analysis = analysis


def _render_incident_brief(incident: Incident) -> str:
    """The only description of the incident the model ever receives.

    Every field of `Incident` and nothing else - no internal application
    state, no other incidents, no ledger contents (Engineering Contract,
    P0.6 context engineering).
    """
    lines = [
        f"Incident: {incident.incident_id}",
        f"Title: {incident.title}",
        f"Severity: {incident.severity}",
        f"Detected at: {incident.detected_at.isoformat()}",
        f"Detected by: {incident.detected_by}",
    ]
    if incident.affected_assets:
        lines.append(f"Affected assets: {', '.join(incident.affected_assets)}")
    lines.append("")
    lines.append(incident.description)
    return "\n".join(lines)


class Investigator:
    """Runs one bounded investigation of one incident at a time.

    Stateless between calls: `investigate()` opens a fresh evidence ledger,
    audit journal, and budget tracker for every incident, so investigations
    never share state with each other. The identity, model, tool registry,
    and system prompt are fixed for the life of the instance - they describe
    *who is investigating and how*, not any one investigation.
    """

    def __init__(
        self,
        *,
        model: ModelClient,
        registry: ToolRegistry,
        identity: AgentIdentity,
        id_generator: IdGenerator,
        clock: Clock,
        tracer: Tracer | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._model = model
        self._registry = registry
        self._identity = identity
        self._id_generator = id_generator
        self._clock = clock
        self._tracer: Tracer = tracer if tracer is not None else NoOpTracer()
        self._system_prompt = (
            system_prompt if system_prompt is not None else load_default_system_prompt()
        )

    def investigate(
        self,
        incident: Incident,
        *,
        budget: Budget,
        audit_sink: AuditSink,
        approvals: Sequence[ApprovalToken] = (),
    ) -> InvestigationRun:
        """Investigate `incident` under `budget`, returning a terminal run.

        Always returns - never raises for a bounded, expected outcome (budget
        exhaustion, a refused model, an unvalidatable analysis). The returned
        run's `state` is always one of the terminal `RunState` values
        (invariant I8): a crash inside this method would be a bug, not a
        modeled failure mode.
        """
        run_id = self._id_generator.new_run_id()
        ledger = EvidenceLedger(run_id)
        journal = AuditJournal(run_id, self._clock, audit_sink)
        tracker = BudgetTracker(budget, self._clock)
        executor = ToolExecutor(
            registry=self._registry,
            identity=self._identity,
            ledger=ledger,
            journal=journal,
            clock=self._clock,
            tracer=self._tracer,
            budget=tracker,
            approvals=approvals,
        )

        started_at = self._clock.now()
        journal.record(
            AuditEventType.RUN_STARTED,
            actor=self._identity.agent_id,
            incident_id=str(incident.incident_id),
            max_turns=budget.max_turns,
            max_tool_calls=budget.max_tool_calls,
        )

        history: list[ConversationMessage] = [
            ConversationMessage(
                role=MessageRole.USER,
                content=(TextBlock(text=_render_incident_brief(incident)),),
            )
        ]

        state: RunState
        failure_reason: str | None
        analysis: Analysis | None
        with self._tracer.span(SpanName.RUN, run_id=str(run_id)):
            try:
                analysis = self._run_loop(tracker, journal, executor, ledger, history)
            except BudgetExceededError as exc:
                state, failure_reason, analysis = RunState.BUDGET_EXCEEDED, str(exc), None
            except _Terminated as exc:
                state, failure_reason, analysis = exc.state, exc.reason, exc.analysis
            else:
                state = (
                    RunState.COMPLETED
                    if analysis.outcome is AnalysisOutcome.COMPLETED
                    else RunState.INCONCLUSIVE
                )
                failure_reason = None

        return self._finish(
            run_id=run_id,
            incident=incident,
            budget=budget,
            tracker=tracker,
            ledger=ledger,
            journal=journal,
            started_at=started_at,
            state=state,
            failure_reason=failure_reason,
            analysis=analysis,
        )

    # -- the loop ------------------------------------------------------- #

    def _run_loop(
        self,
        tracker: BudgetTracker,
        journal: AuditJournal,
        executor: ToolExecutor,
        ledger: EvidenceLedger,
        history: list[ConversationMessage],
    ) -> Analysis:
        """The bounded loop itself. Returns only a citation-valid `Analysis`;
        every other outcome is a raise (`BudgetExceededError` or
        `_Terminated`), caught by the caller."""
        actor = self._identity.agent_id

        while True:
            tracker.begin_turn()  # raises BudgetExceededError - propagates, ending the run

            journal.record(
                AuditEventType.MODEL_CALL_REQUESTED, actor=actor, turn=tracker.usage().turns
            )
            with self._tracer.span(SpanName.AGENT_TURN, turn=tracker.usage().turns):
                turn = self._call_model(journal, tuple(history))

            tracker.record_tokens(input_tokens=turn.input_tokens, output_tokens=turn.output_tokens)
            journal.record(
                AuditEventType.MODEL_CALL_COMPLETED,
                actor=actor,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                decision="analysis" if turn.is_final else "tool_calls",
            )

            if turn.is_final:
                return self._conclude(journal, ledger, turn)

            history.append(ConversationMessage(role=MessageRole.ASSISTANT, content=turn.tool_calls))
            result_blocks = tuple(
                self._execute_tool_call(executor, call.call_id, call.tool_name, call.arguments)
                for call in turn.tool_calls
            )
            history.append(ConversationMessage(role=MessageRole.USER, content=result_blocks))

    def _call_model(
        self, journal: AuditJournal, history: tuple[ConversationMessage, ...]
    ) -> ModelTurn:
        """Call the model, mapping every `ModelError` onto a `_Terminated`.

        P0.6 does not retry a transient failure - a bounded run ends rather
        than looping on the model. Retry policy for `ModelTransientError` is
        deferred to a later phase.
        """
        actor = self._identity.agent_id
        try:
            with self._tracer.span(SpanName.MODEL_CALL):
                return self._model.investigate(
                    system=self._system_prompt,
                    tools=self._registry.schemas(),
                    history=history,
                )
        except ModelRefusalError as exc:
            journal.record(
                AuditEventType.MODEL_CALL_FAILED, actor=actor, reason="refusal", detail=str(exc)
            )
            raise _Terminated(RunState.FAILED, f"model refused: {exc}") from exc
        except ModelContractError as exc:
            journal.record(
                AuditEventType.MODEL_CALL_FAILED,
                actor=actor,
                reason="malformed_response",
                detail=str(exc),
            )
            raise _Terminated(RunState.FAILED, f"malformed model response: {exc}") from exc
        except ModelError as exc:
            journal.record(
                AuditEventType.MODEL_CALL_FAILED, actor=actor, reason="model_error", detail=str(exc)
            )
            raise _Terminated(RunState.FAILED, f"model call failed: {exc}") from exc

    def _conclude(self, journal: AuditJournal, ledger: EvidenceLedger, turn: ModelTurn) -> Analysis:
        """Validate a final turn's citations before accepting it (invariant I1).

        The single enforcement point in this whole module for "no claim
        without evidence" - it delegates entirely to the existing validator
        rather than re-checking citations itself.
        """
        actor = self._identity.agent_id
        analysis = turn.analysis
        assert analysis is not None  # guaranteed by ModelTurn.is_final

        result = validate_citations(analysis, ledger)
        if not result.valid:
            unresolved: list[JsonValue] = [str(item) for item in sorted(result.unresolved)]
            journal.record(AuditEventType.ANALYSIS_REJECTED, actor=actor, unresolved=unresolved)
            msg = (
                f"analysis rejected: citations do not resolve in the evidence ledger ({unresolved})"
            )
            raise _Terminated(RunState.FAILED, msg, analysis)

        cited: list[JsonValue] = [str(item) for item in sorted(result.cited)]
        journal.record(
            AuditEventType.ANALYSIS_VALIDATED,
            actor=actor,
            outcome=str(analysis.outcome),
            cited=cited,
        )
        return analysis

    def _execute_tool_call(
        self,
        executor: ToolExecutor,
        call_id: str,
        tool_name: str,
        arguments: dict[str, JsonValue],
    ) -> ToolResultBlock:
        """Route one model-requested tool call through the executor.

        The only thing this method does with `arguments` is forward it,
        untouched, to `ToolExecutor.execute` - the executor's own input
        validation is what decides whether the (untrusted) model-supplied
        arguments are acceptable (invariant I3, Engineering Contract 13).
        """
        request = ToolRequest(tool_use_id=call_id, tool_name=tool_name, arguments=arguments)
        result = executor.execute(request)  # BudgetExceededError propagates; nothing else raises
        return ToolResultBlock(call_id=call_id, content=result.content, is_error=result.is_error)

    # -- termination ------------------------------------------------------ #

    def _finish(
        self,
        *,
        run_id: RunId,
        incident: Incident,
        budget: Budget,
        tracker: BudgetTracker,
        ledger: EvidenceLedger,
        journal: AuditJournal,
        started_at: datetime,
        state: RunState,
        failure_reason: str | None,
        analysis: Analysis | None,
    ) -> InvestigationRun:
        ended_at = self._clock.now()
        journal.record(
            AuditEventType.RUN_ENDED,
            actor=self._identity.agent_id,
            state=str(state),
            failure_reason=failure_reason,
        )
        journal.close()
        return InvestigationRun(
            run_id=run_id,
            incident_id=incident.incident_id,
            agent_id=self._identity.agent_id,
            state=state,
            budget=budget,
            usage=tracker.usage(),
            started_at=started_at,
            ended_at=ended_at,
            evidence=ledger.snapshot(),
            analysis=analysis,
            failure_reason=failure_reason,
        )
