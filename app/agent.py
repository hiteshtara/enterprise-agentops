import json

from app.model_provider import OpenAIModelProvider
from app.tools import calculator


class AgentService:

    def __init__(self, model: OpenAIModelProvider) -> None:
        self.model = model

    def run(self, message: str) -> str:
        input_items = [
            {
                "role": "user",
                "content": message,
            }
        ]

        while True:
            response = self.model.generate_with_tools(input_items)

            function_call = None

            for item in response.output:
                if item.type == "function_call":
                    function_call = item
                    break

            if function_call is None:
                return response.output_text

            if function_call.name != "calculator":
                raise ValueError(
                    f"Unknown tool: {function_call.name}"
                )

            arguments = json.loads(function_call.arguments)

            result = calculator(
                a=arguments["a"],
                b=arguments["b"],
                operation=arguments["operation"],
            )

            print(
                f"TOOL CALL: calculator "
                f"{arguments} -> {result}"
            )

            input_items.extend(
                [
                    function_call,
                    {
                        "type": "function_call_output",
                        "call_id": function_call.call_id,
                        "output": str(result),
                    },
                ]
            )