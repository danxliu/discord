from typing import Any

import discord

from bot.discord.attachments import attachment_to_text
from bot.discord.image import (
    extract_image_urls_from_text_and_embeds,
    extract_video_urls_from_text_and_embeds,
    format_turn_content,
    is_image_attachment,
    is_video_attachment,
    load_media_from_message,
    merge_turn_contents,
)


def clean_prompt(content: str, client_user: discord.ClientUser | None = None) -> str:
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
        if is_video_attachment(attachment):
            parts.append(f"[Video: {attachment.filename}]")
        elif is_image_attachment(attachment):
            parts.append(f"[Image: {attachment.filename}]")
        else:
            parts.append(f"[Attachment: {attachment.filename} ({attachment.url})]")
    return "\n\n".join(parts).strip()


async def get_channel_context_messages(
    channel: discord.abc.Messageable,
    client_user: discord.ClientUser | None,
    limit: int = 10,
    before: discord.Message | None = None,
    exclude_message_id: int | None = None,
    video_fallbacks: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0 or not hasattr(channel, "history"):
        return []

    try:
        kwargs: dict[str, object] = {"limit": limit}
        if before:
            kwargs["before"] = before
        raw_messages: list[discord.Message] = []
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
    ]

    msg_image_parts: dict[int, list[dict[str, Any]]] = {}
    for msg in reversed(valid_messages):
        if client_id and msg.author.id == client_id:
            continue
        if any(
            is_image_attachment(attachment) or is_video_attachment(attachment)
            for attachment in msg.attachments
        ) or extract_image_urls_from_text_and_embeds(
            msg.content, msg.embeds
        ) or extract_video_urls_from_text_and_embeds(msg.content, msg.embeds):
            loaded, fallbacks = await load_media_from_message(msg)
            if loaded:
                msg_image_parts[msg.id] = loaded
            if video_fallbacks is not None:
                video_fallbacks.update(fallbacks)

    raw_turns: list[dict[str, Any]] = []

    for msg in valid_messages:
        text = extract_message_text(msg)
        images = msg_image_parts.get(msg.id, [])

        if client_id and msg.author.id == client_id:
            if text == "*Thinking...*" or text.startswith("*Thinking"):
                continue
            raw_turns.append({"role": "assistant", "content": text})
            continue

        for attachment in msg.attachments:
            if is_image_attachment(attachment) or is_video_attachment(attachment):
                continue
            attachment_content = await attachment_to_text(attachment)
            text = (
                f"{text}\n\nExtracted attachment {attachment.filename}:\n"
                f"{attachment_content}"
            ).strip()

        if not text and not images:
            continue
        cleaned = clean_prompt(text, client_user)
        if not cleaned and not images:
            continue

        # Include reply context in history if this message was a reply
        reply_prefix = ""
        if msg.reference:
            ref_msg = (
                msg.reference.resolved
                if isinstance(msg.reference.resolved, discord.Message)
                else msg.reference.cached_message
            )
            if isinstance(ref_msg, discord.Message):
                ref_speaker = (
                    "Assistant"
                    if client_id and ref_msg.author.id == client_id
                    else f"{ref_msg.author.display_name} (@{ref_msg.author.name})"
                )
                ref_snippet = clean_prompt(extract_message_text(ref_msg), client_user)
                if len(ref_snippet) > 150:
                    ref_snippet = ref_snippet[:147] + "..."
                if ref_snippet:
                    reply_prefix = f'(Replying to {ref_speaker}: "{ref_snippet}")\n'

        if reply_prefix:
            cleaned = f"{reply_prefix}{cleaned}".strip()

        author_label = f"[{msg.author.display_name} (@{msg.author.name})]"
        user_text = f"{author_label}: {cleaned}" if cleaned else author_label
        turn_content = format_turn_content(user_text, images)
        raw_turns.append({"role": "user", "content": turn_content})

    while raw_turns and raw_turns[0]["role"] == "assistant":
        raw_turns.pop(0)

    merged_turns: list[dict[str, Any]] = []
    for turn in raw_turns:
        if merged_turns and merged_turns[-1]["role"] == turn["role"]:
            merged_turns[-1]["content"] = merge_turn_contents(
                merged_turns[-1]["content"], turn["content"]
            )
            continue
        merged_turns.append(turn)

    return merged_turns
