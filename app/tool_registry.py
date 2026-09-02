from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Tool:
    name: str
    description: str
    function: Callable[..., Any]
    parameters: dict[str, Any]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")

        tool = self._tools[name]
        return tool.function(**arguments)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            tool.schema()
            for tool in self._tools.values()
        ]