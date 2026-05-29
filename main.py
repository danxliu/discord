import asyncio

import discord
from discord import app_commands

import pelican
from config import settings


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
    async def restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "restart")


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

        await interaction.followup.send(
            f"Pelican Server Status (Host: {settings.pelican_base_url})"
        )

        for server_info, res in zip(servers_data, results):
            embed = discord.Embed(title=server_info.name, color=discord.Color.blue())
            if isinstance(res, Exception):
                embed.description = f"Error: {str(res)}"
                await interaction.followup.send(embed=embed)
            else:
                embed.add_field(name="Status", value=res.state, inline=True)
                embed.add_field(name="CPU", value=res.cpu, inline=True)
                embed.add_field(name="Memory", value=res.memory, inline=True)
                embed.add_field(name="Disk", value=res.disk, inline=True)

                view = ServerControlView(server_info.identifier, server_info.name)
                await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        await interaction.followup.send(
            f"An error occurred while fetching the server list: {str(e)}"
        )


def main():
    client.run(settings.discord_token)


if __name__ == "__main__":
    main()
