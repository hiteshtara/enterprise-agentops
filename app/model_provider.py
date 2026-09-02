import json
from abc import ABC, abstractmethod
from typing import Any

from openai import OpenAI

from app.protocol import (
    MessageRole,
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolDefinition,
)

DEFAULT_MODEL = "gpt-5.4-mini"


class ModelProvider(ABC):
    """The seam between the agent runtime and a specific model vendor.

    Implementations translate the provider-neutral types in app.protocol to and
    from their vendor's wire format. AgentService only ever sees the neutral
    types, so adding Anthropic or Bedrock means adding a subclass here and
    changing nothing in the runtime.
    """

    @abstractmethod
    def generate(
        self,
        message: str,
    ) -> str:
        pass

    @abstractmethod
    def generate_with_tools(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
    ) -> ModelResponse:
        pass


class OpenAIModelProvider(ModelProvider):
    """Calls the OpenAI Responses API.

    The underlying client is built lazily on first use, so importing and wiring
    the application does not require OPENAI_API_KEY. Credential validation still
    happens in full -- it is simply deferred to the first real API call.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        client: OpenAI | None = None,
    ) -> None:
        self.model = model
        self._client = client

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI()

        return self._client

    def generate(
        self,
        message: str,
    ) -> str:
        response = self.client.responses.create(
            model=self.model,
            input=message,
        )

        return response.output_text

    def generate_with_tools(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
    ) -> ModelResponse:
        response = self.client.responses.create(
            model=self.model,
            input=self.to_openai_input(messages),
            tools=self.to_openai_tools(tools),
            parallel_tool_calls=False,
        )

        return self.from_openai_response(response)

    # -- translation: AgentGuard -> OpenAI ---------------------------------

    def to_openai_tools(
        self,
        tools: list[ToolDefinition],
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]

    def to_openai_input(
        self,
        messages: list[ModelMessage],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        for message in messages:
            if message.role is MessageRole.TOOL:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content or "",
                    }
                )

                continue

            if message.role is MessageRole.ASSISTANT:
                if message.content:
                    items.append(
                        {
                            "role": "assistant",
                            "content": message.content,
                        }
                    )

                for call in message.tool_calls:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        }
                    )

                continue

            items.append(
                {
                    "role": message.role.value,
                    "content": message.content or "",
                }
            )

        return items

    # -- translation: OpenAI -> AgentGuard ---------------------------------

    def from_openai_response(self, response: Any) -> ModelResponse:
        tool_calls: list[ToolCall] = []

        for item in response.output:
            if getattr(item, "type", None) != "function_call":
                continue

            tool_calls.append(self.to_tool_call(item))

        text = response.output_text or None

        return ModelResponse(text=text, tool_calls=tool_calls)

    def to_tool_call(self, item: Any) -> ToolCall:
        try:
            arguments = json.loads(item.arguments)

        except (TypeError, ValueError) as exc:
            # The model emitted arguments that are not valid JSON. Represent the
            # failure rather than raising, so the runtime can tell the model.
            return ToolCall(
                id=item.call_id,
                name=item.name,
                arguments={},
                argument_error=f"Tool arguments were not valid JSON: {exc}",
            )

        if not isinstance(arguments, dict):
            return ToolCall(
                id=item.call_id,
                name=item.name,
                arguments={},
                argument_error=(
                    f"Tool arguments must be a JSON object, got "
                    f"{type(arguments).__name__}."
                ),
            )

        return ToolCall(
            id=item.call_id,
            name=item.name,
            arguments=arguments,
        )
