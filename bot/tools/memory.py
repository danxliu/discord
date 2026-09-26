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
        return "Read the current user's Discord profile details and stored persistent memory notes, background, and preferences."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, context: ToolContext, **kwargs) -> str:
        status = (
            context.user_status
            if context.user_status
            else "Unavailable (presence data not available in this context)"
        )
        profile = "\n".join(
            [
                "## Discord Profile",
                f"- Display name: {context.user_display_name or context.user_name}",
                f"- Username: {context.user_name}",
                f"- User ID: {context.user_id}",
                f"- Profile: https://discord.com/users/{context.user_id}",
                f"- Avatar: {context.user_avatar_url or 'Unavailable'}",
                f"- Status: {status}",
                "- Bio: Unavailable via the standard Discord bot API",
                "- Pronouns: Unavailable via the standard Discord bot API",
            ]
        )
        memory = self._user_memory.read_memory(context.user_id)
        return f"{profile}\n\n## Stored Memory\n{memory}"


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
