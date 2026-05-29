import asyncio

import discord
from discord import app_commands

import pelican
from config import settings


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
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("------")


client = Client()


class ServerControlView(discord.ui.View):
    def __init__(self, server_id: str, server_name: str):
        super().__init__(timeout=None)
        self.server_id = server_id
        self.server_name = server_name

    async def _handle_action(self, interaction: discord.Interaction, signal: str):
        await interaction.response.defer(ephemeral=True)
        try:
            await pelican.send_power_action(self.server_id, signal)
            await interaction.followup.send(
                f"Sent {signal} signal to {self.server_name}.", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"Failed to send {signal} signal: {str(e)}", ephemeral=True
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
        self.active_messages = {}  # msg_id -> {message, server_info, cache, expires_at}
        self.task = None

    def add_messages(self, messages_data: dict):
        for msg_id, data in messages_data.items():
            self.active_messages[msg_id] = data
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._updater_loop())

    async def _updater_loop(self):
        while self.active_messages:
            await asyncio.sleep(5)
            now = asyncio.get_event_loop().time()
            expired_ids = [
                msg_id
                for msg_id, data in self.active_messages.items()
                if now > data["expires_at"]
            ]
            for msg_id in expired_ids:
                data = self.active_messages.pop(msg_id)
                try:
                    await data["message"].delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            if not self.active_messages:
                break
            server_ids = {
                data["server_info"].identifier: data["server_info"]
                for data in self.active_messages.values()
            }
            fetch_tasks = [
                pelican.get_server_stats(sid, sinfo.name)
                for sid, sinfo in server_ids.items()
            ]
            fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            stats_map = {sid: res for sid, res in zip(server_ids.keys(), fetch_results)}
            msg_ids_to_remove = []
            for msg_id, data in self.active_messages.items():
                server_id = data["server_info"].identifier
                res = stats_map[server_id]

                embed = render_server_embed(
                    data["server_info"], res, data["expires_at"], now
                )

                try:
                    view = ServerControlView(
                        data["server_info"].identifier, data["server_info"].name
                    )
                    await data["message"].edit(embed=embed, view=view)
                except discord.NotFound:
                    msg_ids_to_remove.append(msg_id)
                except Exception:
                    pass

            for msg_id in msg_ids_to_remove:
                self.active_messages.pop(msg_id, None)


status_updater = StatusUpdater()


@client.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    latency = round(client.latency * 1000)
    await interaction.response.send_message(f"Pong! Latency: {latency}ms")


@client.tree.command(name="status", description="Check the status of Pelican servers")
async def status(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        servers_data = await pelican.get_servers()

        if not servers_data:
            await interaction.followup.send("No servers found on this Pelican panel.")
            return

        tasks = []
        for server in servers_data:
            tasks.append(pelican.get_server_stats(server.identifier, server.name))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        messages_data = {}
        now = asyncio.get_event_loop().time()
        expires_at = now + 300

        for server_info, res in zip(servers_data, results):
            embed = render_server_embed(server_info, res, expires_at, now)

            if isinstance(res, Exception):
                msg = await interaction.followup.send(embed=embed, wait=True)
            else:
                view = ServerControlView(server_info.identifier, server_info.name)
                msg = await interaction.followup.send(embed=embed, view=view, wait=True)

            messages_data[msg.id] = {
                "message": msg,
                "server_info": server_info,
                "expires_at": expires_at,
            }

        status_updater.add_messages(messages_data)

    except Exception as e:
        await interaction.followup.send(
            f"An error occurred while fetching the server list: {str(e)}"
        )


def main():
    client.run(settings.discord_token)


if __name__ == "__main__":
    main()
