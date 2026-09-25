from datetime import UTC, datetime
from pathlib import Path

import discord

from bot.tools.base import ToolRegistry


def build_system_prompt(
    prompt_path: str,
    user: discord.User | discord.Member,
    channel: discord.abc.Messageable,
    guild: discord.Guild | None = None,
    tool_registry: ToolRegistry | None = None,
) -> str:
    path = Path(prompt_path)
    base_prompt = (
        path.read_text(encoding="utf-8").strip()
        if path.is_file()
        else "You are an intelligent, helpful AI assistant operating inside Discord."
    )

    if tool_registry:
        tool_lines = [
            f"- `{tool.name}`: {tool.description}" for tool in tool_registry.get_tools()
        ]
        tool_section = "## Available Tools & Capabilities\n" + "\n".join(tool_lines)
    else:
        tool_section = (
            "## Available Tools & Capabilities\n"
            "- `web_search`: Search the web for up-to-date information, news, or images.\n"
            "- `web_scrape`: Extract clean, readable markdown content from a specific webpage URL.\n"
            "- `memory_read`: Read persistent notes and preferences stored about the current user.\n"
            "- `memory_save`: Save a new fact, note, or preference about the current user to their persistent profile."
        )

    now_utc = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    user_info = f"User: {user.display_name} (@{user.name}, ID: {user.id})"

    channel_name = getattr(channel, "name", "direct-message")
    channel_id = getattr(channel, "id", "unknown")
    channel_topic = getattr(channel, "topic", None)

    channel_info = f"Channel: #{channel_name} (ID: {channel_id})"
    if channel_topic:
        channel_info += f"\nChannel Topic: {channel_topic}"

    if guild:
        guild_info = f"Server: {guild.name} (ID: {guild.id})"
        emojis = [
            f"<{'a' if e.animated else ''}:{e.name}:{e.id}>" for e in guild.emojis[:30]
        ]
        emoji_section = (
            f"Available Server Emojis: {' '.join(emojis)}"
            if emojis
            else "No custom server emojis."
        )
    else:
        guild_info = "Server: Direct Message"
        emoji_section = "No custom server emojis (DM)."

    context_section = (
        f"## Current Context\n"
        f"- Date/Time: {now_utc}\n"
        f"- {guild_info}\n"
        f"- {channel_info}\n"
        f"- {user_info}\n"
        f"- {emoji_section}"
    )

    return f"{base_prompt}\n\n{tool_section}\n\n{context_section}"
