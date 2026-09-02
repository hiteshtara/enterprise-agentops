import json
from typing import Any

from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.model_provider import ModelProvider
from app.tool_registry import (
    ApprovalRequired,
    ToolRegistry,
)


class AgentService:
    def __init__(
        self,
        model: ModelProvider,
        tool_registry: ToolRegistry,
        approval_store: ApprovalStore,
        audit_store: AuditStore,
    ) -> None:
        self.model = model
        self.tool_registry = tool_registry
        self.approval_store = approval_store
        self.audit_store = audit_store

    def run(
        self,
        message: str,
    ) -> dict[str, Any]:
        input_items = [
            {
                "role": "user",
                "content": message,
            }
        ]

        trace = []

        while True:
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

            arguments = json.loads(function_call.arguments)

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

            if isinstance(result, dict):
                tool_output = json.dumps(result)
            else:
                tool_output = str(result)

            input_items.extend(
                [
                    function_call,
                    {
                        "type": ("function_call_output"),
                        "call_id": (function_call.call_id),
                        "output": tool_output,
                    },
                ]
            )

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
