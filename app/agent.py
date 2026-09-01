import json

from app.model_provider import OpenAIModelProvider
from app.tools import calculator, get_migration_status


class AgentService:

    def __init__(self, model: OpenAIModelProvider) -> None:
        self.model = model

    def run(self, message: str) -> tuple[str, list[dict]]:
        input_items = [
            {
                "role": "user",
                "content": message,
            }
        ]

        trace = []

        while True:
            response = self.model.generate_with_tools(input_items)

            function_call = None

            for item in response.output:
                if item.type == "function_call":
                    function_call = item
                    break

            if function_call is None:
                return response.output_text, trace

            arguments = json.loads(function_call.arguments)

            if function_call.name == "calculator":
                result = calculator(
                    a=arguments["a"],
                    b=arguments["b"],
                    operation=arguments["operation"],
                )

            elif function_call.name == "get_migration_status":
                result = get_migration_status(
                    batch_id=arguments["batch_id"],
                )

            else:
                raise ValueError(
                    f"Unknown tool: {function_call.name}"
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