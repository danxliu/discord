import logging
from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import Any
from uuid import uuid4

import discord

from bot.agent.loop import AgenticLoop
from bot.discord.context import clean_prompt, extract_message_text, message_to_turn
from bot.discord.image import convert_messages_to_text_only, is_vision_unsupported_error
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


async def _run_agent_with_image_fallback(
    agent_loop: AgenticLoop,
    messages: list[dict[str, Any]],
    context: ToolContext,
    on_status: Callable[[str], Awaitable[None]],
    on_error: Callable[[str], Awaitable[None]],
    on_chunk: Callable[[str], Awaitable[None]] | None,
    streamer: MessageStreamer | None,
    request_id: str,
    deliver_generated_images: Callable[[], Awaitable[None]],
) -> str | None:
    has_images = any(isinstance(message.get("content"), list) for message in messages)
    try:
        return await agent_loop.run(
            messages, context, on_status=on_status, on_chunk=on_chunk
        )
    except Exception as error:
        error_text = str(error)
        vision_unsupported = is_vision_unsupported_error(error_text)
        empty_response = "provider returned an empty response" in error_text.lower()
        if has_images and (vision_unsupported or empty_response):
            logger.info(
                "Model rejected image input or provider returned empty response; retrying without images request_id=%s",
                request_id,
            )
        else:
            logger.exception("Agent request failed request_id=%s", request_id)
            if streamer:
                await streamer.stop(delete_followups=bool(context.generated_images))
            if vision_unsupported:
                error_text += (
                    f"\nNote: The configured model ({settings.ai_model}) may not "
                    "support multimodal/image inputs."
                )
            await on_error(error_text)
            await deliver_generated_images()
            return None

        try:
            status_text = (
                "Model does not support images; retrying as text..."
                if vision_unsupported
                else "Image processing failed on upstream provider; retrying as text..."
            )
            await on_status(status_text)
            text_only_messages = convert_messages_to_text_only(messages)
            answer = await agent_loop.run(
                text_only_messages,
                context,
                on_status=on_status,
                on_chunk=on_chunk,
            )
        except Exception as retry_error:
            logger.exception("Text-only retry failed request_id=%s", request_id)
            if streamer:
                await streamer.stop(delete_followups=bool(context.generated_images))
            await on_error(f"{error_text}\n(Retry as text also failed: {retry_error})")
            await deliver_generated_images()
            return None

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
        return f"{answer}\n\n{note}".strip()


def should_respond(message: discord.Message, client_user: discord.ClientUser) -> bool:
    if message.author.id == client_user.id:
        return False
    if isinstance(message.channel, discord.DMChannel):
        return not message.author.bot
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
    except (discord.NotFound, discord.HTTPException, discord.Forbidden, AttributeError):
        return None

    return None


async def _build_chat_request(
    user: discord.User | discord.Member,
    channel: discord.abc.Messageable,
    guild: discord.Guild | None,
    client_user: discord.ClientUser | None,
    request_id: str,
    agent_loop: AgenticLoop,
    context_turns: list[dict[str, Any]],
    triggering_message_id: int | None,
    generation_image_parts: list[dict[str, Any]] | None,
    video_fallbacks: dict[str, list[dict[str, Any]]] | None,
) -> tuple[list[dict[str, Any]], ToolContext]:
    channel_id = getattr(channel, "id", user.id)
    system_prompt = build_system_prompt(
        settings.ai_system_prompt_path,
        user,
        channel,
        guild,
        tool_registry=agent_loop.tool_registry,
    )
    turns = list(context_turns)

    messages = [{"role": "system", "content": system_prompt}, *turns]
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
        triggering_message_id=triggering_message_id,
        request_id=request_id,
        image_count=image_count,
        current_image_parts=list(generation_image_parts or []),
        video_fallbacks=video_fallbacks or {},
    )
    return messages, context


async def _deliver_chat_response(
    answer: str,
    streamer: MessageStreamer | None,
    on_fallback_send: Callable[[list[str]], Awaitable[None]] | None,
    deliver_generated_images: Callable[[], Awaitable[None]],
    request_id: str,
) -> None:
    if streamer:
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


