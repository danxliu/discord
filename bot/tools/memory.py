from typing import Any, Dict
from bot.memory.user import UserMemory
from bot.tools.base import BaseTool, ToolContext


class MemoryReadTool(BaseTool):
    def __init__(self, user_memory: UserMemory):
        self._user_memory = user_memory

    @property
    def name(self) -> str:
        return "memory_read"

    @property
    def display_name(self) -> str:
        return "Memory Read"

    @property
    def description(self) -> str:
        return (
            "Read the stored persistent memory notes, background, and preferences for the current user."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, context: ToolContext, **kwargs) -> str:
        return self._user_memory.read_memory(context.user_id)


class MemorySaveTool(BaseTool):
    def __init__(self, user_memory: UserMemory):
        self._user_memory = user_memory

    @property
    def name(self) -> str:
        return "memory_save"

    @property
    def display_name(self) -> str:
        return "Memory Save"

    @property
    def description(self) -> str:
        return (
            "Save a new fact, note, or preference about the current user to their persistent profile."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The information or preference to remember about the user",
                },
                "category": {
                    "type": "string",
                    "description": "Category name (e.g. 'Facts & Preferences', 'Projects', 'Technical Setup'). Defaults to 'Facts & Preferences'.",
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
