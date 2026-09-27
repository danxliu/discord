import logging

import discord

logger = logging.getLogger(__name__)


def split_content(text: str, limit: int = 2000) -> list[str]:
    if not text or not text.strip():
        return ["*(No response generated)*"]
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]
    if limit < 16:
        return [text[index : index + limit] for index in range(0, len(text), limit)]

    chunks: list[str] = []
    current = ""
    in_code_block = False
    code_lang = ""

    def code_prefix() -> str:
        prefix = f"```{code_lang}\n" if code_lang else "```\n"
        return prefix if len(prefix) + 4 < limit else "```\n"

    def push_chunk() -> None:
        nonlocal current
        if not current:
            return
        if in_code_block:
            current += "\n```"
        chunks.append(current)
        current = code_prefix() if in_code_block else ""

    def append_text(value: str) -> None:
        nonlocal current
        while value:
            suffix_size = 4 if in_code_block else 0
            available = limit - len(current) - suffix_size
            if available <= 0:
                push_chunk()
                available = limit - len(current) - suffix_size
            if available <= 0:
                current = "```\n" if in_code_block else ""
                available = limit - len(current) - suffix_size
            take = min(available, len(value))
            current += value[:take]
            value = value[take:]
            if value:
                push_chunk()

    for line in text.splitlines(keepends=True):
        is_fence = line.strip().startswith("```")
        line_to_add = (
            line if not current else ("" if current.endswith("\n") else "\n") + line
        )
        next_code_state = not in_code_block if is_fence else in_code_block
        suffix_size = 4 if in_code_block or next_code_state else 0
        if len(current) + len(line_to_add) + suffix_size <= limit:
            current += line_to_add
        else:
            if current:
                push_chunk()
                line_to_add = line
            append_text(line_to_add)

        if is_fence:
            if in_code_block:
                in_code_block = False
                code_lang = ""
            else:
                in_code_block = True
                code_lang = line.strip()[3:].strip()

    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()] or ["*(No response generated)*"]


class DiscordMessenger:
    @staticmethod
    async def safe_edit(message: discord.Message, content: str) -> None:
        if not content or not content.strip():
            content = "*(Empty response)*"
        try:
            await message.edit(content=content)
        except (discord.NotFound, discord.Forbidden):
            logger.debug("Could not edit message message_id=%s", message.id)
        except discord.HTTPException:
            logger.warning(
                "Discord rejected message edit message_id=%s", message.id, exc_info=True
            )
        except Exception:
            logger.exception(
                "Unexpected message edit failure message_id=%s", message.id
            )

    @staticmethod
    async def safe_send(
        channel: discord.abc.Messageable,
        content: str,
        reply_to: discord.Message | None = None,
    ) -> discord.Message | None:
        if not content or not content.strip():
            content = "*(Empty response)*"
        try:
            if reply_to:
                return await reply_to.reply(content, mention_author=False)
            return await channel.send(content)
        except (discord.NotFound, discord.Forbidden):
            logger.debug(
                "Could not send message channel_id=%s", getattr(channel, "id", None)
            )
        except discord.HTTPException:
            logger.warning(
                "Discord rejected message send channel_id=%s",
                getattr(channel, "id", None),
                exc_info=True,
            )
        except Exception:
            logger.exception(
                "Unexpected message send failure channel_id=%s",
                getattr(channel, "id", None),
            )
        return None
