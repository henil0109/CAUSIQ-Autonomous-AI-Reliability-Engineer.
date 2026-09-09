"""The capability layer: tool contract, registry, and the executor.

Maturity: hardened.

This package is the only route from a model's intent to a real system call
(invariant I3). Nothing here decides authorization policy - that lives in
`causiq.authz` and is merely enforced here, so the rules have one home.
"""

from __future__ import annotations

from causiq.tools.base import (
    Tool,
    ToolInput,
    ToolOutput,
    ToolSpec,
    canonical_json,
    schema_digest,
    tool_schema,
)
from causiq.tools.executor import (
    ToolExecutor,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)
from causiq.tools.registry import ToolRegistry

__all__ = [
    "Tool",
    "ToolExecutor",
    "ToolInput",
    "ToolOutput",
    "ToolRegistry",
    "ToolRequest",
    "ToolResult",
    "ToolResultStatus",
    "ToolSpec",
    "canonical_json",
    "schema_digest",
    "tool_schema",
]
