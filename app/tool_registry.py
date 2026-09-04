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
    """One governed capability.

    `model_callable` decides whether the model is *told* this tool exists. It
    does not decide anything else: a non-model-callable tool is registered,
    audited, risk-tiered and approval-gated exactly like any other, and the
    console still sees it in `describe()`. Only `definitions()` -- the
    advertisement the provider sends to the model -- leaves it out.

    That separation is the same one that keeps `risk` out of `definitions()`.
    Some capabilities are for the application to invoke on a person's behalf,
    with the text a person wrote: `AgentService.request_action` submits them and
    a human approves them. Letting the model discover such a tool would hand it
    the one action the design deliberately keeps out of its reach, while
    removing the registration would take the action outside governance
    altogether. Registered-but-unadvertised is the only shape that is both.
    """

    name: str
    description: str
    function: Callable[..., Any]
    parameters: dict[str, Any]
    risk: ToolRisk = ToolRisk.READ
    model_callable: bool = True

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
        """Every *model-callable* tool, in provider-neutral form.

        Tools registered with `model_callable=False` are absent. The model
        cannot call what it has not been told about, and a name it invents is
        rejected by `execute` as an unknown tool -- so the filter here is the
        whole advertisement boundary. Governance is unaffected: `describe()`
        still lists them for the console and `execute()` still gates them on
        their risk tier.
        """
        return [
            tool.definition() for tool in self._tools.values() if tool.model_callable
        ]

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

        Every registered tool appears here, including the ones the model is
        never told about. An operator reviewing what this deployment can do
        must see the whole surface; hiding a capability from the governance
        view is how a capability stops being governed.
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
