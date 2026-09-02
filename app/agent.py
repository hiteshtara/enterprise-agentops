import json
from typing import Any

from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.model_provider import ModelProvider
from app.tool_registry import (
    ApprovalRequired,
    ToolRegistry,
)

DEFAULT_MAX_ITERATIONS = 10

# Deterministic tools raise these to reject arguments they cannot honour. They
# describe a bad request, not a broken system, so the model is told what went
# wrong and gets another iteration to correct itself.
RECOVERABLE_TOOL_ERRORS = (ValueError, TypeError)

MAX_ITERATIONS_ANSWER = (
    "Stopped after reaching the maximum of {max_iterations} reasoning "
    "iterations without producing a final answer."
)

AGENT_FAILED_ANSWER = (
    "The agent stopped because a tool failed unexpectedly. The failure has "
    "been recorded in the audit log."
)


def serialise_tool_result(result: Any) -> str:
    """Render a tool result as JSON for the model.

    Any JSON-serialisable result -- including lists of rows -- is encoded as
    JSON so the model receives well-formed data rather than a Python repr.
    Anything else falls back to str().
    """
    try:
        return json.dumps(result)

    except (TypeError, ValueError):
        return str(result)


def tool_failure_output(error_type: str, message: str) -> str:
    """The function_call_output handed back to the model after a tool failure.

    Carries the error type and message only -- never a traceback.
    """
    return json.dumps(
        {
            "error": {
                "type": error_type,
                "message": message,
            }
        }
    )


