import asyncio
from typing import Any, Dict
from ddgs import DDGS
from bot.tools.base import BaseTool, ToolContext


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
            "Use search_type='news' for recent events, 'images' for image URLs, or 'general' for web search."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
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
    ) -> str:
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
                source_url = item.get("url", "")
                lines.append(
                    f"{i}. [{title}]({source_url})\n   Direct Image: {image_url}\n"
                )
            else:
                title = item.get("title", "No Title")
                href = item.get("href", "")
                body = item.get("body", "")
                lines.append(f"{i}. [{title}]({href})\n   {body}\n")

        return "\n".join(lines).strip()