async def _execute_chat_pipeline(
    user: discord.User | discord.Member,
    channel: discord.abc.Messageable,
    guild: discord.Guild | None,
    client_user: discord.ClientUser | None,
    request_id: str,
    streamer: MessageStreamer | None,
    agent_loop: AgenticLoop,
    on_status: Callable[[str], Awaitable[None]],
    on_error: Callable[[str], Awaitable[None]],
    on_fallback_send: Callable[[list[str]], Awaitable[None]] | None = None,
    context_turns: list[dict[str, Any]] | None = None,
    triggering_message_id: int | None = None,
    generation_image_parts: list[dict[str, Any]] | None = None,
    video_fallbacks: dict[str, list[dict[str, Any]]] | None = None,
    on_generated_images: Callable[[list[GeneratedImage]], Awaitable[None]]
    | None = None,
) -> bool:
    messages, context = await _build_chat_request(
        user,
        channel,
        guild,
        client_user,
        request_id,
        agent_loop,
        context_turns or [],
        triggering_message_id,
        generation_image_parts,
        video_fallbacks,
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

    answer = await _run_agent_with_image_fallback(
        agent_loop,
        messages,
        context,
        on_status,
        on_error,
        on_chunk,
        streamer,
        request_id,
        deliver_generated_images,
    )
    if answer is None:
        return False

    await _deliver_chat_response(
        answer,
        streamer,
        on_fallback_send,
        deliver_generated_images,
        request_id,
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
    is_mention_trigger = client_user in message.mentions
    ref_message = (
        await get_referenced_message(message)
        if is_reply and not is_mention_trigger
        else None
    )
    reply_context_message = (
        ref_message
        if not is_mention_trigger
        and ref_message
        and ref_message.author.id == client_user.id
        else None
    )

    prompt = clean_prompt(
        extract_message_text(message, include_attachments=False), client_user
    )
    has_user_prompt = bool(prompt)
    if not has_user_prompt and not message.attachments and not is_reply:
        return

    seen_urls: set[str] = set()
    video_fallbacks: dict[str, list[dict[str, Any]]] = {}
    context_turns: list[dict[str, Any]] = []
    if not has_user_prompt:
        if message.attachments:
            instruction = "Please analyze the attached image, GIF, video, or file."
            if len(message.attachments) > 1:
                instruction = (
                    "Please analyze the attached images, GIFs, videos, and files."
                )
            prompt = instruction
        elif is_reply:
            prompt = (
                "Please respond to the referenced message."
                if reply_context_message
                else "Please respond to this message."
            )

    current_turn, current_fallbacks = await message_to_turn(
        message,
        client_user,
        text_override=prompt,
        seen_urls=seen_urls,
    )
    video_fallbacks.update(current_fallbacks)
    if reply_context_message:
        reply_turn, reply_fallbacks = await message_to_turn(
            reply_context_message, client_user, seen_urls=seen_urls
        )
        context_turns.append(reply_turn)
        video_fallbacks.update(reply_fallbacks)
    context_turns.append(current_turn)
    current_media_parts = (
        current_turn["content"] if isinstance(current_turn["content"], list) else []
    )
    generation_image_parts = [
        part for part in current_media_parts if part.get("type") == "image_url"
    ]
    image_count = sum(part.get("type") == "image_url" for part in current_media_parts)

    logger.debug(
        "Discord message request prepared request_id=%s user_id=%s channel_id=%s guild_id=%s prompt_chars=%d attachments=%d images=%d",
        request_id,
        message.author.id,
        message.channel.id,
        message.guild.id if message.guild else None,
        len(prompt),
        len(message.attachments),
        image_count,
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
            lambda files: status_msg.edit(attachments=files),
            lambda text: DiscordMessenger.safe_send(message.channel, text),
            request_id,
        )

    status_callback = streamer.set_status if streamer else fallback_status

    await _execute_chat_pipeline(
        user=message.author,
        channel=message.channel,
        guild=message.guild,
        client_user=client_user,
        request_id=request_id,
        streamer=streamer,
        agent_loop=agent_loop,
        on_status=status_callback,
        on_error=on_error,
        on_fallback_send=on_fallback_send,
        context_turns=context_turns,
        triggering_message_id=message.id,
        generation_image_parts=generation_image_parts,
        video_fallbacks=video_fallbacks,
        on_generated_images=on_generated_images,
    )
