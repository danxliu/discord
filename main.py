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

        embed = discord.Embed(
            title="Pelican Server Status",
            color=discord.Color.blue(),
            description=f"Current status of servers on {settings.pelican_base_url}",
        )

        for server_info, res in zip(servers_data, results):
            if isinstance(res, Exception):
                embed.add_field(
                    name=server_info.name, value=f"❌ Error: {str(res)}", inline=False
                )
            else:
                stats = (
                    f"**Status:** {res.state}\n"
                    f"**CPU:** {res.cpu}\n"
                    f"**Memory:** {res.memory}\n"
                    f"**Disk:** {res.disk}"
                )
                embed.add_field(name=res.name, value=stats, inline=False)

        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(
            f"❌ An error occurred while fetching the server list: {str(e)}"
        )


def main():
    client.run(settings.discord_token)


if __name__ == "__main__":
    main()
