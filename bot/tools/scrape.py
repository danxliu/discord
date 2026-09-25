import asyncio
import ssl
from typing import Any, Dict
import aiohttp
import certifi
import trafilatura
from bot.tools.base import BaseTool, ToolContext

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class WebScrapeTool(BaseTool):
    @property
    def name(self) -> str:
        return "web_scrape"

    @property
    def display_name(self) -> str:
        return "Web Scrape"

    @property
    def description(self) -> str:
        return (
            "Extract clean, readable markdown content from a specific webpage URL."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the webpage to scrape and read",
                }
            },
            "required": ["url"],
        }

    async def execute(
        self, context: ToolContext, url: str, max_chars: int = 6000, **kwargs
    ) -> str:
        if not url or not url.strip():
            return "Error: No URL provided to scrape."
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        try:
            max_chars = int(max_chars if max_chars is not None else 6000)
        except (ValueError, TypeError):
            max_chars = 6000

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        timeout = aiohttp.ClientTimeout(total=15)

        try:
            async with aiohttp.ClientSession(
                headers=HEADERS, connector=connector, timeout=timeout
            ) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        return f"Failed to fetch {url}: HTTP {resp.status}"
                    html = await resp.text()
        except asyncio.TimeoutError:
            return f"Timeout error: Request to {url} timed out after 15 seconds."
        except Exception as e:
            return f"Network error while fetching {url}: {str(e)}"

        extracted = await asyncio.to_thread(
            trafilatura.extract,
            html,
            output_format="markdown",
            include_links=True,
        )

        if not extracted or not extracted.strip():
            return f"Could not extract readable main content from {url}."

        cleaned = extracted.strip()
        if len(cleaned) > max_chars:
            cleaned = (
                cleaned[:max_chars]
                + f"\n\n[Content truncated at {max_chars} characters]"
            )

        return f"### Content from {url}:\n\n{cleaned}"
