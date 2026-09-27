import asyncio
import logging
from dataclasses import dataclass

import discord
from discord import app_commands

import pelican
from config import settings

logger = logging.getLogger(__name__)


@dataclass
class ActiveStatusMessage:
    message: discord.Message
    server_info: pelican.ServerInfo
    expires_at: float


def render_server_embed(
    server_info: pelican.ServerInfo,
    result: pelican.ServerStats | Exception,
    expires_at: float,
    now: float,
) -> discord.Embed:
    remaining = int(max(0, expires_at - now))
    timer = f"Expires in: {remaining // 60}m {remaining % 60}s"
    host_link = f"-# {settings.pelican_base_url}"

    embed = discord.Embed(
        title=server_info.name,
        url=f"{settings.pelican_base_url}/server/{server_info.identifier}",
        color=discord.Color.blue(),
        description=f"{timer}\n{host_link}",
    )
    if isinstance(result, Exception):
        embed.description += f"\nError: {result}"
        return embed

    embed.add_field(name="Status", value=result.state, inline=False)
    embed.add_field(name="CPU", value=result.cpu, inline=False)
    embed.add_field(name="Memory", value=result.memory, inline=False)
    embed.add_field(name="Disk", value=result.disk, inline=False)
    return embed


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
        except Exception as error:
            logger.exception(
                "Server power action failed user_id=%s server_id=%s action=%s",
                interaction.user.id,
                self.server_id,
                signal,
            )
            await interaction.followup.send(
                f"Failed to send {signal} signal: {error}", ephemeral=True
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

    def add_messages(self, messages: dict[int, ActiveStatusMessage]) -> None:
        self.active_messages.update(messages)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._updater_loop())

    async def close(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        self.task = None

    async def _cleanup_expired(self, now: float) -> None:
        for message_id, data in list(self.active_messages.items()):
            if now <= data.expires_at:
                continue
            try:
                await data.message.delete()
            except (discord.NotFound, discord.Forbidden):
                logger.debug(
                    "Could not delete expired status message id=%s", message_id
                )
            except Exception:
                logger.exception(
                    "Failed to delete expired status message id=%s", message_id
                )
                continue
            self.active_messages.pop(message_id, None)

    async def _refresh_messages(self, now: float) -> None:
        servers = {
            data.server_info.identifier: data.server_info
            for data in self.active_messages.values()
        }
        stats_results = await asyncio.gather(
            *(
                pelican.get_server_stats(server_id, server.name)
                for server_id, server in servers.items()
            ),
            return_exceptions=True,
        )
        stats_by_id = dict(zip(servers, stats_results))

        for message_id, data in list(self.active_messages.items()):
            result = stats_by_id.get(data.server_info.identifier)
            embed = render_server_embed(data.server_info, result, data.expires_at, now)
            try:
                view = ServerControlView(
                    data.server_info.identifier, data.server_info.name
                )
                await data.message.edit(embed=embed, view=view)
            except discord.NotFound:
                logger.info("Removing deleted status message id=%s", message_id)
                self.active_messages.pop(message_id, None)
            except Exception:
                logger.exception("Failed to refresh status message id=%s", message_id)

    async def _updater_loop(self) -> None:
        while self.active_messages:
            await asyncio.sleep(5)
            now = asyncio.get_running_loop().time()
            await self._cleanup_expired(now)
            if self.active_messages:
                await self._refresh_messages(now)


status_updater = StatusUpdater()


def register_server_commands(
    tree: app_commands.CommandTree,
    client: discord.Client,
    updater: StatusUpdater,
) -> tuple[app_commands.Command, app_commands.Command]:
    @tree.command(name="servers", description="Check the status of Pelican servers")
    async def servers(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        logger.info("Server status requested user_id=%s", interaction.user.id)

        try:
            server_infos = await pelican.get_servers()
            if not server_infos:
                await interaction.followup.send(
                    "No servers found on this Pelican panel."
                )
                return

            results = await asyncio.gather(
                *(
                    pelican.get_server_stats(server.identifier, server.name)
                    for server in server_infos
                ),
                return_exceptions=True,
            )
            now = asyncio.get_running_loop().time()
            expires_at = now + 300
            active_messages: dict[int, ActiveStatusMessage] = {}

            for server_info, result in zip(server_infos, results):
                embed = render_server_embed(server_info, result, expires_at, now)
                view = (
                    None
                    if isinstance(result, Exception)
                    else ServerControlView(server_info.identifier, server_info.name)
                )
                message = await interaction.followup.send(
                    embed=embed, view=view, wait=True
                )
                active_messages[message.id] = ActiveStatusMessage(
                    message, server_info, expires_at
                )
            updater.add_messages(active_messages)
        except Exception as error:
            logger.exception(
                "Failed to fetch server status user_id=%s", interaction.user.id
            )
            await interaction.followup.send(
                f"An error occurred while fetching the server list: {error}"
            )

    @tree.command(name="ping", description="Check the bot's latency")
    async def ping(interaction: discord.Interaction) -> None:
        latency = round(client.latency * 1000)
        await interaction.response.send_message(f"Pong! Latency: {latency}ms")

    return servers, ping
