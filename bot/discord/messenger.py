import asyncio
from typing import List, Optional
import discord


def split_content(text: str, limit: int = 2000) -> List[str]:
    if not text or not text.strip():
        return ["*(No response generated)*"]
    if len(text) <= limit:
        return [text]

    chunks = []
    current_chunk = []
    current_length = 0
    in_code_block = False
    code_lang = ""

    lines = text.split("\n")

    for line in lines:
        line_len = len(line) + 1

        if line.strip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_lang = line.strip()[3:].strip()
            else:
                in_code_block = False
                code_lang = ""

        if current_length + line_len > (limit - 10):
            if in_code_block:
                current_chunk.append("```")

            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_length = 0

            if in_code_block:
                current_chunk.append(f"```{code_lang}")
                current_length = len(current_chunk[-1]) + 1

        if line_len > limit - 20:
            words = line.split(" ")
            for word in words:
                word_len = len(word) + 1
                if current_length + word_len > (limit - 10):
                    if in_code_block:
                        current_chunk.append("```")
                    chunks.append("\n".join(current_chunk))
                    current_chunk = []
                    current_length = 0
                    if in_code_block:
                        current_chunk.append(f"```{code_lang}")
                        current_length = len(current_chunk[-1]) + 1
                current_chunk.append(word)
                current_length += word_len
        else:
            current_chunk.append(line)
            current_length += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


class DiscordMessenger:
    @staticmethod
    async def safe_react(message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except Exception:
            pass

    @staticmethod
    async def safe_remove_reaction(
        message: discord.Message, emoji: str, user: discord.ClientUser
    ) -> None:
        try:
            await message.remove_reaction(emoji, user)
        except Exception:
            pass

    @staticmethod
    async def safe_edit(message: discord.Message, content: str) -> None:
        if not content or not content.strip():
            content = "*(Empty response)*"
        try:
            await message.edit(content=content)
        except Exception:
            pass

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
        except Exception:
            return None
