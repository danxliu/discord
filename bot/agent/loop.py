import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional
from openai import AsyncOpenAI
from bot.tools.base import ToolContext, ToolRegistry

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
    ):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations

    def _sanitize(self, text: str) -> str:
        if not text:
            return ""
        cleaned = text
        for pattern in THINK_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        return cleaned.strip()

    async def run(
        self,
        messages: List[Dict[str, Any]],
        context: ToolContext,
        on_status: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> str:
        current_messages = list(messages)
        tools = self.tool_registry.get_schemas()
        iteration = 0

        while iteration < self.max_iterations:
            if on_status:
                await on_status("Thinking...")

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=current_messages,
                tools=tools if tools else None,
            )

            choice = response.choices[0]
            msg = choice.message

            if not msg.tool_calls:
                return self._sanitize(msg.content or "")

            assistant_turn = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            current_messages.append(assistant_turn)

            for tc in msg.tool_calls:
                fn_name = tc.function.name
                tool_obj = self.tool_registry.get(fn_name)
                display_name = tool_obj.display_name if tool_obj else fn_name

                if on_status:
                    await on_status(f"Calling: {display_name}...")

                if isinstance(tc.function.arguments, dict):
                    args = tc.function.arguments
                else:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}

                result = await self.tool_registry.execute(fn_name, args, context)

                current_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": fn_name,
                        "content": str(result),
                    }
                )

            iteration += 1

        return "Reached maximum tool iterations without completing response."
