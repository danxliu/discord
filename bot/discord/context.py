from typing import Any

import discord

from bot.discord.attachments import attachment_to_text
from bot.discord.image import (
    format_turn_content,
    is_image_attachment,
    is_video_attachment,
    load_media_from_message,
)


def clean_prompt(content: str, client_user: discord.ClientUser | None = None) -> str:
    if not content:
        return ""
    cleaned = content
    if client_user:
        cleaned = cleaned.replace(f"<@{client_user.id}>", "")
        cleaned = cleaned.replace(f"<@!{client_user.id}>", "")
    return cleaned.strip()


def extract_message_text(msg: discord.Message, include_attachments: bool = True) -> str:
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
    if include_attachments:
        for attachment in msg.attachments:
            if is_video_attachment(attachment):
                parts.append(f"[Video: {attachment.filename}]")
            elif is_image_attachment(attachment):
                parts.append(f"[Image: {attachment.filename}]")
            else:
                parts.append(f"[Attachment: {attachment.filename} ({attachment.url})]")
    return "\n\n".join(parts).strip()


def _reaction_summary(msg: discord.Message) -> str:
    reactions = getattr(msg, "reactions", None) or []
    if not reactions:
        return ""
    summary = ", ".join(f"{reaction.emoji} {reaction.count}" for reaction in reactions)
    return f"Reactions: {summary}"


async def message_to_turn(
    msg: discord.Message,
    client_user: discord.ClientUser | None,
    *,
    text_override: str | None = None,
    seen_urls: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Convert one Discord message to a role-tagged model turn with its media."""
    text = clean_prompt(
        text_override
        if text_override is not None
        else extract_message_text(msg, include_attachments=False),
        client_user,
    )
    attachment_parts: list[str] = []
    for attachment in msg.attachments:
        if is_video_attachment(attachment):
            attachment_parts.append(f"[Video attachment: {attachment.filename}]")
        elif is_image_attachment(attachment):
            attachment_parts.append(f"[Image attachment: {attachment.filename}]")
        else:
            try:
                extracted = await attachment_to_text(attachment)
            except Exception:
                extracted = f"Could not read attachment {attachment.filename}."
            attachment_parts.append(
                f"[Attachment: {attachment.filename} ({attachment.url})]\n{extracted}"
            )

    details = [
        item for item in [text, *attachment_parts, _reaction_summary(msg)] if item
    ]
    body = "\n\n".join(details)
    images, fallbacks = await load_media_from_message(msg, seen_urls=seen_urls)
    is_assistant = bool(client_user and msg.author.id == client_user.id)
    if is_assistant:
        role = "assistant"
        turn_text = body
    else:
        role = "user"
        author_label = f"[{msg.author.display_name} (@{msg.author.name})]"
        turn_text = f"{author_label}: {body}" if body else author_label
    return {"role": role, "content": format_turn_content(turn_text, images)}, fallbacks
