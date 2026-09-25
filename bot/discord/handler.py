from datetime import datetime
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from uuid import uuid4

import discord
from bot.agent.loop import AgenticLoop
from bot.discord.image import (
    attachment_to_image_part,
    convert_messages_to_text_only,
    extract_image_urls_from_text_and_embeds,
    format_turn_content,
    is_image_attachment,
    is_vision_unsupported_error,
    load_images_from_message,
    merge_turn_contents,
    to_text_summary,
    url_to_image_part,
)
from bot.discord.messenger import DiscordMessenger, split_content
from bot.discord.streamer import MessageStreamer
from bot.memory.channel import ChannelHistory
from bot.memory.prompt import build_system_prompt
from bot.tools.base import ToolContext
from config import settings

logger = logging.getLogger(__name__)


def should_respond(message: discord.Message, client_user: discord.ClientUser) -> bool:
    if message.author.bot:
        return False
    if isinstance(message.channel, discord.DMChannel):
        return True
    if client_user in message.mentions:
        return True
    if message.reference:
        ref = message.reference.resolved or message.reference.cached_message
        if isinstance(ref, discord.Message) and ref.author.id == client_user.id:
            return True
    return False


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


async def get_referenced_message(
    message: discord.Message,
) -> discord.Message | None:
    if not message.reference or not message.reference.message_id:
        return None

    ref = message.reference.resolved or message.reference.cached_message
    if isinstance(ref, discord.Message):
        return ref

    try:
        channel = message.channel
        if (
            message.reference.channel_id
            and message.reference.channel_id != message.channel.id
            and message.guild
        ):
            fetched_channel = message.guild.get_channel(message.reference.channel_id)
            if isinstance(fetched_channel, discord.abc.Messageable):
                channel = fetched_channel
        fetched = await channel.fetch_message(message.reference.message_id)
        if isinstance(fetched, discord.Message):
            return fetched
    except (discord.NotFound, discord.HTTPException, discord.Forbidden):
        return None

    return None


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
                is_image_attachment(a) for a in msg.attachments
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
        else:
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
        else:
            merged_turns.append(turn)

    return merged_turns


