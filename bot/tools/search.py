import asyncio
import logging
from typing import Any

from ddgs import DDGS

from bot.tools.base import BaseTool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class WebSearchTool(BaseTool):
    @property
    def name(self) -> str:
        return "web_search"

    @property
    def display_name(self) -> str:
        return "Web Search"

    @property
    def description(self) -> str:
        return (
            "Search the web for up-to-date information, news, or images. "
            "Use search_type='news' for recent events, 'images' to find images and send fetched visual results to the model, or 'general' for web search."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query keywords",
                },
                "search_type": {
                    "type": "string",
                    "enum": ["general", "news", "images"],
                    "description": "Type of search: 'general', 'news', or 'images'. Defaults to 'general'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (1-10, default 5)",
                },
            },
            "required": ["query"],
        }

    def _sync_search(
        self, query: str, search_type: str, max_results: int
    ) -> list[dict]:
        with DDGS() as ddgs:
            if search_type == "news":
                return list(ddgs.news(query, max_results=max_results))
            elif search_type == "images":
                return list(ddgs.images(query, max_results=max_results))
            else:
                return list(ddgs.text(query, max_results=max_results))

    async def execute(
        self,
        context: ToolContext,
        query: str,
        search_type: str = "general",
        max_results: int = 5,
        **kwargs,
    ) -> str | ToolResult:
        search_type = (search_type or "general").lower().strip()
        try:
            max_results = max(1, min(int(max_results or 5), 10))
        except (ValueError, TypeError):
            max_results = 5

        results = await asyncio.to_thread(
            self._sync_search, query, search_type, max_results
        )

        if not results:
            return f"No results found for query: '{query}' ({search_type})."

        lines = [f"### Web Search Results for '{query}' ({search_type}):\n"]
        image_results: list[tuple[str, str, list[str]]] = []
        for i, item in enumerate(results, start=1):
            if search_type == "news":
                title = item.get("title", "No Title")
                url = item.get("url", "")
                date = item.get("date", "")
                source = item.get("source", "")
                body = item.get("body", "")
                lines.append(
                    f"{i}. [{title}]({url})\n   Source: {source} | Date: {date}\n   {body}\n"
                )
            elif search_type == "images":
                title = item.get("title", "Image")
                image_url = item.get("image", "")
                thumbnail_url = item.get("thumbnail", "")
                source_url = item.get("url", "")
                lines.append(
                    f"{i}. [{title}]({source_url})\n   Direct Image: {image_url}\n"
                )
                remaining_images = max(0, context.max_images - context.image_count)
                if image_url and len(image_results) < remaining_images:
                    candidates = [image_url]
                    if thumbnail_url and thumbnail_url != image_url:
                        candidates.append(thumbnail_url)
                    image_results.append((title, source_url, candidates))
            else:
                title = item.get("title", "No Title")
                href = item.get("href", "")
                body = item.get("body", "")
                lines.append(f"{i}. [{title}]({href})\n   {body}\n")

        text_result = "\n".join(lines).strip()
        if search_type != "images" or not image_results:
            return text_result

        # Delay import: bot.discord imports the handler, which imports bot.tools.
        from bot.discord.image import url_to_image_part

        async def fetch_image(
            candidates: list[str],
        ) -> tuple[str, dict[str, Any]] | None:
            for image_url in candidates:
                image_part = await url_to_image_part(
                    image_url, max_size_bytes=context.max_image_size_bytes
                )
                if isinstance(image_part, dict):
                    return image_url, image_part
            return None

        fetched_results = await asyncio.gather(
            *(fetch_image(candidates) for _, _, candidates in image_results),
            return_exceptions=True,
        )
        multimodal_content: list[dict[str, Any]] = []
        for (title, source_url, _), fetched in zip(image_results, fetched_results):
            if not isinstance(fetched, tuple):
                continue
            image_url, image_part = fetched
            multimodal_content.extend(
                [
                    {
                        "type": "text",
                        "text": f"Image search result: {title}\nSource: {source_url}\nImage URL: {image_url}",
                    },
                    image_part,
                ]
            )

        image_count = sum(
            part.get("type") == "image_url" for part in multimodal_content
        )
        logger.info(
            "Image search downloads completed query=%r fetched=%d attempted=%d",
            query,
            image_count,
            len(image_results),
        )
        if not image_count:
            logger.warning("No image data fetched for image search query=%r", query)
            return f"{text_result}\n\n[No image data was fetched; these results are URLs only.]"
        context.image_count += image_count
        return ToolResult(text_result, multimodal_content)
