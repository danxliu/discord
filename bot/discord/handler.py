import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import discord

from bot.agent.loop import AgenticLoop
from bot.discord.attachments import attachment_to_text
from bot.discord.context import (
    clean_prompt,
    extract_message_text,
    get_channel_context_messages,
)
from bot.discord.image import (
    attachment_to_image_part,
    convert_messages_to_text_only,
    extract_image_urls_from_text_and_embeds,
    format_turn_content,
    is_image_attachment,
    is_vision_unsupported_error,
    load_images_from_message,
    merge_turn_contents,
    url_to_image_part,
)
from bot.discord.messenger import DiscordMessenger, split_content
from bot.discord.streamer import MessageStreamer
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


async def _execute_chat_pipeline(
    user: discord.User | discord.Member,
    channel: discord.abc.Messageable,
    guild: discord.Guild | None,
    client_user: discord.ClientUser | None,
    prompt: str,
    request_id: str,
    streamer: MessageStreamer | None,
    agent_loop: AgenticLoop,
    on_status: Callable[[str], Awaitable[None]],
    on_error: Callable[[str], Awaitable[None]],
    on_fallback_send: Callable[[list[str]], Awaitable[None]] | None = None,
    before_message: discord.Message | None = None,
    exclude_message_id: int | None = None,
    image_parts: list[dict[str, Any]] | None = None,
) -> bool:
    channel_id = getattr(channel, "id", user.id)
    sys_prompt = build_system_prompt(
        settings.ai_system_prompt_path,
        user,
        channel,
        guild,
        tool_registry=agent_loop.tool_registry,
    )

    history_turns: list[dict[str, Any]] = []
    if isinstance(channel, discord.abc.Messageable):
        history_turns = await get_channel_context_messages(
            channel=channel,
            client_user=client_user,
            limit=settings.ai_channel_history_limit,
            before=before_message,
            exclude_message_id=exclude_message_id,
        )

    current_author_label = f"[{user.display_name} (@{user.name})]"
    current_user_text = f"{current_author_label}: {prompt}"
    current_turn_content = format_turn_content(current_user_text, image_parts)

    all_turns = list(history_turns)
    if all_turns and all_turns[-1]["role"] == "user":
        all_turns[-1]["content"] = merge_turn_contents(
            all_turns[-1]["content"], current_turn_content
        )
    else:
        all_turns.append({"role": "user", "content": current_turn_content})

    messages = [{"role": "system", "content": sys_prompt}] + all_turns

    image_count = sum(
        part.get("type") == "image_url"
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
    )
    context = ToolContext(
        user_id=user.id,
        user_name=user.name,
        channel_id=channel_id,
        guild_id=guild.id if guild else None,
        request_id=request_id,
        image_count=image_count,
    )

    on_chunk = streamer.feed if streamer else None

    has_images = any(isinstance(message.get("content"), list) for message in messages)
    try:
        answer = await agent_loop.run(
            messages, context, on_status=on_status, on_chunk=on_chunk
        )
    except Exception as error:
        err_msg = str(error)
        vision_unsupported = is_vision_unsupported_error(err_msg)
        is_empty_response = "provider returned an empty response" in err_msg.lower()
        should_retry_text = has_images and (vision_unsupported or is_empty_response)
        if should_retry_text:
            logger.info(
                "Model rejected image input or provider returned empty response; retrying without images request_id=%s",
                request_id,
            )
        else:
            logger.exception("Agent request failed request_id=%s", request_id)
        if not should_retry_text:
            if streamer:
                await streamer.stop()
            if vision_unsupported:
                err_msg += (
                    f"\nNote: The configured model ({settings.ai_model}) may not "
                    "support multimodal/image inputs."
                )
            await on_error(err_msg)
            return False

        try:
            status_text = (
                "Model does not support images; retrying as text..."
                if vision_unsupported
                else "Image processing failed on upstream provider; retrying as text..."
            )
            await on_status(status_text)
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

        if vision_unsupported:
            note = (
                f"-# *Note: `{settings.ai_model}` does not support image analysis; "
                "responded to text only.*"
            )
        else:
            note = (
                f"-# *Note: Upstream provider returned an empty response for `{settings.ai_model}` "
                "when processing the image; responded to text only.*"
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

    return True


async def handle_message_event(
    message: discord.Message,
    client_user: discord.ClientUser,
    agent_loop: AgenticLoop,
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
    has_user_prompt = bool(prompt)

    attachment_notes = []
    for attachment in message.attachments:
        if is_image_attachment(attachment):
            attachment_notes.append(f"[Image: {attachment.filename}]")
        else:
            attachment_content = await attachment_to_text(attachment)
            attachment_notes.append(
                f"[Attachment: {attachment.filename} ({attachment.url})]\n"
                f"{attachment_content}"
            )
    if attachment_notes:
        prompt = "\n".join(part for part in [prompt, *attachment_notes] if part)

    if is_reply and ref_message:
        ref_text = clean_prompt(extract_message_text(ref_message), client_user)
        if len(ref_text) > 300:
            ref_text = ref_text[:297] + "..."
        speaker = (
            "Assistant"
            if client_user and ref_message.author.id == client_user.id
            else f"{ref_message.author.display_name} (@{ref_message.author.name})"
        )
        prompt = f'(Replying to {speaker}: "{ref_text}")\n{prompt}'.strip()
    seen_urls: set[str] = set()
    current_image_parts = await load_images_from_message(
        message,
        seen_urls=seen_urls,
    )
    if ref_message:
        current_image_parts.extend(
            await load_images_from_message(
                ref_message,
                seen_urls=seen_urls,
            )
        )

    if not has_user_prompt:
        if current_image_parts or attachment_notes:
            instruction = "Please analyze the attached image or file."
            if len(current_image_parts) > 1:
                instruction = "Please analyze the attached images and files."
            prompt = f"{instruction}\n{prompt}".strip()
        elif is_reply and ref_message:
            prompt = f"Please respond to the referenced message.\n{prompt}".strip()
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

    async def on_fallback_send(chunks: list[str]) -> None:
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
    image: discord.Attachment | None = None,
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

    has_user_prompt = bool(prompt.strip())
    current_image_parts: list[dict[str, Any]] = []
    if image:
        if is_image_attachment(image):
            part = await attachment_to_image_part(image)
            if part:
                current_image_parts.append(part)
                prompt = f"{prompt}\n[Image: {image.filename}]".strip()
            else:
                prompt = (
                    f"{prompt}\n[Attachment: {image.filename} (could not load image)]"
                ).strip()
        else:
            attachment_content = await attachment_to_text(image)
            prompt = (
                f"{prompt}\n[Attachment: {image.filename} ({image.url})]\n"
                f"{attachment_content}"
            ).strip()

    urls = extract_image_urls_from_text_and_embeds(prompt, [])
    for url in urls:
        part = await url_to_image_part(url)
        if part:
            current_image_parts.append(part)

    if not has_user_prompt:
        if current_image_parts or image:
            prompt = f"Please analyze the attached image or file.\n{prompt}".strip()
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
            logger.exception(
                "Unexpected chat status update failure request_id=%s", request_id
            )

    async def on_error(err: str) -> None:
        await interaction.edit_original_response(content=f"❌ Error: {err}")

    async def on_fallback_send(chunks: list[str]) -> None:
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
        on_status=status_callback,
        on_error=on_error,
        on_fallback_send=on_fallback_send,
        image_parts=current_image_parts,
    )
