from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional, Union
import discord
from bot.agent.loop import AgenticLoop
from bot.discord.messenger import DiscordMessenger, split_content
from bot.discord.streamer import MessageStreamer
from bot.memory.channel import ChannelHistory
from bot.memory.prompt import build_system_prompt
from bot.tools.base import ToolContext
from config import settings


def should_respond(
    message: discord.Message, client_user: discord.ClientUser
) -> bool:
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
        parts.append(f"[Attachment: {attachment.url}]")
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
            fetched_channel = message.guild.get_channel(
                message.reference.channel_id
            )
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
) -> List[Dict[str, str]]:
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

    raw_turns: List[Dict[str, str]] = []
    client_id = client_user.id if client_user else None

    for msg in raw_messages:
        if exclude_message_id and msg.id == exclude_message_id:
            continue
        if after_timestamp and msg.created_at <= after_timestamp:
            continue

        text = extract_message_text(msg)
        if not text:
            continue

        if client_id and msg.author.id == client_id:
            if text == "*Thinking...*" or text.startswith("*Thinking"):
                continue
            raw_turns.append({"role": "assistant", "content": text})
        else:
            cleaned = clean_prompt(text, client_user)
            if not cleaned:
                continue
            raw_turns.append(
                {"role": "user", "content": f"{msg.author.display_name}: {cleaned}"}
            )

    while raw_turns and raw_turns[0]["role"] == "assistant":
        raw_turns.pop(0)

    merged_turns: List[Dict[str, str]] = []
    for turn in raw_turns:
        if merged_turns and merged_turns[-1]["role"] == turn["role"]:
            merged_turns[-1]["content"] += "\n" + turn["content"]
        else:
            merged_turns.append(turn)

    return merged_turns


async def _execute_chat_pipeline(
    user: Union[discord.User, discord.Member],
    channel: discord.abc.Messageable,
    guild: Optional[discord.Guild],
    client_user: Optional[discord.ClientUser],
    prompt: str,
    streamer: Optional[MessageStreamer],
    agent_loop: AgenticLoop,
    channel_history: ChannelHistory,
    on_status: Callable[[str], Awaitable[None]],
    on_error: Callable[[str], Awaitable[None]],
    on_fallback_send: Optional[Callable[[List[str]], Awaitable[None]]] = None,
    before_message: Optional[discord.Message] = None,
    exclude_message_id: Optional[int] = None,
) -> bool:
    channel_id = getattr(channel, "id", user.id)
    sys_prompt = build_system_prompt(
        settings.ai_system_prompt_path,
        user,
        channel,
        guild,
        tool_registry=agent_loop.tool_registry,
    )

    history_turns: List[Dict[str, str]] = []
    if isinstance(channel, discord.abc.Messageable):
        history_turns = await get_channel_context_messages(
            channel=channel,
            client_user=client_user,
            limit=settings.ai_channel_history_limit,
            before=before_message,
            after_timestamp=channel_history.get_cleared_at(channel_id),
            exclude_message_id=exclude_message_id,
        )

    if not history_turns:
        history_turns = channel_history.get_history(channel_id)

    current_user_content = f"{user.display_name}: {prompt}"
    all_turns = list(history_turns)
    if all_turns and all_turns[-1]["role"] == "user":
        all_turns[-1]["content"] += "\n" + current_user_content
    else:
        all_turns.append({"role": "user", "content": current_user_content})

    messages = [{"role": "system", "content": sys_prompt}] + all_turns

    context = ToolContext(
        user_id=user.id,
        user_name=user.name,
        channel_id=channel_id,
        guild_id=guild.id if guild else None,
    )

    on_chunk = streamer.feed if streamer else None

    try:
        answer = await agent_loop.run(
            messages, context, on_status=on_status, on_chunk=on_chunk
        )
        if streamer:
            await streamer.finalize(answer)
        elif on_fallback_send:
            chunks = split_content(answer)
            await on_fallback_send(chunks)

        channel_history.add_turn(channel_id, "user", current_user_content)
        channel_history.add_turn(channel_id, "assistant", answer)
        return True
    except Exception as e:
        if streamer:
            await streamer.stop()
        await on_error(str(e))
        return False


async def handle_message_event(
    message: discord.Message,
    client_user: discord.ClientUser,
    agent_loop: AgenticLoop,
    channel_history: ChannelHistory,
) -> None:
    if not should_respond(message, client_user):
        return

    is_reply = bool(message.reference and message.reference.message_id)
    ref_message = await get_referenced_message(message) if is_reply else None

    prompt = clean_prompt(message.content, client_user)
    if message.attachments:
        att_urls = [f"[Attachment: {a.url}]" for a in message.attachments]
        prompt = (prompt + "\n" if prompt else "") + "\n".join(att_urls)

    if not prompt:
        if is_reply and ref_message:
            prompt = "Please respond to the referenced message."
        else:
            return

    if is_reply and ref_message:
        ref_text = clean_prompt(extract_message_text(ref_message), client_user)
        if len(ref_text) > 300:
            ref_text = ref_text[:297] + "..."
        speaker = (
            "Assistant"
            if ref_message.author.id == client_user.id
            else ref_message.author.display_name
        )
        prompt = f'(Replying to {speaker}: "{ref_text}")\n{prompt}'

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
        streamer=streamer,
        agent_loop=agent_loop,
        channel_history=channel_history,
        on_status=status_callback,
        on_error=on_error,
        on_fallback_send=on_fallback_send,
        before_message=message,
        exclude_message_id=status_msg.id,
    )

    if success:
        await DiscordMessenger.safe_remove_reaction(message, "⏳", client_user)
        await DiscordMessenger.safe_react(message, "✅")


async def handle_chat_command(
    interaction: discord.Interaction,
    prompt: str,
    agent_loop: AgenticLoop,
    channel_history: ChannelHistory,
) -> None:
    await interaction.response.defer()

    channel = interaction.channel or interaction.user
    client_user = interaction.client.user if interaction.client else None

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
        except Exception:
            pass

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
        streamer=streamer,
        agent_loop=agent_loop,
        channel_history=channel_history,
        on_status=status_callback,
        on_error=on_error,
        on_fallback_send=on_fallback_send,
    )
