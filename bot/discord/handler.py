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
    if message.reference and message.reference.resolved:
        ref = message.reference.resolved
        if isinstance(ref, discord.Message) and ref.author.id == client_user.id:
            return True
    return False


def clean_prompt(content: str, client_user: discord.ClientUser) -> str:
    cleaned = content.replace(f"<@{client_user.id}>", "")
    cleaned = cleaned.replace(f"<@!{client_user.id}>", "")
    return cleaned.strip()


async def handle_message_event(
    message: discord.Message,
    client_user: discord.ClientUser,
    agent_loop: AgenticLoop,
    channel_history: ChannelHistory,
) -> None:
    if not should_respond(message, client_user):
        return

    prompt = clean_prompt(message.content, client_user)
    if not prompt:
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
    history = channel_history.get_history(message.channel.id)
    messages = [{"role": "system", "content": sys_prompt}] + history + [
        {"role": "user", "content": prompt}
    ]

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
