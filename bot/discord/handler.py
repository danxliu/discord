from typing import Optional
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


def clean_prompt(content: str, client_user: discord.ClientUser) -> str:
    cleaned = content.replace(f"<@{client_user.id}>", "")
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

    messages = [{"role": "system", "content": sys_prompt}]
    initial_role: str | None = None
    initial_content: str | None = None

    if is_reply:
        if ref_message:
            ref_text = extract_message_text(ref_message)
            if ref_message.author.id == client_user.id:
                initial_role = "assistant"
                initial_content = ref_text
            else:
                initial_role = "user"
                cleaned_ref_text = clean_prompt(ref_text, client_user)
                if ref_message.author.id != message.author.id:
                    initial_content = (
                        f"{ref_message.author.display_name}: {cleaned_ref_text}"
                    )
                else:
                    initial_content = cleaned_ref_text

            if initial_content:
                messages.append({"role": initial_role, "content": initial_content})
    else:
        messages.extend(channel_history.get_history(message.channel.id))

    messages.append({"role": "user", "content": prompt})

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

        if is_reply:
            channel_history.clear(message.channel.id)
            if initial_role and initial_content:
                channel_history.add_turn(
                    message.channel.id, initial_role, initial_content
                )
        channel_history.add_turn(message.channel.id, "user", prompt)
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
    sys_prompt = build_system_prompt(
        settings.ai_system_prompt_path,
        interaction.user,
        channel,
        interaction.guild,
        tool_registry=agent_loop.tool_registry,
    )
    history = channel_history.get_history(channel_id)
    messages = [{"role": "system", "content": sys_prompt}] + history + [
        {"role": "user", "content": prompt}
    ]

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

        channel_history.add_turn(channel_id, "user", prompt)
        channel_history.add_turn(channel_id, "assistant", answer)
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Error: {str(e)}")
