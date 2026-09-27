import logging
from typing import Any

import discord

from bot.discord.attachments import attachment_to_text
from bot.discord.context import extract_message_text
from bot.discord.image import (
    is_image_attachment,
    is_video_attachment,
    load_media_from_message,
)
from bot.tools.base import BaseTool, ToolContext, ToolResult

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
                return (
                    "Error: Threads can only be created from messages in text channels."
                )
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
    MAX_MESSAGE_TEXT = 2000
    MAX_ATTACHMENT_TEXT = 1200

    @property
    def name(self) -> str:
        return "discord_history_search"

    @property
    def display_name(self) -> str:
        return "Search Discord History"

    @property
    def description(self) -> str:
        return (
            "Search recent messages in the current channel by text, or retrieve a "
            "bounded window before, after, or around the message that invoked you. "
            "Results include embeds, attachments, media, and reaction summaries."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to find; required in keyword mode",
                },
                "mode": {
                    "type": "string",
                    "enum": ["keyword", "relative"],
                    "description": "Keyword search (default) or messages relative to the invoking message",
                },
                "direction": {
                    "type": "string",
                    "enum": ["before", "after", "around"],
                    "description": "Relative mode direction; defaults to around",
                },
                "count": {
                    "type": "integer",
                    "description": "Maximum number of results, from 1 to 10",
                },
            },
            "required": [],
        }

    async def _relative_messages(
        self,
        context: ToolContext,
        channel: Any,
        direction: str,
        count: int,
    ) -> list[Any] | str:
        if context.triggering_message_id is None:
            return "Error: Relative search requires an invoking message."
        anchor, error = await self._get_message(
            context, channel, context.triggering_message_id
        )
        if error:
            return error

        before_count = count if direction == "before" else 0
        after_count = count if direction == "after" else 0
        if direction == "around":
            before_count = (count + 1) // 2
            after_count = count // 2

        messages: list[Any] = []
        if before_count:
            previous = [
                item
                async for item in channel.history(limit=before_count, before=anchor)
            ]
            messages.extend(reversed(previous))
        if after_count:
            following = [
                item
                async for item in channel.history(
                    limit=after_count, after=anchor, oldest_first=True
                )
            ]
            messages.extend(following)
        return messages

    async def _describe_messages(
        self,
        messages: list[Any],
        context: ToolContext,
        *,
        query: str | None = None,
    ) -> str | ToolResult:
        lines: list[str] = []
        multimodal: list[dict[str, Any]] = []
        video_fallbacks: dict[str, list[dict[str, Any]]] = {}
        seen_urls: set[str] = set()

        for message in messages[: self.MAX_RESULTS]:
            text = extract_message_text(message, include_attachments=False)
            attachment_notes: list[str] = []
            for attachment in message.attachments:
                if is_video_attachment(attachment):
                    attachment_notes.append(
                        f"[Video attachment: {attachment.filename}]"
                    )
                elif is_image_attachment(attachment):
                    attachment_notes.append(
                        f"[Image attachment: {attachment.filename}]"
                    )
                else:
                    try:
                        extracted = await attachment_to_text(
                            attachment, max_chars=self.MAX_ATTACHMENT_TEXT
                        )
                    except Exception:
                        extracted = f"Could not read attachment {attachment.filename}."
                    attachment_notes.append(
                        f"[Attachment: {attachment.filename} ({attachment.url})]\n{extracted}"
                    )
            if attachment_notes:
                text = "\n\n".join([part for part in [text, *attachment_notes] if part])

            reactions = getattr(message, "reactions", None) or []
            if reactions:
                text = "\n\n".join(
                    [
                        part
                        for part in [
                            text,
                            "Reactions: "
                            + ", ".join(
                                f"{reaction.emoji} {reaction.count}"
                                for reaction in reactions
                            ),
                        ]
                        if part
                    ]
                )

            if query and query.casefold() not in text.casefold():
                continue
            if len(text) > self.MAX_MESSAGE_TEXT:
                text = text[: self.MAX_MESSAGE_TEXT - 3] + "..."

            author = getattr(message.author, "display_name", message.author.name)
            timestamp = getattr(message, "created_at", None)
            timestamp_text = timestamp.isoformat() if timestamp else "unknown time"
            lines.append(
                f"Message {message.id} by {author} at {timestamp_text}:\n{text or '[No text content]'}"
            )

            media_parts, fallbacks = await load_media_from_message(
                message, seen_urls=seen_urls
            )
            video_fallbacks.update(fallbacks)
            if media_parts:
                multimodal.append(
                    {
                        "type": "text",
                        "text": f"Media attached to message {message.id} by {author}.",
                    }
                )
                multimodal.extend(media_parts)

        if not lines:
            if query:
                return f"No recent messages matched '{query}' in this channel."
            return "No messages were found in that relative window."

        heading = (
            f"Found {len(lines)} recent match(es) for '{query}':"
            if query
            else f"Found {len(lines)} message(s) relative to the invoking message:"
        )
        result_text = heading + "\n\n" + "\n\n".join(lines)
        if multimodal:
            return ToolResult(result_text, multimodal, video_fallbacks)
        return result_text

    async def execute(
        self,
        context: ToolContext,
        query: str | None = None,
        mode: str = "keyword",
        direction: str = "around",
        count: int = MAX_RESULTS,
        **kwargs,
    ) -> str | ToolResult:
        if not isinstance(mode, str):
            return "Error: Mode must be 'keyword' or 'relative'."
        mode = mode.strip().lower()
        if mode not in {"keyword", "relative"}:
            return "Error: Mode must be 'keyword' or 'relative'."
        if not isinstance(direction, str) or direction not in {
            "before",
            "after",
            "around",
        }:
            return "Error: Direction must be 'before', 'after', or 'around'."
        try:
            count = int(count)
        except (TypeError, ValueError):
            return "Error: Count must be an integer from 1 to 10."
        if not 1 <= count <= self.MAX_RESULTS:
            return "Error: Count must be from 1 to 10."
        if query is not None and not isinstance(query, str):
            return "Error: The search query must be text."
        query = (query or "").strip()
        if mode == "keyword" and not query:
            return "Error: The search query cannot be empty in keyword mode."

        try:
            channel = await self._get_channel(context)
            if channel is None:
                return "Error: The current channel could not be resolved."
            if not hasattr(channel, "history"):
                return "Error: This channel does not support message history."

            if mode == "relative":
                result = await self._relative_messages(
                    context, channel, direction, count
                )
                if isinstance(result, str):
                    return result
                return await self._describe_messages(result, context)

            matches = []
            async for message in channel.history(limit=self.MAX_SCAN_MESSAGES):
                text = extract_message_text(message)
                if query.casefold() not in text.casefold():
                    continue
                matches.append(message)
                if len(matches) >= min(count, self.MAX_RESULTS):
                    break
            matches.reverse()
            return await self._describe_messages(matches, context, query=query)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            self._log_discord_error(context, self.name)
            if isinstance(error, discord.Forbidden):
                return "Error: Discord denied access to this channel's message history."
            if isinstance(error, discord.NotFound):
                return "Error: Discord could not find the channel, invoking message, or search result."
            return "Error: Discord could not read this channel's history. Please try again later."
