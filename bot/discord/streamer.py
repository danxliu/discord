import asyncio
import logging
import time

import discord

from bot.discord.messenger import split_content

logger = logging.getLogger(__name__)


def auto_close_code_blocks(text: str) -> str:
    """If text has an unclosed code block, temporarily close it for live preview."""
    count = text.count("```")
    if count % 2 != 0:
        return text + "\n```"
    return text


class MessageStreamer:
    """Streams text to Discord by periodically editing messages at a safe interval.

    Handles:
    - Discord HTTP 429 rate limit backoff
    - Automatic multi-message splitting when content exceeds 2000 characters
    - Temporary markdown code-block closure during intermediate streaming
    - Safe error handling and interaction vs standard message differences
    """

    def __init__(
        self,
        target_message: discord.Message | None = None,
        interaction: discord.Interaction | None = None,
        channel: discord.abc.Messageable | None = None,
        interval: float = 1.2,
    ):
        if target_message is None and interaction is None:
            raise ValueError("Either target_message or interaction must be provided.")

        self.target_message = target_message
        self.interaction = interaction
        self.channel = channel or (
            target_message.channel
            if target_message
            else getattr(interaction, "channel", None)
        )
        self.interval = max(0.1, interval)

        self.buffer: str = ""
        self._last_rendered_buffer: str = ""
        self._rendered_chunks: dict[int, str] = {}
        # messages list tracks the discord.Message objects for chunks [0, 1, 2, ...]
        # For interaction, messages[0] is None because interaction.edit_original_response is used
        self.messages: list[discord.Message | None] = (
            [target_message] if target_message else [None]
        )

        self._is_active: bool = True
        self._rate_limit_until: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._updater_task: asyncio.Task | None = asyncio.create_task(
            self._periodic_loop()
        )

    async def feed(self, chunk: str) -> None:
        """Feed a new token/chunk to the streamer."""
        if not chunk:
            return
        async with self._lock:
            self.buffer += chunk

    async def set_status(self, status: str) -> None:
        """Display an intermediate status (e.g. '*Calling: Web Search...*') if content streaming hasn't started yet."""
        async with self._lock:
            if not self.buffer.strip():
                content = f"*{status}*"
                await self._edit_message_index(0, content)

    async def _edit_message_index(self, index: int, content: str) -> bool:
        """Safely edit message at index with rate-limit backoff."""
        if not content or not content.strip():
            content = "*(Thinking...)*"

        now = time.monotonic()
        if now < self._rate_limit_until:
            await asyncio.sleep(self._rate_limit_until - now)

        try:
            if index == 0 and self.interaction:
                await self.interaction.edit_original_response(content=content)
                return True
            else:
                msg = self.messages[index]
                if msg:
                    await msg.edit(content=content)
                    return True
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, "retry_after", 2.0) or 2.0
                logger.warning(
                    "Discord rate limited message edit; retry delay_seconds=%.2f",
                    retry_after,
                )
                self._rate_limit_until = time.monotonic() + retry_after
                await asyncio.sleep(retry_after)
            elif e.code == 50006:
                logger.debug("Discord rejected empty message edit")
            else:
                logger.warning("Discord message edit failed", exc_info=True)
        except Exception:
            logger.exception("Unexpected message edit failure")
        return False

    async def _send_next_message(self, content: str) -> discord.Message | None:
        """Send a new message for chunk overflow (> 2000 characters)."""
        if not content or not content.strip():
            content = "..."

        now = time.monotonic()
        if now < self._rate_limit_until:
            await asyncio.sleep(self._rate_limit_until - now)

        try:
            if self.interaction:
                msg = await self.interaction.followup.send(content, wait=True)
            elif self.channel:
                msg = await self.channel.send(content)
            else:
                return None
            self.messages.append(msg)
            return msg
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, "retry_after", 2.0) or 2.0
                logger.warning(
                    "Discord rate limited message send; retry delay_seconds=%.2f",
                    retry_after,
                )
                self._rate_limit_until = time.monotonic() + retry_after
                await asyncio.sleep(retry_after)
            else:
                logger.warning("Discord message send failed", exc_info=True)
        except Exception:
            logger.exception("Unexpected message send failure")
        return None

    async def _flush(self, is_final: bool = False) -> None:
        """Flush current buffer to Discord."""
        async with self._lock:
            if not self.buffer.strip():
                return
            if not is_final and self.buffer == self._last_rendered_buffer:
                return

            text_to_render = (
                self.buffer if is_final else auto_close_code_blocks(self.buffer)
            )
            chunks = split_content(text_to_render, limit=2000)
            if not chunks:
                return

            for i, chunk in enumerate(chunks):
                if i < len(self.messages):
                    if self._rendered_chunks.get(i) != chunk:
                        if await self._edit_message_index(i, chunk):
                            self._rendered_chunks[i] = chunk
                else:
                    if await self._send_next_message(chunk):
                        self._rendered_chunks[i] = chunk

            self._last_rendered_buffer = self.buffer

    async def _periodic_loop(self) -> None:
        """Background loop executing periodically to update Discord."""
        try:
            while self._is_active:
                await asyncio.sleep(self.interval)
                if not self._is_active:
                    break
                await self._flush(is_final=False)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Message stream update failed")

    async def finalize(
        self, final_text: str | None = None
    ) -> list[discord.Message | None]:
        """Stop periodic loop and ensure the entire final response is rendered on Discord."""
        self._is_active = False
        if self._updater_task and not self._updater_task.done():
            self._updater_task.cancel()
            try:
                await self._updater_task
            except asyncio.CancelledError:
                pass

        if final_text is not None:
            async with self._lock:
                self.buffer = final_text

        await self._flush(is_final=True)
        return self.messages

    async def stop(self) -> None:
        """Cancel updater loop without performing a final edit."""
        self._is_active = False
        if self._updater_task and not self._updater_task.done():
            self._updater_task.cancel()
            try:
                await self._updater_task
            except asyncio.CancelledError:
                pass
