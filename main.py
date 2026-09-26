import asyncio
import logging
from dataclasses import dataclass

import discord
from discord import app_commands

import pelican
from bot.agent import AgenticLoop
from bot.discord import handle_message_event
from bot.memory import UserMemory
from bot.tools import (
    DiscordHistorySearchTool,
    DiscordReactionAddTool,
    DiscordThreadCreateTool,
    MemoryAddTool,
    MemoryReadTool,
    MemoryRemoveTool,
    ToolRegistry,
    WebScrapeTool,
    WebSearchTool,
    register_image_gen_tool,
)
from config import settings

logger = logging.getLogger(__name__)


@dataclass
class ActiveStatusMessage:
    message: discord.Message
    server_info: pelican.ServerInfo
    expires_at: float


def render_server_embed(
    server_info: pelican.ServerInfo,
    res: pelican.ServerStats | Exception,
    expires_at: float,
    now: float,
) -> discord.Embed:
    remaining = int(max(0, expires_at - now))
    timer_str = f"Expires in: {remaining // 60}m {remaining % 60}s"
    host_link = f"-# {settings.pelican_base_url}"

    embed = discord.Embed(
        title=server_info.name,
        url=f"{settings.pelican_base_url}/server/{server_info.identifier}",
        color=discord.Color.blue(),
        description=f"{timer_str}\n{host_link}",
    )

    if isinstance(res, Exception):
        embed.description += f"\nError: {str(res)}"
    else:
        embed.add_field(name="Status", value=res.state, inline=False)
        embed.add_field(name="CPU", value=res.cpu, inline=False)
        embed.add_field(name="Memory", value=res.memory, inline=False)
        embed.add_field(name="Disk", value=res.disk, inline=False)

    return embed


class Client(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        # Presence is privileged and must also be enabled in the Discord Developer Portal.
        intents.presences = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Synced slash commands")

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message):
        await handle_message_event(message, self.user, agent_loop)


client = Client()

user_memory = UserMemory()

tool_registry = ToolRegistry()
tool_registry.register(WebSearchTool())
tool_registry.register(WebScrapeTool())
tool_registry.register(MemoryReadTool(user_memory))
tool_registry.register(MemoryAddTool(user_memory))
tool_registry.register(MemoryRemoveTool(user_memory))
tool_registry.register(DiscordThreadCreateTool(client))
tool_registry.register(DiscordReactionAddTool(client))
tool_registry.register(DiscordHistorySearchTool(client))
register_image_gen_tool(
    tool_registry,
    model=settings.ai_image_model,
    api_key=settings.ai_api_key,
    base_url=settings.ai_base_url,
)

agent_loop = AgenticLoop(
    api_key=settings.ai_api_key,
    base_url=settings.ai_base_url,
    model=settings.ai_model,
    tool_registry=tool_registry,
    max_iterations=settings.ai_max_iterations,
    stream=settings.ai_stream_response,
)


