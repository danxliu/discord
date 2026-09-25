import re
from collections.abc import Awaitable, Callable

OPEN_TAG_PATTERNS: list[tuple[re.Pattern, re.Pattern]] = [
    (re.compile(r"^\s*<think>", re.IGNORECASE), re.compile(r"</think>", re.IGNORECASE)),
    (
        re.compile(r"^\s*<thought>", re.IGNORECASE),
        re.compile(r"</thought>", re.IGNORECASE),
    ),
    (
        re.compile(r"^\s*<reasoning>", re.IGNORECASE),
        re.compile(r"</reasoning>", re.IGNORECASE),
    ),
    (
        re.compile(r"^\s*<\|thought\|>", re.IGNORECASE),
        re.compile(r"<\|/?thought\|>", re.IGNORECASE),
    ),
    (
        re.compile(r"^\s*<\|think\|>", re.IGNORECASE),
        re.compile(r"<\|/?think\|>", re.IGNORECASE),
    ),
    (
        re.compile(r"^\s*\|thought\|", re.IGNORECASE),
        re.compile(r"\|/?thought\|", re.IGNORECASE),
    ),
    (
        re.compile(r"^\s*\|think\|", re.IGNORECASE),
        re.compile(r"\|/?think\|", re.IGNORECASE),
    ),
    (
        re.compile(r"^\s*\[THINK\]", re.IGNORECASE),
        re.compile(r"\[/THINK\]", re.IGNORECASE),
    ),
]


class StreamingSanitizer:
    """Filters reasoning/thinking tokens (e.g. <think>...</think>) from a token stream.

    Only emits user-facing response tokens to `on_chunk`.
    """

    def __init__(
        self,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
        on_status: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.on_chunk = on_chunk
        self.on_status = on_status
        self.is_checking_prefix: bool = True
        self.in_thinking: bool = False
        self.close_regex: re.Pattern | None = None
        self.buffer: str = ""
        self.think_buffer: str = ""

    async def process(self, chunk: str) -> None:
        if not chunk:
            return

        if self.is_checking_prefix:
            self.buffer += chunk
            stripped = self.buffer.lstrip()
            if not stripped:
                return

            # If the first non-whitespace character cannot start any thinking tag
            if stripped[0] not in ("<", "[", "|"):
                self.is_checking_prefix = False
                to_send = self.buffer
                self.buffer = ""
                if self.on_chunk:
                    await self.on_chunk(to_send)
                return

            # Check if any opening tag matches
            matched = False
            for open_re, close_re in OPEN_TAG_PATTERNS:
                m = open_re.match(self.buffer)
                if m:
                    self.is_checking_prefix = False
                    self.in_thinking = True
                    self.close_regex = close_re
                    after_open = self.buffer[m.end() :]
                    self.buffer = ""
                    self.think_buffer = after_open
                    matched = True
                    if self.on_status:
                        await self.on_status("Thinking...")
                    break

            if matched:
                if self.think_buffer:
                    await self._check_thinking_close()
                return

            # If prefix buffer has grown past potential opening tags without matching
            if len(stripped) >= 15:
                self.is_checking_prefix = False
                to_send = self.buffer
                self.buffer = ""
                if self.on_chunk:
                    await self.on_chunk(to_send)
                return
            return

        if self.in_thinking:
            self.think_buffer += chunk
            await self._check_thinking_close()
            return

        if self.on_chunk:
            await self.on_chunk(chunk)

    async def _check_thinking_close(self) -> None:
        if not self.close_regex:
            return

        m = self.close_regex.search(self.think_buffer)
        if m:
            self.in_thinking = False
            remaining = self.think_buffer[m.end() :].lstrip("\r\n")
            self.think_buffer = ""
            if remaining and self.on_chunk:
                await self.on_chunk(remaining)

    async def flush(self) -> None:
        if self.is_checking_prefix and self.buffer:
            to_send = self.buffer
            self.buffer = ""
            self.is_checking_prefix = False
            if self.on_chunk:
                await self.on_chunk(to_send)
        elif self.in_thinking and self.think_buffer:
            # If thinking tag was never closed, look for paragraph break
            m = re.search(r"\n\n+", self.think_buffer)
            if m:
                remaining = self.think_buffer[m.end() :].strip()
                if remaining and self.on_chunk:
                    await self.on_chunk(remaining)
            self.think_buffer = ""
            self.in_thinking = False
