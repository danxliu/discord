import logging
from collections.abc import Awaitable, Callable
from io import BytesIO
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
    convert_messages_to_text_only,
    format_turn_content,
    is_image_attachment,
    is_vision_unsupported_error,
    load_images_from_message,
    merge_turn_contents,
)
from bot.discord.messenger import DiscordMessenger, split_content
from bot.discord.streamer import MessageStreamer
from bot.memory.prompt import build_system_prompt
from bot.tools.base import GeneratedImage, ToolContext
from config import settings

logger = logging.getLogger(__name__)


async def _deliver_generated_images(
    images: list[GeneratedImage],
    upload: Callable[[list[discord.File]], Awaitable[None]],
    warn: Callable[[str], Awaitable[None]],
    request_id: str,
) -> None:
    files: list[discord.File] = []
    try:
        files = [
            discord.File(BytesIO(image.data), filename=image.filename)
            for image in images
        ]
        await upload(files)
    except Exception:
        logger.exception("Generated image upload failed request_id=%s", request_id)
        try:
            await warn("I generated an image, but Discord could not attach it.")
        except Exception:
            logger.exception(
                "Generated image upload warning failed request_id=%s", request_id
            )
    finally:
        for file in files:
            file.close()


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
    generation_image_parts: list[dict[str, Any]] | None = None,
    on_generated_images: Callable[[list[GeneratedImage]], Awaitable[None]]
    | None = None,
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
    presence_status = getattr(user, "status", None)
    context = ToolContext(
        user_id=user.id,
        user_name=user.name,
        channel_id=channel_id,
        user_display_name=user.display_name,
        user_avatar_url=getattr(getattr(user, "display_avatar", None), "url", None),
        user_status=(
            getattr(presence_status, "name", str(presence_status))
            if presence_status is not None
            else None
        ),
        guild_id=guild.id if guild else None,
        triggering_message_id=before_message.id if before_message else None,
        request_id=request_id,
        image_count=image_count,
        current_image_parts=list(
            generation_image_parts
            if generation_image_parts is not None
            else image_parts or []
        ),
    )

    async def deliver_generated_images() -> None:
        if not context.generated_images or not on_generated_images:
            return
        try:
            await on_generated_images(context.generated_images)
        except Exception:
            logger.exception(
                "Generated image delivery failed request_id=%s", request_id
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
                await streamer.stop(delete_followups=bool(context.generated_images))
            if vision_unsupported:
                err_msg += (
                    f"\nNote: The configured model ({settings.ai_model}) may not "
                    "support multimodal/image inputs."
                )
            await on_error(err_msg)
            await deliver_generated_images()
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
                await streamer.stop(delete_followups=bool(context.generated_images))
            await on_error(f"{err_msg}\n(Retry as text also failed: {retry_error})")
            await deliver_generated_images()
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

    if context.generated_images and on_generated_images:
        if streamer:
            await streamer.stop(delete_followups=True)
    elif streamer:
        await streamer.finalize(answer)
    elif on_fallback_send:
        await on_fallback_send(split_content(answer))

    await deliver_generated_images()

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
    generation_image_parts = await load_images_from_message(
        message,
        seen_urls=seen_urls,
    )
    current_image_parts = list(generation_image_parts)
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
    status_msg = await DiscordMessenger.safe_send(
        message.channel, "*Thinking...*", reply_to=message
    )
    if not status_msg:
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

    async def on_fallback_send(chunks: list[str]) -> None:
        await DiscordMessenger.safe_edit(status_msg, chunks[0])
        for chunk in chunks[1:]:
            await DiscordMessenger.safe_send(message.channel, chunk)

    async def on_generated_images(images: list[GeneratedImage]) -> None:
        await _deliver_generated_images(
            images,
            lambda files: status_msg.edit(content=None, attachments=files),
            lambda text: DiscordMessenger.safe_edit(status_msg, text),
            request_id,
        )

    status_callback = streamer.set_status if streamer else fallback_status

    await _execute_chat_pipeline(
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
        generation_image_parts=generation_image_parts,
        on_generated_images=on_generated_images,
    )

