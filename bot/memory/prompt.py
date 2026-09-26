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

    tools = tool_registry.get_tools() if tool_registry is not None else []
    tool_lines = [f"- `{tool.name}`: {tool.description}" for tool in tools]
    tool_section = "## Available Tools & Capabilities\n" + (
        "\n".join(tool_lines) if tool_lines else "No tools are currently registered."
    )

    now_utc = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    channel_name = getattr(channel, "name", "direct-message")
    channel_id = getattr(channel, "id", "unknown")
    channel_topic = getattr(channel, "topic", None)

    channel_info = f"Channel: #{channel_name} (ID: {channel_id})"
    if channel_topic:
        channel_info += f"\n- Channel Topic: {channel_topic}"

    if guild:
        environment_info = (
            f"Environment: Discord Server / Guild '{guild.name}' (ID: {guild.id})"
        )
        speaker_info = (
            f"Current Speaker (invoking user): {user.display_name} (@{user.name}, ID: {user.id})\n"
            f"- Multi-User Channel: Multiple people participate in this channel. Messages from users are prefixed with `[DisplayName (@username)]:`. "
            f"Address and respond directly to the Current Speaker ({user.display_name}) unless they ask about someone else."
        )
        emojis = [
            f"<{'a' if e.animated else ''}:{e.name}:{e.id}>" for e in guild.emojis[:30]
        ]
        emoji_section = (
            f"Available Server Emojis: {' '.join(emojis)}"
            if emojis
            else "No custom server emojis."
        )
    else:
        environment_info = "Environment: Direct Message (1-on-1 private chat)"
        speaker_info = f"User: {user.display_name} (@{user.name}, ID: {user.id})"
        emoji_section = "No custom server emojis (DM)."

    context_section = (
        f"## Current Context\n"
        f"- Date/Time: {now_utc}\n"
        f"- {environment_info}\n"
        f"- {channel_info}\n"
        f"- {speaker_info}\n"
        f"- {emoji_section}"
    )

    return f"{base_prompt}\n\n{tool_section}\n\n{context_section}"
