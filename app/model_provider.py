from abc import ABC, abstractmethod

from openai import OpenAI


CALCULATOR_TOOL = {
    "type": "function",
    "name": "calculator",
    "description": "Perform a basic arithmetic operation.",
    "parameters": {
        "type": "object",
        "properties": {
            "a": {"type": "number"},
            "b": {"type": "number"},
            "operation": {
                "type": "string",
                "enum": [
                    "add",
                    "subtract",
                    "multiply",
                    "divide",
                ],
            },
        },
        "required": ["a", "b", "operation"],
        "additionalProperties": False,
    },
}


class ModelProvider(ABC):

    @abstractmethod
    def generate(self, message: str) -> str:
        pass


class OpenAIModelProvider(ModelProvider):

    def __init__(self) -> None:
        self.client = OpenAI()

    def generate(self, message: str) -> str:
        response = self.client.responses.create(
            model="gpt-5.4-mini",
            input=message,
        )
        return response.output_text

    def generate_with_tools(self, input_items):
        return self.client.responses.create(
            model="gpt-5.4-mini",
            input=input_items,
            tools=[CALCULATOR_TOOL],
            parallel_tool_calls=False,
        )