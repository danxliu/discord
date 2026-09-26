from typing import Any

from bot.memory.user import UserMemory
from bot.tools.base import BaseTool, ToolContext


class MemoryReadTool(BaseTool):
    def __init__(self, user_memory: UserMemory):
        self._user_memory = user_memory

    @property
    def name(self) -> str:
        return "mem_read"

    @property
    def display_name(self) -> str:
        return "Memory Read"

    @property
    def description(self) -> str:
        return "Read the stored persistent memory notes, background, and preferences for the current user."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, context: ToolContext, **kwargs) -> str:
        return self._user_memory.read_memory(context.user_id)


class MemoryAddTool(BaseTool):
    def __init__(self, user_memory: UserMemory):
        self._user_memory = user_memory

    @property
    def name(self) -> str:
        return "mem_add"

    @property
    def display_name(self) -> str:
        return "Memory Add"

    @property
    def description(self) -> str:
        return "Add a fact, note, or preference about the current user to their persistent profile."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The information or preference to remember about the user",
                },
                "category": {
                    "type": "string",
                    "description": "Category name. Defaults to 'Facts & Preferences'.",
                },
            },
            "required": ["fact"],
        }

    async def execute(
        self,
        context: ToolContext,
        fact: str,
        category: str = "Facts & Preferences",
        **kwargs,
    ) -> str:
        return self._user_memory.save_fact(
            context.user_id, context.user_name, fact, category
        )


class MemoryRemoveTool(BaseTool):
    def __init__(self, user_memory: UserMemory):
        self._user_memory = user_memory

    @property
    def name(self) -> str:
        return "mem_remove"

    @property
    def display_name(self) -> str:
        return "Memory Remove"

    @property
    def description(self) -> str:
        return "Remove an exact note from the current user's persistent profile."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The exact note text to remove",
                },
                "category": {
                    "type": "string",
                    "description": "Category to search. Defaults to 'Facts & Preferences'.",
                },
            },
            "required": ["fact"],
        }

    async def execute(
        self,
        context: ToolContext,
        fact: str,
        category: str = "Facts & Preferences",
        **kwargs,
    ) -> str:
        return self._user_memory.remove_fact(context.user_id, fact, category)
