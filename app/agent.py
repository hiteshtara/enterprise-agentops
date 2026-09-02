import json

from app.model_provider import OpenAIModelProvider
from app.tool_registry import ToolRegistry


class AgentService:
    def __init__(
        self,
        model: OpenAIModelProvider,
        tool_registry: ToolRegistry,
    ) -> None:
        self.model = model
        self.tool_registry = tool_registry

    def run(self, message: str) -> tuple[str, list[dict]]:
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
                return response.output_text, trace

            arguments = json.loads(function_call.arguments)

            result = self.tool_registry.execute(
                function_call.name,
                arguments,
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
                        "type": "function_call_output",
                        "call_id": function_call.call_id,
                        "output": tool_output,
                    },
                ]
            )