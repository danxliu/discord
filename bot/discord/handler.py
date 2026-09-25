from datetime import datetime
from typing import Dict, List, Optional
import discord
from bot.agent.loop import AgenticLoop
from bot.discord.messenger import DiscordMessenger, split_content
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
        if prompt:
            prompt += "\n" + "\n".join(att_urls)
        else:
            prompt = "\n".join(att_urls)

    if not prompt:
        if is_reply and ref_message:
            prompt = "Please respond to the referenced message."
        else:
            return

    await DiscordMessenger.safe_react(message, "⏳")
    status_msg = await DiscordMessenger.safe_send(
        message.channel, "*Thinking...*", reply_to=message
    )
    if not status_msg:
        await DiscordMessenger.safe_remove_reaction(message, "⏳", client_user)
        return

    async def on_status(text: str) -> None:
        await DiscordMessenger.safe_edit(status_msg, f"*{text}*")

    sys_prompt = build_system_prompt(
        settings.ai_system_prompt_path,
        message.author,
        message.channel,
        message.guild,
        tool_registry=agent_loop.tool_registry,
    )

    history_turns: List[Dict[str, str]] = []
    if isinstance(message.channel, discord.abc.Messageable):
        history_turns = await get_channel_context_messages(
            channel=message.channel,
            client_user=client_user,
            limit=settings.ai_channel_history_limit,
            before=message,
            after_timestamp=channel_history.get_cleared_at(message.channel.id),
            exclude_message_id=status_msg.id if status_msg else None,
        )

    if not history_turns:
        history_turns = channel_history.get_history(message.channel.id)

    current_prompt = prompt
    if is_reply and ref_message:
        ref_text = clean_prompt(extract_message_text(ref_message), client_user)
        if len(ref_text) > 300:
            ref_text = ref_text[:297] + "..."
        if ref_message.author.id == client_user.id:
            reply_prefix = f"(Replying to Assistant: \"{ref_text}\")\n"
        else:
            reply_prefix = f"(Replying to {ref_message.author.display_name}: \"{ref_text}\")\n"
        current_prompt = reply_prefix + current_prompt

    current_user_content = f"{message.author.display_name}: {current_prompt}"
    all_turns = list(history_turns)
    if all_turns and all_turns[-1]["role"] == "user":
        all_turns[-1]["content"] += "\n" + current_user_content
    else:
        all_turns.append({"role": "user", "content": current_user_content})

    messages = [{"role": "system", "content": sys_prompt}] + all_turns

    context = ToolContext(
        user_id=message.author.id,
        user_name=message.author.name,
        channel_id=message.channel.id,
        guild_id=message.guild.id if message.guild else None,
    )

    try:
        answer = await agent_loop.run(messages, context, on_status=on_status)
        chunks = split_content(answer)
        await DiscordMessenger.safe_edit(status_msg, chunks[0])
        for chunk in chunks[1:]:
            await DiscordMessenger.safe_send(message.channel, chunk)

        channel_history.add_turn(message.channel.id, "user", current_user_content)
        channel_history.add_turn(message.channel.id, "assistant", answer)

        await DiscordMessenger.safe_remove_reaction(message, "⏳", client_user)
        await DiscordMessenger.safe_react(message, "✅")
    except Exception as e:
        await DiscordMessenger.safe_edit(status_msg, f"❌ Error: {str(e)}")
        await DiscordMessenger.safe_remove_reaction(message, "⏳", client_user)
        await DiscordMessenger.safe_react(message, "❌")


async def handle_chat_command(
    interaction: discord.Interaction,
    prompt: str,
    agent_loop: AgenticLoop,
    channel_history: ChannelHistory,
) -> None:
    await interaction.response.defer()

    async def on_status(text: str) -> None:
        try:
            await interaction.edit_original_response(content=f"*{text}*")
        except Exception:
            pass

    await on_status("Thinking...")

    channel = interaction.channel or interaction.user
    channel_id = interaction.channel_id or interaction.user.id
    client_user = interaction.client.user if interaction.client else None
    sys_prompt = build_system_prompt(
        settings.ai_system_prompt_path,
        interaction.user,
        channel,
        interaction.guild,
        tool_registry=agent_loop.tool_registry,
    )

    history_turns: List[Dict[str, str]] = []
    if isinstance(channel, discord.abc.Messageable):
        history_turns = await get_channel_context_messages(
            channel=channel,
            client_user=client_user,
            limit=settings.ai_channel_history_limit,
            after_timestamp=channel_history.get_cleared_at(channel_id),
        )

    if not history_turns:
        history_turns = channel_history.get_history(channel_id)

    current_user_content = f"{interaction.user.display_name}: {prompt}"
    all_turns = list(history_turns)
    if all_turns and all_turns[-1]["role"] == "user":
        all_turns[-1]["content"] += "\n" + current_user_content
    else:
        all_turns.append({"role": "user", "content": current_user_content})

    messages = [{"role": "system", "content": sys_prompt}] + all_turns

    context = ToolContext(
        user_id=interaction.user.id,
        user_name=interaction.user.name,
        channel_id=channel_id,
        guild_id=interaction.guild_id,
    )

    try:
        answer = await agent_loop.run(messages, context, on_status=on_status)
        chunks = split_content(answer)
        await interaction.edit_original_response(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

        channel_history.add_turn(channel_id, "user", current_user_content)
        channel_history.add_turn(channel_id, "assistant", answer)
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Error: {str(e)}")
