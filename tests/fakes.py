"""Deterministic ModelProvider fakes.

These speak only the provider-neutral protocol -- no OpenAI SDK shapes -- which
is what makes them valid substitutes for any future provider.
"""

from typing import Any

from app.model_provider import ModelProvider
from app.protocol import (
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolDefinition,
)


def tool_response(
    name: str,
    arguments: dict[str, Any] | None = None,
    call_id: str = "call-1",
    argument_error: str | None = None,
    text: str | None = None,
) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=[
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments or {},
                argument_error=argument_error,
            )
        ],
    )


def final_response(text: str) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=[])


class RecordingModelProvider(ModelProvider):
    """Base fake: records every conversation and tool list it is handed."""

    def __init__(self) -> None:
        self.call_count = 0
        self.conversations: list[list[ModelMessage]] = []
        self.tool_definitions: list[list[ToolDefinition]] = []

    def generate(self, message: str) -> str:
        raise AssertionError("generate() must not be used by the agent loop.")

    def observe(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
    ) -> None:
        self.call_count += 1
        self.conversations.append(list(messages))
        self.tool_definitions.append(list(tools))

    def tool_results_seen(self) -> list[str]:
        """Every tool-result payload the model has been shown, in order."""
        seen: list[str] = []

        for conversation in self.conversations:
            for message in conversation:
                if message.role.value == "tool" and message.content not in seen:
                    seen.append(message.content)

        return seen

    def generate_with_tools(self, messages, tools) -> ModelResponse:
        raise NotImplementedError


class ScriptedModelProvider(RecordingModelProvider):
    """Replays a fixed list of ModelResponses, repeating the last one."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__()
        self.responses = list(responses)

    def generate_with_tools(self, messages, tools) -> ModelResponse:
        index = min(self.call_count, len(self.responses) - 1)

        self.observe(messages, tools)

        return self.responses[index]


class LoopingModelProvider(RecordingModelProvider):
    """Always asks for the same valid tool call, never producing an answer."""

    def __init__(
        self,
        name: str = "query_migration_batches",
        arguments: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self.arguments = arguments if arguments is not None else {"limit": 1}

    def generate_with_tools(self, messages, tools) -> ModelResponse:
        self.observe(messages, tools)

        return tool_response(
            self.name,
            self.arguments,
            call_id=f"loop-{self.call_count}",
        )


class UnusedModelProvider(ModelProvider):
    """Fails loudly if the runtime calls the model at all."""

    def generate(self, message: str) -> str:
        raise AssertionError("The model must not be called.")

    def generate_with_tools(self, messages, tools) -> ModelResponse:
        raise AssertionError("The model must not be called.")
