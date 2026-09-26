from importlib import import_module

_EXPORTS = {
    "DiscordMessenger": ("bot.discord.messenger", "DiscordMessenger"),
    "split_content": ("bot.discord.messenger", "split_content"),
    "handle_message_event": ("bot.discord.handler", "handle_message_event"),
    "MessageStreamer": ("bot.discord.streamer", "MessageStreamer"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = _EXPORTS[name]
    attribute = getattr(import_module(module_name), attribute_name)
    globals()[name] = attribute
    return attribute
