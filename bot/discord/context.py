from datetime import datetime
from typing import Any, Dict, List, Optional

import discord

from bot.discord.image import (
    extract_image_urls_from_text_and_embeds,
    format_turn_content,
    is_image_attachment,
    load_images_from_message,
    merge_turn_contents,
)


def clean_prompt(content: str, client_user: Optional[discord.ClientUser] = None) -> str:
    if not content:
        return ""
    cleaned = content
    if client_user:
        cleaned = cleaned.replace(f"<@{client_user.id}>", "")
        cleaned = cleaned.replace(f"<@!{client_user.id}>", "")
    return cleaned.strip()


def extract_message_text(msg: discord.Message) -> str:
    parts = []
    if msg.content:
        parts.append(msg.content)
    for embed in msg.embeds:
        embed_parts = []
        if embed.title:
            embed_parts.append(embed.title)
        if embed.description:
            embed_parts.append(embed.description)
        for field in embed.fields:
            embed_parts.append(f"{field.name}: {field.value}")
        if embed_parts:
            parts.append("\n".join(embed_parts))
    for attachment in msg.attachments:
        if is_image_attachment(attachment):
            parts.append(f"[Image: {attachment.filename}]")
        else:
            parts.append(f"[Attachment: {attachment.filename} ({attachment.url})]")
    return "\n\n".join(parts).strip()


async def get_channel_context_messages(
    channel: discord.abc.Messageable,
    client_user: Optional[discord.ClientUser],
    limit: int = 10,
    before: Optional[discord.Message] = None,
    after_timestamp: Optional[datetime] = None,
    exclude_message_id: Optional[int] = None,
    max_images: int = 0,
    max_size_bytes: int = 20 * 1024 * 1024,
) -> List[Dict[str, Any]]:
    if limit <= 0 or not hasattr(channel, "history"):
        return []

    try:
        kwargs: Dict[str, object] = {"limit": limit}
        if before:
            kwargs["before"] = before
        raw_messages: List[discord.Message] = []
        async for msg in channel.history(**kwargs):
            raw_messages.append(msg)
        raw_messages.reverse()
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return []

    client_id = client_user.id if client_user else None

    valid_messages = [
        msg
        for msg in raw_messages
        if not (exclude_message_id and msg.id == exclude_message_id)
        and not (after_timestamp and msg.created_at <= after_timestamp)
    ]

    msg_image_parts: Dict[int, List[Dict[str, Any]]] = {}
    if max_images > 0:
        remaining = max_images
        for msg in reversed(valid_messages):
            if remaining <= 0:
                break
            if client_id and msg.author.id == client_id:
                continue
            if any(
                is_image_attachment(attachment) for attachment in msg.attachments
            ) or extract_image_urls_from_text_and_embeds(msg.content, msg.embeds):
                loaded = await load_images_from_message(
                    msg, max_images=remaining, max_size_bytes=max_size_bytes
                )
                if loaded:
                    msg_image_parts[msg.id] = loaded
                    remaining -= len(loaded)

    raw_turns: List[Dict[str, Any]] = []

    for msg in valid_messages:
        text = extract_message_text(msg)
        images = msg_image_parts.get(msg.id, [])
        if not text and not images:
            continue

        if client_id and msg.author.id == client_id:
            if text == "*Thinking...*" or text.startswith("*Thinking"):
                continue
            raw_turns.append({"role": "assistant", "content": text})
            continue

        cleaned = clean_prompt(text, client_user)
        if not cleaned and not images:
            continue
        user_text = (
            f"{msg.author.display_name}: {cleaned}"
            if cleaned
            else f"{msg.author.display_name}"
        )
        turn_content = format_turn_content(user_text, images)
        raw_turns.append({"role": "user", "content": turn_content})

    while raw_turns and raw_turns[0]["role"] == "assistant":
        raw_turns.pop(0)

    merged_turns: List[Dict[str, Any]] = []
    for turn in raw_turns:
        if merged_turns and merged_turns[-1]["role"] == turn["role"]:
            merged_turns[-1]["content"] = merge_turn_contents(
                merged_turns[-1]["content"], turn["content"]
            )
            continue
        merged_turns.append(turn)

    return merged_turns