async def _execute_chat_pipeline(
    user: Union[discord.User, discord.Member],
    channel: discord.abc.Messageable,
    guild: Optional[discord.Guild],
    client_user: Optional[discord.ClientUser],
    prompt: str,
    request_id: str,
    streamer: Optional[MessageStreamer],
    agent_loop: AgenticLoop,
    channel_history: ChannelHistory,
    on_status: Callable[[str], Awaitable[None]],
    on_error: Callable[[str], Awaitable[None]],
    on_fallback_send: Optional[Callable[[List[str]], Awaitable[None]]] = None,
    before_message: Optional[discord.Message] = None,
    exclude_message_id: Optional[int] = None,
    image_parts: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    channel_id = getattr(channel, "id", user.id)
    sys_prompt = build_system_prompt(
        settings.ai_system_prompt_path,
        user,
        channel,
        guild,
        tool_registry=agent_loop.tool_registry,
    )

    max_images = getattr(settings, "ai_max_context_images", 5)
    max_size_bytes = getattr(settings, "ai_max_image_size_mb", 20) * 1024 * 1024
    history_image_budget = max(0, max_images - len(image_parts or []))

    history_turns: List[Dict[str, Any]] = []
    if isinstance(channel, discord.abc.Messageable):
        history_turns = await get_channel_context_messages(
            channel=channel,
            client_user=client_user,
            limit=settings.ai_channel_history_limit,
            before=before_message,
            after_timestamp=channel_history.get_cleared_at(channel_id),
            exclude_message_id=exclude_message_id,
            max_images=history_image_budget,
            max_size_bytes=max_size_bytes,
        )

    if not history_turns:
        history_turns = channel_history.get_history(channel_id)

    current_user_text = f"{user.display_name}: {prompt}"
    current_turn_content = format_turn_content(current_user_text, image_parts)

    all_turns = list(history_turns)
    if all_turns and all_turns[-1]["role"] == "user":
        all_turns[-1]["content"] = merge_turn_contents(
            all_turns[-1]["content"], current_turn_content
        )
    else:
        all_turns.append({"role": "user", "content": current_turn_content})

    messages = [{"role": "system", "content": sys_prompt}] + all_turns

    context = ToolContext(
        user_id=user.id,
        user_name=user.name,
        channel_id=channel_id,
        guild_id=guild.id if guild else None,
        request_id=request_id,
    )

    on_chunk = streamer.feed if streamer else None

    has_images = any(isinstance(message.get("content"), list) for message in messages)
    try:
        answer = await agent_loop.run(
            messages, context, on_status=on_status, on_chunk=on_chunk
        )
    except Exception as error:
        logger.exception("Agent request failed request_id=%s", request_id)
        err_msg = str(error)
        if not has_images or not is_vision_unsupported_error(err_msg):
            if streamer:
                await streamer.stop()
            if is_vision_unsupported_error(err_msg):
                err_msg += (
                    f"\nNote: The configured model ({settings.ai_model}) may not "
                    "support multimodal/image inputs."
                )
            await on_error(err_msg)
            return False

        try:
            await on_status("Model does not support images; retrying as text...")
            text_only_messages = convert_messages_to_text_only(messages)
            answer = await agent_loop.run(
                text_only_messages, context, on_status=on_status, on_chunk=on_chunk
            )
        except Exception as retry_error:
            logger.exception("Text-only retry failed request_id=%s", request_id)
            if streamer:
                await streamer.stop()
            await on_error(f"{err_msg}\n(Retry as text also failed: {retry_error})")
            return False

        note = (
            f"-# *Note: `{settings.ai_model}` does not support image analysis; "
            "responded to text only.*"
        )
        answer = f"{answer}\n\n{note}".strip()

    if streamer:
        await streamer.finalize(answer)
    elif on_fallback_send:
        await on_fallback_send(split_content(answer))
    logger.info(
        "Response handling finished request_id=%s response_chars=%d streaming=%s",
        request_id,
        len(answer),
        streamer is not None,
    )

    channel_history.add_turn(channel_id, "user", to_text_summary(current_turn_content))
    channel_history.add_turn(channel_id, "assistant", answer)
    return True


async def handle_message_event(
    message: discord.Message,
    client_user: discord.ClientUser,
    agent_loop: AgenticLoop,
    channel_history: ChannelHistory,
) -> None:
    if not should_respond(message, client_user):
        return

    request_id = uuid4().hex[:12]
    logger.info(
        "Discord message received request_id=%s user_id=%s channel_id=%s guild_id=%s content_chars=%d attachments=%d",
        request_id,
        message.author.id,
        message.channel.id,
        message.guild.id if message.guild else None,
        len(message.content),
        len(message.attachments),
    )
    is_reply = bool(message.reference and message.reference.message_id)
    ref_message = await get_referenced_message(message) if is_reply else None

    prompt = clean_prompt(message.content, client_user)

    max_images = getattr(settings, "ai_max_context_images", 5)
    max_size_bytes = getattr(settings, "ai_max_image_size_mb", 20) * 1024 * 1024

    for attachment in message.attachments:
        if is_image_attachment(attachment):
            attachment_text = f"[Image: {attachment.filename}]"
        else:
            attachment_text = f"[Attachment: {attachment.filename} ({attachment.url})]"
        prompt = f"{prompt}\n{attachment_text}".strip()

    if is_reply and ref_message:
        ref_text = clean_prompt(extract_message_text(ref_message), client_user)
        if len(ref_text) > 300:
            ref_text = ref_text[:297] + "..."
        speaker = (
            "Assistant"
            if ref_message.author.id == client_user.id
            else ref_message.author.display_name
        )
        prompt = f'(Replying to {speaker}: "{ref_text}")\n{prompt}'.strip()
    seen_urls: set[str] = set()
    current_image_parts = await load_images_from_message(
        message,
        max_images=max_images,
        max_size_bytes=max_size_bytes,
        seen_urls=seen_urls,
    )
    if ref_message and len(current_image_parts) < max_images:
        current_image_parts.extend(
            await load_images_from_message(
                ref_message,
                max_images=max_images - len(current_image_parts),
                max_size_bytes=max_size_bytes,
                seen_urls=seen_urls,
            )
        )

    if not prompt.strip():
        if current_image_parts:
            prompt = (
                "Please analyze and describe this image."
                if len(current_image_parts) == 1
                else "Please analyze and describe these images."
            )
        elif is_reply and ref_message:
            prompt = "Please respond to the referenced message."
        else:
            return

    logger.debug(
        "Discord message request prepared request_id=%s user_id=%s channel_id=%s guild_id=%s prompt_chars=%d attachments=%d images=%d",
        request_id,
        message.author.id,
        message.channel.id,
        message.guild.id if message.guild else None,
        len(prompt),
        len(message.attachments),
        len(current_image_parts),
    )
    await DiscordMessenger.safe_react(message, "⏳")
    status_msg = await DiscordMessenger.safe_send(
        message.channel, "*Thinking...*", reply_to=message
    )
    if not status_msg:
        await DiscordMessenger.safe_remove_reaction(message, "⏳", client_user)
        return

    streamer = (
        MessageStreamer(
            target_message=status_msg,
            channel=message.channel,
            interval=settings.ai_stream_interval,
        )
        if settings.ai_stream_response
        else None
    )

    async def fallback_status(text: str) -> None:
        await DiscordMessenger.safe_edit(status_msg, f"*{text}*")

    async def on_error(err: str) -> None:
        await DiscordMessenger.safe_edit(status_msg, f"❌ Error: {err}")
        await DiscordMessenger.safe_remove_reaction(message, "⏳", client_user)
        await DiscordMessenger.safe_react(message, "❌")

    async def on_fallback_send(chunks: List[str]) -> None:
        await DiscordMessenger.safe_edit(status_msg, chunks[0])
        for chunk in chunks[1:]:
            await DiscordMessenger.safe_send(message.channel, chunk)

    status_callback = streamer.set_status if streamer else fallback_status

    success = await _execute_chat_pipeline(
        user=message.author,
        channel=message.channel,
        guild=message.guild,
        client_user=client_user,
        prompt=prompt,
        request_id=request_id,
        streamer=streamer,
        agent_loop=agent_loop,
        channel_history=channel_history,
        on_status=status_callback,
        on_error=on_error,
        on_fallback_send=on_fallback_send,
        before_message=message,
        exclude_message_id=status_msg.id,
        image_parts=current_image_parts,
    )

    if success:
        await DiscordMessenger.safe_remove_reaction(message, "⏳", client_user)
        await DiscordMessenger.safe_react(message, "✅")


async def handle_chat_command(
    interaction: discord.Interaction,
    prompt: str,
    agent_loop: AgenticLoop,
    channel_history: ChannelHistory,
    image: Optional[discord.Attachment] = None,
) -> None:
    request_id = uuid4().hex[:12]
    await interaction.response.defer()
    logger.info(
        "Slash chat request received request_id=%s user_id=%s channel_id=%s guild_id=%s prompt_chars=%d image_attached=%s",
        request_id,
        interaction.user.id,
        interaction.channel_id,
        interaction.guild_id,
        len(prompt),
        image is not None,
    )

    channel = interaction.channel or interaction.user
    client_user = interaction.client.user if interaction.client else None

    max_images = getattr(settings, "ai_max_context_images", 5)
    max_size_bytes = getattr(settings, "ai_max_image_size_mb", 20) * 1024 * 1024

    current_image_parts: List[Dict[str, Any]] = []
    if image:
        if is_image_attachment(image):
            part = await attachment_to_image_part(image, max_size_bytes=max_size_bytes)
            if part:
                current_image_parts.append(part)
                prompt = f"{prompt}\n[Image: {image.filename}]".strip()
            else:
                prompt = (
                    f"{prompt}\n[Attachment: {image.filename} (could not load image)]"
                ).strip()
        else:
            prompt = f"{prompt}\n[Attachment: {image.filename} ({image.url})]".strip()

    remaining = max_images - len(current_image_parts)
    urls = extract_image_urls_from_text_and_embeds(prompt, [])[:remaining]
    for url in urls:
        part = await url_to_image_part(url, max_size_bytes=max_size_bytes)
        if part:
            current_image_parts.append(part)

    if not prompt or not prompt.strip():
        if current_image_parts:
            prompt = "Please analyze and describe this image."
        else:
            prompt = "Hello!"

    logger.debug(
        "Slash chat request prepared request_id=%s user_id=%s channel_id=%s guild_id=%s prompt_chars=%d images=%d",
        request_id,
        interaction.user.id,
        interaction.channel_id,
        interaction.guild_id,
        len(prompt),
        len(current_image_parts),
    )

    streamer = (
        MessageStreamer(
            interaction=interaction,
            channel=channel if isinstance(channel, discord.abc.Messageable) else None,
            interval=settings.ai_stream_interval,
        )
        if settings.ai_stream_response
        else None
    )

    async def on_status(text: str) -> None:
        try:
            await interaction.edit_original_response(content=f"*{text}*")
        except discord.HTTPException:
            logger.warning("Could not update chat status request_id=%s", request_id)
        except Exception:
            logger.exception("Unexpected chat status update failure request_id=%s", request_id)

    async def on_error(err: str) -> None:
        await interaction.edit_original_response(content=f"❌ Error: {err}")

    async def on_fallback_send(chunks: List[str]) -> None:
        await interaction.edit_original_response(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    status_callback = streamer.set_status if streamer else on_status
    await status_callback("Thinking...")

    await _execute_chat_pipeline(
        user=interaction.user,
        channel=channel,
        guild=interaction.guild,
        client_user=client_user,
        prompt=prompt,
        request_id=request_id,
        streamer=streamer,
        agent_loop=agent_loop,
        channel_history=channel_history,
        on_status=status_callback,
        on_error=on_error,
        on_fallback_send=on_fallback_send,
        image_parts=current_image_parts,
    )
