import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

from bot.agent.sanitizer import StreamingSanitizer
from bot.discord.image import (
    is_video_unsupported_error,
    replace_video_parts_with_fallbacks,
)
from bot.tools.base import ToolContext, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

THINK_PATTERNS = [
    re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<thought>.*?</thought>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<reasoning>.*?</reasoning>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\|thought\|>.*?<\|/?thought\|>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\|think\|>.*?<\|/?think\|>", re.DOTALL | re.IGNORECASE),
    re.compile(r"\|thought\|.*?\|/?thought\|", re.DOTALL | re.IGNORECASE),
    re.compile(r"\|think\|.*?\|/?think\|", re.DOTALL | re.IGNORECASE),
    re.compile(r"\[THINK\].*?\[/THINK\]", re.DOTALL | re.IGNORECASE),
    re.compile(
        r"^(?:<think>|<thought>|<\|thought\|>|\[THINK\]).*?(?:\n\n|\Z)",
        re.DOTALL | re.IGNORECASE,
    ),
]


class AgenticLoop:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        tool_registry: ToolRegistry,
        max_iterations: int = 10,
        stream: bool = True,
    ):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations
        self.stream = stream

    def _sanitize(self, text: str) -> str:
        if not text:
            return ""
        cleaned = text
        for pattern in THINK_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        return cleaned.strip()

    def _accumulate_tool_call_delta(
        self, accumulated: dict[int, dict[str, Any]], tc_delta: Any
    ) -> None:
        idx = tc_delta.index
        if idx not in accumulated:
            accumulated[idx] = {
                "id": tc_delta.id or "",
                "type": tc_delta.type or "function",
                "function": {
                    "name": (tc_delta.function.name or "" if tc_delta.function else ""),
                    "arguments": (
                        tc_delta.function.arguments or "" if tc_delta.function else ""
                    ),
                },
            }
            return

        target = accumulated[idx]
        if tc_delta.id:
            target["id"] += tc_delta.id
        if tc_delta.function:
            fn = tc_delta.function
            if fn.name:
                target["function"]["name"] += fn.name
            if fn.arguments:
                target["function"]["arguments"] += fn.arguments

    async def _non_stream_step(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools if tools else None,
        )
        msg = response.choices[0].message
        tool_calls = [
            {
                "id": tc.id,
                "type": tc.type,
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in (msg.tool_calls or [])
        ]
        return msg.content or "", tool_calls

    async def _stream_step(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_status: Callable[[str], Awaitable[None]] | None,
        on_chunk: Callable[[str], Awaitable[None]] | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        response_stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools if tools else None,
            stream=True,
        )

        accumulated_tool_calls: dict[int, dict[str, Any]] = {}
        content_pieces: list[str] = []
        sanitizer = StreamingSanitizer(on_chunk=on_chunk, on_status=on_status)

        async for chunk in response_stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if not delta:
                continue

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    self._accumulate_tool_call_delta(accumulated_tool_calls, tc)

            if delta.content:
                content_pieces.append(delta.content)
                if not accumulated_tool_calls:
                    await sanitizer.process(delta.content)

        await sanitizer.flush()

        tool_calls = [
            accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls.keys())
        ]
        return "".join(content_pieces), tool_calls

    def _parse_tool_arguments(self, raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        try:
            return json.loads(raw_args or "{}")
        except Exception:
            return {}

    async def _execute_tools(
        self,
        tool_calls: list[dict[str, Any]],
        context: ToolContext,
        messages: list[dict[str, Any]],
        on_status: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        multimodal_content: list[dict[str, Any]] = []
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            tool_obj = self.tool_registry.get(fn_name)
            display_name = tool_obj.display_name if tool_obj else fn_name

            if on_status:
                await on_status(f"Calling: {display_name}...")

            args = self._parse_tool_arguments(tc["function"]["arguments"])
            started_at = time.monotonic()
            logger.info(
                "Tool call started request_id=%s tool=%s",
                context.request_id,
                fn_name,
            )
            result = await self.tool_registry.execute(fn_name, args, context)
            result_text = result.content if isinstance(result, ToolResult) else result
            logger.info(
                "Tool call completed request_id=%s tool=%s success=%s duration_seconds=%.2f",
                context.request_id,
                fn_name,
                not result_text.startswith("Error"),
                time.monotonic() - started_at,
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fn_name,
                    "content": result_text,
                }
            )
            if isinstance(result, ToolResult):
                context.video_fallbacks.update(result.video_fallbacks)
                if result.multimodal_content:
                    multimodal_content.extend(result.multimodal_content)

        if multimodal_content:
            messages.append({"role": "user", "content": multimodal_content})

    async def run(
        self,
        messages: list[dict[str, Any]],
        context: ToolContext,
        on_status: Callable[[str], Awaitable[None]] | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        current_messages = list(messages)
        tools = self.tool_registry.get_schemas()
        should_stream = self.stream and on_chunk is not None
        iteration = 0
        tool_call_count = 0
        started_at = time.monotonic()
        logger.info(
            "Agent run started request_id=%s model=%s messages=%d",
            context.request_id,
            self.model,
            len(messages),
        )

        while iteration < self.max_iterations:
            image_count = sum(
                part.get("type") == "image_url"
                for message in current_messages
                if isinstance(message.get("content"), list)
                for part in message["content"]
            )
            logger.info(
                "Submitting model request request_id=%s image_parts=%d",
                context.request_id,
                image_count,
            )
            if on_status:
                await on_status("Thinking...")

            try:
                if should_stream:
                    content, tool_calls = await self._stream_step(
                        current_messages, tools, on_status, on_chunk
                    )
                else:
                    content, tool_calls = await self._non_stream_step(
                        current_messages, tools
                    )
            except Exception as error:
                if (
                    context.video_input_fallback_used
                    or not context.video_fallbacks
                    or not is_video_unsupported_error(str(error))
                ):
                    raise
                if not replace_video_parts_with_fallbacks(
                    current_messages, context.video_fallbacks
                ):
                    raise
                context.video_input_fallback_used = True
                logger.info(
                    "Video input rejected; retrying with sampled frames request_id=%s error=%s",
                    context.request_id,
                    error,
                )
                if on_status:
                    await on_status("Video input is unsupported; retrying with sampled frames...")
                if should_stream:
                    content, tool_calls = await self._stream_step(
                        current_messages, tools, on_status, on_chunk
                    )
                else:
                    content, tool_calls = await self._non_stream_step(
                        current_messages, tools
                    )

            if not tool_calls:
                answer = self._sanitize(content)
                logger.info(
                    "Agent run completed request_id=%s iterations=%d tool_calls=%d response_chars=%d duration_seconds=%.2f",
                    context.request_id,
                    iteration + 1,
                    tool_call_count,
                    len(answer),
                    time.monotonic() - started_at,
                )
                return answer

            current_messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                }
            )

            await self._execute_tools(tool_calls, context, current_messages, on_status)
            tool_call_count += len(tool_calls)
            iteration += 1

        logger.warning(
            "Agent reached iteration limit request_id=%s iterations=%d tool_calls=%d",
            context.request_id,
            iteration,
            tool_call_count,
        )
        return "Reached maximum tool iterations without completing response."
