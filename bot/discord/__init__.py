from typing import TYPE_CHECKING

from bot.discord.messenger import DiscordMessenger, split_content
from bot.discord.streamer import MessageStreamer

if TYPE_CHECKING:
    from bot.discord.handler import handle_message_event

__all__ = [
    "DiscordMessenger",
    "MessageStreamer",
    "handle_message_event",
    "split_content",
]


def __getattr__(name: str):
    if name == "handle_message_event":
        from bot.discord.handler import handle_message_event

        globals()["handle_message_event"] = handle_message_event
        return handle_message_event
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