class ServerControlView(discord.ui.View):
    def __init__(self, server_id: str, server_name: str):
        super().__init__(timeout=None)
        self.server_id = server_id
        self.server_name = server_name

    async def _handle_action(self, interaction: discord.Interaction, signal: str):
        await interaction.response.defer(ephemeral=True)
        logger.info(
            "Server power action requested user_id=%s server_id=%s action=%s",
            interaction.user.id,
            self.server_id,
            signal,
        )
        try:
            await pelican.send_power_action(self.server_id, signal)
        except Exception as e:
            logger.exception(
                "Server power action failed user_id=%s server_id=%s action=%s",
                interaction.user.id,
                self.server_id,
                signal,
            )
            await interaction.followup.send(
                f"Failed to send {signal} signal: {str(e)}", ephemeral=True
            )
            return

        logger.info(
            "Server power action sent user_id=%s server_id=%s action=%s",
            interaction.user.id,
            self.server_id,
            signal,
        )
        try:
            await interaction.followup.send(
                f"Sent {signal} signal to {self.server_name}.", ephemeral=True
            )
        except Exception:
            logger.exception(
                "Server power action succeeded but confirmation delivery failed user_id=%s server_id=%s action=%s",
                interaction.user.id,
                self.server_id,
                signal,
            )

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "start")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "stop")

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary)
    async def restart(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._handle_action(interaction, "restart")


class StatusUpdater:
    def __init__(self):
        self.active_messages: dict[int, ActiveStatusMessage] = {}
        self.task: asyncio.Task | None = None

    def add_messages(self, messages_data: dict[int, ActiveStatusMessage]) -> None:
        self.active_messages.update(messages_data)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._updater_loop())

    async def _cleanup_expired(self, now: float) -> None:
        expired_ids = [
            msg_id
            for msg_id, data in self.active_messages.items()
            if now > data.expires_at
        ]
        for msg_id in expired_ids:
            data = self.active_messages.pop(msg_id, None)
            if data:
                try:
                    await data.message.delete()
                except (discord.NotFound, discord.Forbidden):
                    logger.debug(
                        "Could not delete expired status message id=%s", msg_id
                    )

    async def _refresh_messages(self, now: float) -> None:
        server_ids = {
            data.server_info.identifier: data.server_info
            for data in self.active_messages.values()
        }
        fetch_tasks = [
            pelican.get_server_stats(sid, sinfo.name)
            for sid, sinfo in server_ids.items()
        ]
        fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        stats_map = dict(zip(server_ids.keys(), fetch_results))

        msg_ids_to_remove = []
        for msg_id, data in self.active_messages.items():
            server_id = data.server_info.identifier
            res = stats_map.get(server_id)

            embed = render_server_embed(data.server_info, res, data.expires_at, now)
            try:
                view = ServerControlView(
                    data.server_info.identifier, data.server_info.name
                )
                await data.message.edit(embed=embed, view=view)
            except discord.NotFound:
                logger.info("Removing deleted status message id=%s", msg_id)
                msg_ids_to_remove.append(msg_id)
            except Exception:
                logger.exception("Failed to refresh status message id=%s", msg_id)

        for msg_id in msg_ids_to_remove:
            self.active_messages.pop(msg_id, None)

    async def _updater_loop(self) -> None:
        while self.active_messages:
            await asyncio.sleep(5)
            now = asyncio.get_running_loop().time()
            await self._cleanup_expired(now)
            if self.active_messages:
                await self._refresh_messages(now)


status_updater = StatusUpdater()


@client.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    latency = round(client.latency * 1000)
    await interaction.response.send_message(f"Pong! Latency: {latency}ms")


@client.tree.command(name="servers", description="Check the status of Pelican servers")
async def servers(interaction: discord.Interaction):
    await interaction.response.defer()
    logger.info("Server status requested user_id=%s", interaction.user.id)

    try:
        servers_data = await pelican.get_servers()

        if not servers_data:
            await interaction.followup.send("No servers found on this Pelican panel.")
            return

        tasks = []
        for server in servers_data:
            tasks.append(pelican.get_server_stats(server.identifier, server.name))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        messages_data: dict[int, ActiveStatusMessage] = {}
        now = asyncio.get_running_loop().time()
        expires_at = now + 300

        for server_info, res in zip(servers_data, results):
            embed = render_server_embed(server_info, res, expires_at, now)

            if isinstance(res, Exception):
                msg = await interaction.followup.send(embed=embed, wait=True)
            else:
                view = ServerControlView(server_info.identifier, server_info.name)
                msg = await interaction.followup.send(embed=embed, view=view, wait=True)

            messages_data[msg.id] = ActiveStatusMessage(
                message=msg,
                server_info=server_info,
                expires_at=expires_at,
            )

        status_updater.add_messages(messages_data)

    except Exception as e:
        logger.exception(
            "Failed to fetch server status user_id=%s", interaction.user.id
        )
        await interaction.followup.send(
            f"An error occurred while fetching the server list: {str(e)}"
        )


def main():
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("Starting Discord bot")
    client.run(settings.discord_token)


if __name__ == "__main__":
    main()
