"""Test tools.

Phase 0.4 builds the capability layer but no real investigation tool - the first
of those (`query_warehouse`) is P0.5. These doubles exist so the registry and the
executor can be exercised against every branch without a real system, and they
live under `tests/` rather than `src/` so nothing shippable can accidentally
depend on them.
"""

from __future__ import annotations

import threading

from pydantic import BaseModel, ConfigDict, Field

from causiq.authz import Permission
from causiq.domain import EvidenceSource, RiskLevel
from causiq.tools import ToolInput, ToolOutput, ToolSpec


class EchoInput(ToolInput):
    """Arguments for the read-only echo tool."""

    value: str = Field(min_length=1, description="Text to echo back.")
    times: int = Field(default=1, ge=1, le=5, description="How many times to repeat it.")


class EchoTool:
    """A read-only tool that succeeds. The happy path for every executor test."""

    def __init__(self, name: str = "echo_reader", *, timeout_seconds: float = 5.0) -> None:
        self._spec = ToolSpec(
            name=name,
            description="Echo the supplied value back. Read-only test double.",
            permission=Permission.WAREHOUSE_READ,
            mutating=False,
            risk=RiskLevel.LOW,
            source=EvidenceSource.WAREHOUSE,
            timeout_seconds=timeout_seconds,
        )
        self.calls: list[EchoInput] = []

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def input_model(self) -> type[ToolInput]:
        return EchoInput

    def run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, EchoInput)
        self.calls.append(payload)
        return ToolOutput(
            content=" ".join([payload.value] * payload.times),
            content_type="text/plain",
        )


class MutatingInput(ToolInput):
    """Arguments for the write tool."""

    target: str = Field(min_length=1, description="What to modify.")


class MutatingTool:
    """A tool that declares `mutating=True`.

    Exists so the approval gate can be proven to deny before any real mutating
    tool is written (AC-8). It never actually mutates anything.
    """

    def __init__(self, name: str = "mutating_writer") -> None:
        self._spec = ToolSpec(
            name=name,
            description="Pretend to modify state. Write-classified test double.",
            permission=Permission.WAREHOUSE_WRITE,
            mutating=True,
            risk=RiskLevel.HIGH,
            source=EvidenceSource.WAREHOUSE,
        )
        self.calls: list[MutatingInput] = []

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def input_model(self) -> type[ToolInput]:
        return MutatingInput

    def run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, MutatingInput)
        self.calls.append(payload)
        return ToolOutput(content=f'{{"modified": "{payload.target}"}}')


class ExplodingTool:
    """A read-only tool that always raises. Exercises the failure path."""

    def __init__(self, name: str = "exploding_reader") -> None:
        self._spec = ToolSpec(
            name=name,
            description="Always fails. Test double for the tool-failure path.",
            permission=Permission.WAREHOUSE_READ,
            source=EvidenceSource.WAREHOUSE,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def input_model(self) -> type[ToolInput]:
        return EchoInput

    def run(self, payload: ToolInput) -> ToolOutput:
        del payload
        msg = "upstream warehouse connection reset"
        raise RuntimeError(msg)


class HangingTool:
    """A read-only tool that blocks past its deadline. Exercises the timeout path.

    Waits on an `Event` nobody sets, with its own ceiling so the worker thread
    always exits rather than leaking for the life of the test session.
    """

    def __init__(self, name: str = "hanging_reader", *, timeout_seconds: float = 0.05) -> None:
        self._spec = ToolSpec(
            name=name,
            description="Blocks past its deadline. Test double for the timeout path.",
            permission=Permission.WAREHOUSE_READ,
            source=EvidenceSource.WAREHOUSE,
            timeout_seconds=timeout_seconds,
        )
        self.released = threading.Event()

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def input_model(self) -> type[ToolInput]:
        return EchoInput

    def run(self, payload: ToolInput) -> ToolOutput:
        del payload
        self.released.wait(timeout=5.0)
        return ToolOutput(content="never observed by the executor")


class NotAToolInput(ToolInput):
    """An input model that permits extra keys - therefore not strict-eligible."""

    model_config = ToolInput.model_config | {"extra": "allow"}

    value: str = "x"


class LooseTool:
    """A tool whose input model would emit a non-strict schema. Must be rejected."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="loose_reader",
            description="Input model allows extra keys.",
            permission=Permission.WAREHOUSE_READ,
            source=EvidenceSource.WAREHOUSE,
        )

    @property
    def input_model(self) -> type[ToolInput]:
        return NotAToolInput

    def run(self, payload: ToolInput) -> ToolOutput:
        del payload
        return ToolOutput(content="{}")


class ForeignInput(BaseModel):
    """A perfectly good pydantic model that is not a `ToolInput`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = "x"


class ForeignInputTool:
    """A tool whose argument model does not inherit `ToolInput`.

    Must be rejected at registration: without the base class there is no
    guaranteed `reason` field, so evidence would lose its provenance.
    """

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="foreign_reader",
            description="Argument model does not subclass ToolInput.",
            permission=Permission.WAREHOUSE_READ,
            source=EvidenceSource.WAREHOUSE,
        )

    @property
    def input_model(self) -> type[ToolInput]:
        return ForeignInput  # type: ignore[return-value]

    def run(self, payload: ToolInput) -> ToolOutput:
        del payload
        return ToolOutput(content="{}")
