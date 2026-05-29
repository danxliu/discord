import asyncio
import os

import discord
from discord import app_commands
from dotenv import load_dotenv

import pelican

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PELICAN_APPLICATION_KEY = os.getenv("PELICAN_APPLICATION_KEY")
PELICAN_CLIENT_KEY = os.getenv("PELICAN_CLIENT_KEY")
PELICAN_BASE_URL = os.getenv("PELICAN_BASE_URL")


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
        servers_data = await pelican.get_servers(
            PELICAN_APPLICATION_KEY, PELICAN_BASE_URL
        )

        if not servers_data:
            await interaction.followup.send("No servers found on this Pelican panel.")
            return
        tasks = []
        for server in servers_data:
            tasks.append(
                pelican.get_server_stats(
                    PELICAN_CLIENT_KEY,
                    PELICAN_BASE_URL,
                    server["identifier"],
                    server["name"],
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        embed = discord.Embed(
            title="Pelican Server Status",
            color=discord.Color.blue(),
            description=f"Current status of servers on {PELICAN_BASE_URL}",
        )

        for server_info, res in zip(servers_data, results):
            if isinstance(res, Exception):
                embed.add_field(
                    name=server_info["name"],
                    value=f"❌ Error: {str(res)}",
                    inline=False,
                )
            else:
                stats = (
                    f"**Status:** {res['state']}\n"
                    f"**CPU:** {res['cpu']}\n"
                    f"**Memory:** {res['memory']}\n"
                    f"**Disk:** {res['disk']}"
                )
                embed.add_field(name=res["name"], value=stats, inline=False)

        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(
            f"❌ An error occurred while fetching the server list: {str(e)}"
        )


def main():
    if not TOKEN:
        print("Error: DISCORD_TOKEN not found in environment.")
        return
    if not PELICAN_APPLICATION_KEY or not PELICAN_CLIENT_KEY or not PELICAN_BASE_URL:
        print(
            "Error: Pelican configuration (Key or Base URL) not found in environment."
        )
        return

    client.run(TOKEN)


if __name__ == "__main__":
    main()
