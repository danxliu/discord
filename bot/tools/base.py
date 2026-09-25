import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    user_id: int
    user_name: str
    channel_id: int
    guild_id: int | None = None
    request_id: str | None = None


class BaseTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        pass

    @abstractmethod
    async def execute(self, context: ToolContext, **kwargs) -> str:
        pass

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def get_schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    async def execute(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found."
        try:
            return await tool.execute(context, **arguments)
        except Exception as e:
            logger.exception(
                "Tool execution failed tool=%s request_id=%s",
                name,
                context.request_id,
            )
            return f"Error executing tool '{name}': {str(e)}"
