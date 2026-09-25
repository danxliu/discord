import logging
from typing import List, Optional

import discord

logger = logging.getLogger(__name__)


def split_content(text: str, limit: int = 2000) -> List[str]:
    if not text or not text.strip():
        return ["*(No response generated)*"]
    if len(text) <= limit:
        return [text]

    chunks = []
    current_chunk = ""
    in_code_block = False
    code_lang = ""

    def push_chunk() -> None:
        nonlocal current_chunk
        if not current_chunk:
            return
        if in_code_block:
            current_chunk += "\n```"
        chunks.append(current_chunk)
        current_chunk = f"```{code_lang}\n" if in_code_block else ""

    def append_token(token: str) -> None:
        nonlocal current_chunk
        closing_fence = "\n```" if in_code_block else ""
        if len(current_chunk) + len(token) + len(closing_fence) <= limit:
            current_chunk += token
            return

        push_chunk()
        while len(token) + (len("\n```") if in_code_block else 0) > limit:
            closing_len = len("\n```") if in_code_block else 0
            cut = limit - closing_len - len(current_chunk)
            current_chunk += token[:cut]
            token = token[cut:]
            push_chunk()

        current_chunk += token

    lines = text.split("\n")
    for line in lines:
        line_to_add = line if not current_chunk else "\n" + line
        stripped_line = line.strip()
        is_code_fence = stripped_line.startswith("```")

        next_in_code_block = (
            not in_code_block if is_code_fence else in_code_block
        )
        next_code_lang = (
            stripped_line[3:].strip()
            if is_code_fence and not in_code_block
            else ("" if is_code_fence else code_lang)
        )

        closing_fence = "\n```" if in_code_block else ""
        if len(current_chunk) + len(line_to_add) + len(closing_fence) <= limit:
            current_chunk += line_to_add
        else:
            if current_chunk:
                push_chunk()
                line_to_add = line

            closing_fence = "\n```" if in_code_block else ""
            if len(current_chunk) + len(line_to_add) + len(closing_fence) <= limit:
                current_chunk += line_to_add
            else:
                words = line.split(" ")
                for word in words:
                    prefix = "" if not current_chunk or current_chunk.endswith("\n") else " "
                    append_token(prefix + word)

        in_code_block = next_in_code_block
        code_lang = next_code_lang

    if current_chunk:
        chunks.append(current_chunk)

    return [c for c in chunks if c.strip()] or ["*(No response generated)*"]


class DiscordMessenger:
    @staticmethod
    async def safe_react(message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except (discord.NotFound, discord.Forbidden):
            logger.debug("Could not add reaction message_id=%s", message.id)
        except discord.HTTPException:
            logger.warning("Discord rejected reaction message_id=%s", message.id, exc_info=True)
        except Exception:
            logger.exception("Unexpected reaction failure message_id=%s", message.id)

    @staticmethod
    async def safe_remove_reaction(
        message: discord.Message, emoji: str, user: discord.ClientUser
    ) -> None:
        try:
            await message.remove_reaction(emoji, user)
        except (discord.NotFound, discord.Forbidden):
            logger.debug("Could not remove reaction message_id=%s", message.id)
        except discord.HTTPException:
            logger.warning("Discord rejected reaction removal message_id=%s", message.id, exc_info=True)
        except Exception:
            logger.exception("Unexpected reaction removal failure message_id=%s", message.id)

    @staticmethod
    async def safe_edit(message: discord.Message, content: str) -> None:
        if not content or not content.strip():
            content = "*(Empty response)*"
        try:
            await message.edit(content=content)
        except (discord.NotFound, discord.Forbidden):
            logger.debug("Could not edit message message_id=%s", message.id)
        except discord.HTTPException:
            logger.warning("Discord rejected message edit message_id=%s", message.id, exc_info=True)
        except Exception:
            logger.exception("Unexpected message edit failure message_id=%s", message.id)

    @staticmethod
    async def safe_send(
        channel: discord.abc.Messageable,
        content: str,
        reply_to: Optional[discord.Message] = None,
    ) -> Optional[discord.Message]:
        if not content or not content.strip():
            content = "*(Empty response)*"
        try:
            if reply_to:
                return await reply_to.reply(content, mention_author=False)
            return await channel.send(content)
        except (discord.NotFound, discord.Forbidden):
            logger.debug("Could not send message channel_id=%s", getattr(channel, "id", None))
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