class AgentService:
    """Runs the reasoning loop: one tool call per model iteration, bounded.

    Failure policy:
      - ApprovalRequired  -> not a failure; stops and asks a human (unchanged).
      - ValueError/TypeError from a tool -> recoverable. Audited as TOOL_FAILED
        and reported back to the model, which may retry with better arguments.
      - Any other exception from a tool -> non-recoverable. Audited as
        AGENT_FAILED and the run ends with a controlled response.
      - Exceeding max_iterations -> audited as AGENT_MAX_ITERATIONS and the run
        ends without executing a further tool.
    """

    def __init__(
        self,
        model: ModelProvider,
        tool_registry: ToolRegistry,
        approval_store: ApprovalStore,
        audit_store: AuditStore,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        if max_iterations < 1:
            raise ValueError(
                f"max_iterations must be at least 1, got {max_iterations}."
            )

        self.model = model
        self.tool_registry = tool_registry
        self.approval_store = approval_store
        self.audit_store = audit_store
        self.max_iterations = max_iterations

    def record_tool_failure(
        self,
        tool: str,
        arguments: dict[str, Any] | None,
        exc: Exception,
        message: str | None = None,
    ) -> str:
        """Audit a recoverable tool failure and build the model's feedback."""
        error_type = type(exc).__name__
        detail = message or str(exc)

        self.audit_store.record(
            "TOOL_FAILED",
            {
                "tool": tool,
                "arguments": arguments,
                "error_type": error_type,
                "error": detail,
            },
        )

        return tool_failure_output(error_type, detail)

    def append_tool_output(
        self,
        input_items: list[Any],
        function_call: Any,
        output: str,
    ) -> None:
        input_items.extend(
            [
                function_call,
                {
                    "type": ("function_call_output"),
                    "call_id": (function_call.call_id),
                    "output": output,
                },
            ]
        )

    def run(
        self,
        message: str,
    ) -> dict[str, Any]:
        input_items: list[Any] = [
            {
                "role": "user",
                "content": message,
            }
        ]

        trace: list[dict[str, Any]] = []

        for _ in range(self.max_iterations):
            response = self.model.generate_with_tools(
                input_items,
                self.tool_registry.schemas(),
            )

            function_call = None

            for item in response.output:
                if item.type == "function_call":
                    function_call = item
                    break

            if function_call is None:
                return {
                    "answer": response.output_text,
                    "trace": trace,
                    "approval_required": None,
                }

            try:
                arguments = json.loads(function_call.arguments)

            except json.JSONDecodeError as exc:
                # The model emitted malformed arguments. Tell it so, and let it
                # try again rather than failing the whole request.
                self.append_tool_output(
                    input_items,
                    function_call,
                    self.record_tool_failure(
                        function_call.name,
                        None,
                        exc,
                        message=f"Tool arguments were not valid JSON: {exc}",
                    ),
                )

                continue

            self.audit_store.record(
                "TOOL_REQUESTED",
                {
                    "tool": function_call.name,
                    "arguments": arguments,
                },
            )

            try:
                result = self.tool_registry.execute(
                    function_call.name,
                    arguments,
                )

            except ApprovalRequired as approval:
                pending = self.approval_store.create(
                    tool=approval.tool_name,
                    arguments=approval.arguments,
                    risk=approval.risk.value,
                )

                self.audit_store.record(
                    "APPROVAL_REQUIRED",
                    {
                        "approval_id": (pending.approval_id),
                        "tool": pending.tool,
                        "arguments": (pending.arguments),
                        "risk": pending.risk,
                    },
                )

                return {
                    "answer": (
                        f"Approval required before executing {approval.tool_name}."
                    ),
                    "trace": trace,
                    "approval_required": {
                        "approval_id": (pending.approval_id),
                        "tool": pending.tool,
                        "arguments": (pending.arguments),
                        "risk": pending.risk,
                    },
                }

            except RECOVERABLE_TOOL_ERRORS as exc:
                self.append_tool_output(
                    input_items,
                    function_call,
                    self.record_tool_failure(
                        function_call.name,
                        arguments,
                        exc,
                    ),
                )

                continue

            except Exception as exc:  # noqa: BLE001 -- deliberate safety net
                # An unexpected exception means the tool is broken, not that the
                # model asked for the wrong thing. Retrying cannot help, so the
                # run ends here. The message is audited but never returned.
                self.audit_store.record(
                    "AGENT_FAILED",
                    {
                        "tool": function_call.name,
                        "arguments": arguments,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )

                return {
                    "answer": AGENT_FAILED_ANSWER,
                    "trace": trace,
                    "approval_required": None,
                }

            self.audit_store.record(
                "TOOL_EXECUTED",
                {
                    "tool": function_call.name,
                    "arguments": arguments,
                    "result": result,
                },
            )

            trace.append(
                {
                    "tool": function_call.name,
                    "arguments": arguments,
                    "result": result,
                }
            )

            self.append_tool_output(
                input_items,
                function_call,
                serialise_tool_result(result),
            )

        self.audit_store.record(
            "AGENT_MAX_ITERATIONS",
            {
                "max_iterations": self.max_iterations,
                "tool_calls": len(trace),
            },
        )

        return {
            "answer": MAX_ITERATIONS_ANSWER.format(
                max_iterations=self.max_iterations,
            ),
            "trace": trace,
            "approval_required": None,
        }

    def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
    ) -> dict[str, Any]:
        pending = self.approval_store.get(approval_id)

        if pending is None:
            raise ValueError(f"Unknown approval ID: {approval_id}")

        if not approved:
            self.audit_store.record(
                "APPROVAL_DENIED",
                {
                    "approval_id": approval_id,
                    "tool": pending.tool,
                    "arguments": (pending.arguments),
                },
            )

            self.approval_store.remove(approval_id)

            return {
                "approval_id": approval_id,
                "approved": False,
                "tool": pending.tool,
                "result": None,
            }

        result = self.tool_registry.execute(
            pending.tool,
            pending.arguments,
            approved=True,
        )

        self.audit_store.record(
            "APPROVAL_GRANTED",
            {
                "approval_id": approval_id,
                "tool": pending.tool,
                "arguments": pending.arguments,
                "result": result,
            },
        )

        self.audit_store.record(
            "TOOL_EXECUTED",
            {
                "tool": pending.tool,
                "arguments": pending.arguments,
                "result": result,
                "approved": True,
            },
        )

        self.approval_store.remove(approval_id)

        return {
            "approval_id": approval_id,
            "approved": True,
            "tool": pending.tool,
            "result": result,
        }
