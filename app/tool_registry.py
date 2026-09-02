from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.protocol import ToolDefinition


class ToolRisk(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    DANGEROUS = "DANGEROUS"


@dataclass
class Tool:
    name: str
    description: str
    function: Callable[..., Any]
    parameters: dict[str, Any]
    risk: ToolRisk = ToolRisk.READ

    def definition(self) -> ToolDefinition:
        """The provider-neutral advertisement of this tool."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


class ApprovalRequired(Exception):
    def __init__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        risk: ToolRisk,
    ) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.risk = risk

        super().__init__(f"Approval required for {tool_name}")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        tool: Tool,
    ) -> None:
        self._tools[tool.name] = tool

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        approved: bool = False,
    ) -> Any:
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")

        tool = self._tools[name]

        if tool.risk != ToolRisk.READ and not approved:
            raise ApprovalRequired(
                tool_name=name,
                arguments=arguments,
                risk=tool.risk,
            )

        return tool.function(**arguments)

    def definitions(
        self,
    ) -> list[ToolDefinition]:
        """Every registered tool, in provider-neutral form."""
        return [tool.definition() for tool in self._tools.values()]

    def get(
        self,
        name: str,
    ) -> Tool | None:
        return self._tools.get(name)

    def describe(
        self,
    ) -> list[dict[str, Any]]:
        """Operator-facing metadata for the console.

        Includes the risk tier, which definitions() deliberately omits -- the
        model has no business knowing how a tool is governed. Metadata only:
        the callable itself is never exposed.
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk": tool.risk.value,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]
