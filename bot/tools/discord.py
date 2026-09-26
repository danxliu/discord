import logging
from typing import Any

import discord

from bot.discord.context import extract_message_text
from bot.tools.base import BaseTool, ToolContext

logger = logging.getLogger(__name__)


class DiscordTool(BaseTool):
    def __init__(self, client: discord.Client):
        self.client = client

    async def _get_channel(self, context: ToolContext):
        channel = self.client.get_channel(context.channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(context.channel_id)

        channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if context.guild_id is not None and channel_guild_id != context.guild_id:
            return None
        return channel

    async def _get_message(
        self,
        context: ToolContext,
        channel: Any,
        message_id: int | str | None,
    ):
        target_id = (
            message_id if message_id is not None else context.triggering_message_id
        )
        if target_id is None:
            return None, "Error: No target message is available; provide a message ID."
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return None, "Error: The message ID must be an integer."

        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return None, "Error: This channel does not support fetching messages."
        return await fetch_message(target_id), None

    def _log_discord_error(self, context: ToolContext, tool_name: str) -> None:
        logger.warning(
            "Discord tool failed request_id=%s tool=%s",
            context.request_id,
            tool_name,
            exc_info=True,
        )

    def _discord_error(self, context: ToolContext, tool_name: str, error: Exception):
        self._log_discord_error(context, tool_name)
        if isinstance(error, discord.NotFound):
            return "Error: Discord could not find that channel or message."
        if isinstance(error, discord.Forbidden):
            return "Error: Discord denied that action; the bot may be missing channel permissions."
        return "Error: Discord could not complete the request. Please try again later."


class DiscordReactionAddTool(DiscordTool):
    @property
    def name(self) -> str:
        return "discord_reaction_add"

    @property
    def display_name(self) -> str:
        return "Add Discord Reaction"

    @property
    def description(self) -> str:
        return (
            "Add the bot's reaction to a message in the current channel. "
            "Defaults to the message that invoked the assistant; optionally provide "
            "a message ID from this channel."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "A Unicode emoji or custom Discord emoji such as <:name:id>",
                },
                "message_id": {
                    "type": "integer",
                    "description": "Optional message ID in the current channel",
                },
            },
            "required": ["emoji"],
        }

    async def execute(
        self,
        context: ToolContext,
        emoji: str,
        message_id: int | str | None = None,
        **kwargs,
    ) -> str:
        emoji = emoji.strip()
        if not emoji:
            return "Error: The emoji cannot be empty."

        try:
            channel = await self._get_channel(context)
            if channel is None:
                return "Error: The current channel could not be resolved."
            message, error = await self._get_message(context, channel, message_id)
            if error:
                return error
            await message.add_reaction(emoji)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            return self._discord_error(context, self.name, error)

        return f"Added {emoji} reaction to message {message.id}."


class DiscordThreadCreateTool(DiscordTool):
    @property
    def name(self) -> str:
        return "discord_thread_create"

    @property
    def display_name(self) -> str:
        return "Create Discord Thread"

    @property
    def description(self) -> str:
        return (
            "Create a thread from a message in the current text channel. "
            "Defaults to the message that invoked the assistant; optionally provide "
            "a message ID from this channel."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Thread name, from 1 to 100 characters",
                },
                "message_id": {
                    "type": "integer",
                    "description": "Optional message ID in the current channel",
                },
            },
            "required": ["name"],
        }

    async def execute(
        self,
        context: ToolContext,
        name: str,
        message_id: int | str | None = None,
        **kwargs,
    ) -> str:
        name = name.strip()
        if not name or len(name) > 100:
            return "Error: The thread name must contain between 1 and 100 characters."

        try:
            channel = await self._get_channel(context)
            if channel is None:
                return "Error: The current channel could not be resolved."
            channel_type = getattr(channel, "type", None)
            if channel_type not in (discord.ChannelType.text, discord.ChannelType.news):
                return "Error: Threads can only be created from messages in text channels."
            message, error = await self._get_message(context, channel, message_id)
            if error:
                return error
            thread = await message.create_thread(name=name)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            return self._discord_error(context, self.name, error)

        guild_id = getattr(getattr(channel, "guild", None), "id", context.guild_id)
        link = f"https://discord.com/channels/{guild_id}/{thread.id}"
        return f"Created thread **{thread.name}**: {link} (ID: {thread.id})."


class DiscordHistorySearchTool(DiscordTool):
    MAX_SCAN_MESSAGES = 500
    MAX_RESULTS = 10
    MAX_EXCERPT_LENGTH = 300

    @property
    def name(self) -> str:
        return "discord_history_search"

    @property
    def display_name(self) -> str:
        return "Search Discord History"

    @property
    def description(self) -> str:
        return (
            "Search up to the 500 most recent messages in the current channel for "
            "a case-insensitive text match, returning at most 10 results."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to find in recent messages in this channel",
                },
            },
            "required": ["query"],
        }

    def _excerpt(self, text: str, query: str) -> str:
        match_at = text.casefold().find(query.casefold())
        start = max(0, match_at - self.MAX_EXCERPT_LENGTH // 3)
        end = min(len(text), start + self.MAX_EXCERPT_LENGTH)
        excerpt = text[start:end]
        if start:
            excerpt = "..." + excerpt
        if end < len(text):
            excerpt += "..."
        return excerpt

    async def execute(
        self,
        context: ToolContext,
        query: str,
        **kwargs,
    ) -> str:
        query = query.strip()
        if not query:
            return "Error: The search query cannot be empty."

        try:
            channel = await self._get_channel(context)
            if channel is None:
                return "Error: The current channel could not be resolved."
            if not hasattr(channel, "history"):
                return "Error: This channel does not support message history."

            matches = []
            async for message in channel.history(limit=self.MAX_SCAN_MESSAGES):
                text = extract_message_text(message)
                if query.casefold() not in text.casefold():
                    continue
                matches.append((message, text))
                if len(matches) >= self.MAX_RESULTS:
                    break
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            self._log_discord_error(context, self.name)
            if isinstance(error, discord.Forbidden):
                return "Error: Discord denied access to this channel's message history."
            if isinstance(error, discord.NotFound):
                return "Error: The current channel could not be found."
            return "Error: Discord could not read this channel's history. Please try again later."

        if not matches:
            return f"No recent messages matched '{query}' in this channel."

        lines = [f"Found {len(matches)} recent match(es) for '{query}':"]
        for message, text in matches:
            author = getattr(message.author, "display_name", message.author.name)
            timestamp = getattr(message, "created_at", None)
            timestamp_text = timestamp.isoformat() if timestamp else "unknown time"
            lines.append(
                f"- Message {message.id} by {author} at {timestamp_text}: "
                f"{self._excerpt(text, query)}"
            )
        return "\n".join(lines)
