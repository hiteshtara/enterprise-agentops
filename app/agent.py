import json
from typing import Any

from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.model_provider import ModelProvider
from app.protocol import (
    ModelMessage,
    ToolCall,
    serialise_conversation,
)
from app.run_store import RunStatus, RunStore, StepType
from app.tool_registry import (
    ApprovalRequired,
    ToolRegistry,
)

DEFAULT_MAX_ITERATIONS = 10

# Deterministic tools raise these to reject arguments they cannot honour. They
# describe a bad request, not a broken system, so the model is told what went
# wrong and gets another iteration to correct itself.
RECOVERABLE_TOOL_ERRORS = (ValueError, TypeError)

INVALID_ARGUMENTS_ERROR = "InvalidToolArguments"

MAX_ITERATIONS_ANSWER = (
    "Stopped after reaching the maximum of {max_iterations} reasoning "
    "iterations without producing a final answer."
)

AGENT_FAILED_ANSWER = (
    "The agent stopped because a tool failed unexpectedly. The failure has "
    "been recorded in the audit log."
)

APPROVAL_DENIED_ANSWER = (
    "The requested action was not approved, so nothing was executed."
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
    """The tool result handed back to the model after a tool failure.

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

    The runtime speaks only the provider-neutral types in app.protocol, so it
    never sees a vendor SDK object and everything it persists is JSON.

    Failure policy:
      - ApprovalRequired  -> not a failure; the run parks in
        WAITING_FOR_APPROVAL until a human decides, then resumes.
      - ValueError/TypeError from a tool -> recoverable. Audited as TOOL_FAILED
        and reported back to the model, which may retry with better arguments.
      - Any other exception from a tool -> non-recoverable. Audited as
        AGENT_FAILED and the run ends FAILED.
      - Exceeding max_iterations -> audited as AGENT_MAX_ITERATIONS; the run
        ends FAILED without executing a further tool.
    """

    def __init__(
        self,
        model: ModelProvider,
        tool_registry: ToolRegistry,
        approval_store: ApprovalStore,
        audit_store: AuditStore,
        run_store: RunStore,
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
        self.run_store = run_store
        self.max_iterations = max_iterations

    # -- entry points ------------------------------------------------------

    def run(
        self,
        message: str,
    ) -> dict[str, Any]:
        run_id = self.run_store.create_run(message)

        conversation = [ModelMessage.user(message)]

        self.run_store.save_conversation(
            run_id,
            serialise_conversation(conversation),
        )

        return self.drive(run_id, conversation, [])

    def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
    ) -> dict[str, Any]:
        """Record the decision and, when approved, resume the original run."""
        pending = self.approval_store.get(approval_id)

        if pending is None:
            raise ValueError(f"Unknown approval ID: {approval_id}")

        if pending.status != "PENDING":
            raise ValueError(
                f"Approval {approval_id} was already resolved as {pending.status}."
            )

        run_id = pending.run_id
        arguments = pending.arguments

        if not approved:
            return self.reject(approval_id, pending, run_id, arguments)

        return self.approve(approval_id, pending, run_id, arguments)

    def reject(
        self,
        approval_id: str,
        pending: Any,
        run_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.approval_store.resolve(approval_id, approved=False)

        self.audit_store.record(
            "APPROVAL_DENIED",
            {
                "approval_id": approval_id,
                "tool": pending.tool,
                "arguments": arguments,
            },
            run_id=run_id,
        )

        self.run_store.add_step(
            run_id,
            StepType.APPROVAL_DENIED,
            tool_name=pending.tool,
            arguments=arguments,
        )

        self.run_store.cancel(run_id, APPROVAL_DENIED_ANSWER)

        return {
            "approval_id": approval_id,
            "approved": False,
            "tool": pending.tool,
            "result": None,
            "run_id": run_id,
            "run_status": RunStatus.CANCELLED.value,
            "answer": APPROVAL_DENIED_ANSWER,
            "trace": [],
            "approval_required": None,
        }

    def approve(
        self,
        approval_id: str,
        pending: Any,
        run_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        run = self.run_store.get_run(run_id)

        if run is None:
            raise ValueError(f"Unknown run ID: {run_id}")

        conversation = [ModelMessage.from_dict(entry) for entry in run.conversation]

        self.approval_store.resolve(approval_id, approved=True)
        self.run_store.resume(run_id)

        self.audit_store.record(
            "APPROVAL_GRANTED",
            {
                "approval_id": approval_id,
                "tool": pending.tool,
                "arguments": arguments,
            },
            run_id=run_id,
        )

        self.run_store.add_step(
            run_id,
            StepType.APPROVAL_GRANTED,
            tool_name=pending.tool,
            arguments=arguments,
        )

        call = ToolCall(
            id=pending.tool_call_id,
            name=pending.tool,
            arguments=arguments,
        )

        try:
            result = self.tool_registry.execute(
                pending.tool,
                arguments,
                approved=True,
            )

        except RECOVERABLE_TOOL_ERRORS as exc:
            conversation.append(
                ModelMessage.tool_result(
                    call.id,
                    call.name,
                    self.fail_tool(run_id, call, type(exc).__name__, str(exc)),
                )
            )

            outcome = self.drive(run_id, conversation, [])

            return self.approval_outcome(approval_id, pending.tool, None, outcome)

        except Exception as exc:  # noqa: BLE001 -- deliberate safety net
            return self.approval_outcome(
                approval_id,
                pending.tool,
                None,
                self.abort(run_id, call, exc, []),
            )

        self.record_execution(run_id, call, result)

        conversation.append(
            ModelMessage.tool_result(
                call.id,
                call.name,
                serialise_tool_result(result),
            )
        )

        trace = [
            {
                "tool": call.name,
                "arguments": arguments,
                "result": result,
            }
        ]

        outcome = self.drive(run_id, conversation, trace)

        return self.approval_outcome(approval_id, pending.tool, result, outcome)

    def approval_outcome(
        self,
        approval_id: str,
        tool: str,
        result: Any,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "approval_id": approval_id,
            "approved": True,
            "tool": tool,
            "result": result,
            "run_id": outcome["run_id"],
            "run_status": outcome["status"],
            "answer": outcome["answer"],
            "trace": outcome["trace"],
            "approval_required": outcome["approval_required"],
        }

    # -- the loop ----------------------------------------------------------

    def drive(
        self,
        run_id: str,
        conversation: list[ModelMessage],
        trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        for _ in range(self.max_iterations):
            response = self.model.generate_with_tools(
                conversation,
                self.tool_registry.definitions(),
            )

            self.run_store.add_step(
                run_id,
                StepType.MODEL_RESPONSE,
                result={
                    "text": response.text,
                    "tool_calls": [call.to_dict() for call in response.tool_calls],
                },
            )

            call = response.first_tool_call()

            if call is None:
                answer = response.text or ""

                self.run_store.complete(run_id, answer)

                return self.finished(run_id, RunStatus.COMPLETED, answer, trace)

            conversation.append(
                ModelMessage.assistant(response.text, [call]),
            )

            if call.argument_error is not None:
                conversation.append(
                    ModelMessage.tool_result(
                        call.id,
                        call.name,
                        self.fail_tool(
                            run_id,
                            call,
                            INVALID_ARGUMENTS_ERROR,
                            call.argument_error,
                        ),
                    )
                )

                self.save(run_id, conversation)

                continue

            self.audit_store.record(
                "TOOL_REQUESTED",
                {
                    "tool": call.name,
                    "arguments": call.arguments,
                },
                run_id=run_id,
            )

            self.run_store.add_step(
                run_id,
                StepType.TOOL_REQUESTED,
                tool_name=call.name,
                arguments=call.arguments,
            )

            try:
                result = self.tool_registry.execute(call.name, call.arguments)

            except ApprovalRequired as approval:
                return self.park(run_id, conversation, call, approval, trace)

            except RECOVERABLE_TOOL_ERRORS as exc:
                conversation.append(
                    ModelMessage.tool_result(
                        call.id,
                        call.name,
                        self.fail_tool(
                            run_id,
                            call,
                            type(exc).__name__,
                            str(exc),
                        ),
                    )
                )

                self.save(run_id, conversation)

                continue

            except Exception as exc:  # noqa: BLE001 -- deliberate safety net
                return self.abort(run_id, call, exc, trace)

            self.record_execution(run_id, call, result)

            trace.append(
                {
                    "tool": call.name,
                    "arguments": call.arguments,
                    "result": result,
                }
            )

            conversation.append(
                ModelMessage.tool_result(
                    call.id,
                    call.name,
                    serialise_tool_result(result),
                )
            )

            self.save(run_id, conversation)

        answer = MAX_ITERATIONS_ANSWER.format(max_iterations=self.max_iterations)

        self.audit_store.record(
            "AGENT_MAX_ITERATIONS",
            {
                "max_iterations": self.max_iterations,
                "tool_calls": len(trace),
            },
            run_id=run_id,
        )

        self.run_store.fail(run_id, answer)

        return self.finished(run_id, RunStatus.FAILED, answer, trace)

    # -- outcomes ----------------------------------------------------------

    def park(
        self,
        run_id: str,
        conversation: list[ModelMessage],
        call: ToolCall,
        approval: ApprovalRequired,
        trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Stop the run and wait for a human, keeping resumable state."""
        self.run_store.await_approval(
            run_id,
            serialise_conversation(conversation),
        )

        pending = self.approval_store.create(
            tool=approval.tool_name,
            arguments=approval.arguments,
            risk=approval.risk.value,
            run_id=run_id,
            tool_call_id=call.id,
        )

        details = {
            "approval_id": pending.approval_id,
            "tool": pending.tool,
            "arguments": pending.arguments,
            "risk": pending.risk,
        }

        self.audit_store.record("APPROVAL_REQUIRED", details, run_id=run_id)

        self.run_store.add_step(
            run_id,
            StepType.APPROVAL_REQUIRED,
            tool_name=pending.tool,
            arguments=pending.arguments,
        )

        return {
            "run_id": run_id,
            "status": RunStatus.WAITING_FOR_APPROVAL.value,
            "answer": (f"Approval required before executing {approval.tool_name}."),
            "trace": trace,
            "approval_required": {
                "approval_id": pending.approval_id,
                "run_id": run_id,
                "tool": pending.tool,
                "arguments": pending.arguments,
                "risk": pending.risk,
            },
        }

    def abort(
        self,
        run_id: str,
        call: ToolCall,
        exc: Exception,
        trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """An unexpected tool exception. Audited in full, reported generically."""
        self.audit_store.record(
            "AGENT_FAILED",
            {
                "tool": call.name,
                "arguments": call.arguments,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            run_id=run_id,
        )

        self.run_store.fail(run_id, AGENT_FAILED_ANSWER)

        return self.finished(run_id, RunStatus.FAILED, AGENT_FAILED_ANSWER, trace)

    def finished(
        self,
        run_id: str,
        status: RunStatus,
        answer: str,
        trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": status.value,
            "answer": answer,
            "trace": trace,
            "approval_required": None,
        }

    # -- shared recording --------------------------------------------------

    def record_execution(
        self,
        run_id: str,
        call: ToolCall,
        result: Any,
    ) -> None:
        self.audit_store.record(
            "TOOL_EXECUTED",
            {
                "tool": call.name,
                "arguments": call.arguments,
                "result": result,
            },
            run_id=run_id,
        )

        self.run_store.add_step(
            run_id,
            StepType.TOOL_EXECUTED,
            tool_name=call.name,
            arguments=call.arguments,
            result=result,
        )

    def fail_tool(
        self,
        run_id: str,
        call: ToolCall,
        error_type: str,
        message: str,
    ) -> str:
        """Audit a recoverable tool failure and build the model's feedback."""
        error = {
            "error_type": error_type,
            "error": message,
        }

        self.audit_store.record(
            "TOOL_FAILED",
            {
                "tool": call.name,
                "arguments": call.arguments or None,
                **error,
            },
            run_id=run_id,
        )

        self.run_store.add_step(
            run_id,
            StepType.TOOL_FAILED,
            tool_name=call.name,
            arguments=call.arguments or None,
            error=error,
        )

        return tool_failure_output(error_type, message)

    def save(
        self,
        run_id: str,
        conversation: list[ModelMessage],
    ) -> None:
        self.run_store.save_conversation(
            run_id,
            serialise_conversation(conversation),
        )
