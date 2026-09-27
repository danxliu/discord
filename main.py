import asyncio
import logging

import discord
from discord import app_commands

import pelican
from bot.agent import AgenticLoop
from bot.discord import handle_message_event
from bot.discord.server_status import (
    ActiveStatusMessage,
    ServerControlView,
    StatusUpdater,
    register_server_commands,
    render_server_embed,
    status_updater,
)
from bot.memory import UserMemory
from bot.net import close_public_session
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
    register_gif_send_tool,
    register_image_gen_tool,
)
from config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "ActiveStatusMessage",
    "Client",
    "ServerControlView",
    "StatusUpdater",
    "ping",
    "render_server_embed",
    "servers",
    "status_updater",
]


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

    async def close(self):
        try:
            await status_updater.close()
        finally:
            try:
                await asyncio.gather(pelican.close_session(), close_public_session())
            finally:
                await super().close()


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
register_gif_send_tool(tool_registry, settings.giphy_api_key)

agent_loop = AgenticLoop(
    api_key=settings.ai_api_key,
    base_url=settings.ai_base_url,
    model=settings.ai_model,
    tool_registry=tool_registry,
    max_iterations=settings.ai_max_iterations,
    stream=settings.ai_stream_response,
)

servers, ping = register_server_commands(client.tree, client, status_updater)


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
