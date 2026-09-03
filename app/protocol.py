"""Provider-neutral types exchanged between the agent runtime and a model.

Nothing in this module knows about OpenAI, Anthropic, or Bedrock. A
ModelProvider translates between these types and its vendor's wire format, so
AgentService and ToolRegistry never touch a vendor SDK object.

Every type here is JSON-serialisable via to_dict()/from_dict(), which is what
makes a run's conversation safe to persist and resume.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolDefinition:
    """A tool as advertised to a model. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class ToolCall:
    """A model's request to invoke one tool.

    `arguments` is always a dict. When the model emits arguments that are not
    valid JSON the provider records why in `argument_error` and leaves
    `arguments` empty, so a malformed call stays representable instead of
    raising inside the provider.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    argument_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "argument_error": self.argument_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCall":
        return cls(
            id=data["id"],
            name=data["name"],
            arguments=data.get("arguments") or {},
            argument_error=data.get("argument_error"),
        )


@dataclass
class ModelUsage:
    """Token accounting for one model call, normalised across providers.

    Every field is optional. A provider that does not report a figure leaves it
    None, which stays None all the way to the console -- an unknown count is
    never rendered as zero.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass
class ModelResponse:
    """One model turn: free text, tool calls, or both.

    `usage`, `model_name` and `provider_request_id` are observability metadata
    normalised by the provider. No vendor SDK object reaches the runtime.
    """

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: ModelUsage | None = None
    model_name: str | None = None
    provider_request_id: str | None = None

    def first_tool_call(self) -> ToolCall | None:
        """The runtime executes one tool per iteration; this is that one."""
        return self.tool_calls[0] if self.tool_calls else None


@dataclass
class ModelMessage:
    """One entry of durable conversation state."""

    role: MessageRole
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None

    @classmethod
    def user(cls, content: str) -> "ModelMessage":
        return cls(role=MessageRole.USER, content=content)

    @classmethod
    def assistant(
        cls,
        content: str | None = None,
        tool_calls: list[ToolCall] | None = None,
    ) -> "ModelMessage":
        return cls(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=list(tool_calls or []),
        )

    @classmethod
    def tool_result(
        cls,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> "ModelMessage":
        return cls(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "content": self.content,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelMessage":
        return cls(
            role=MessageRole(data["role"]),
            content=data.get("content"),
            tool_calls=[
                ToolCall.from_dict(call) for call in data.get("tool_calls") or []
            ],
            tool_call_id=data.get("tool_call_id"),
            tool_name=data.get("tool_name"),
        )


def serialise_conversation(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    return [message.to_dict() for message in messages]


def deserialise_conversation(data: list[dict[str, Any]]) -> list[ModelMessage]:
    return [ModelMessage.from_dict(entry) for entry in data]
