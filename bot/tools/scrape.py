import logging
from typing import Any

from bot.tools.base import BaseTool, ToolContext, ToolResult
from bot.utils.media import (
    WebFetchError,
    extract_media_resource,
    fetch_web_resource,
)

logger = logging.getLogger(__name__)


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
            "Fetch and read a URL based on its content type. Extracts text from HTML, "
            "PDF, and text documents, and sends supported images as visual input."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the webpage or file to read",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum extracted text characters (1-20000, default 6000)",
                },
            },
            "required": ["url"],
        }

    async def execute(
        self, context: ToolContext, url: str, max_chars: int = 6000, **kwargs
    ) -> str | ToolResult:
        if not url or not url.strip():
            return "Error: No URL provided to scrape."
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        try:
            max_chars = max(1, min(int(max_chars), 20_000))
        except (ValueError, TypeError):
            max_chars = 6000

        try:
            resource = await fetch_web_resource(url)
        except TimeoutError:
            return f"Timeout error: Request to {url} timed out."
        except WebFetchError as error:
            return f"Failed to fetch {url}: {error}"
        except Exception as error:
            logger.exception("Failed to fetch web resource url=%s", url)
            return f"Network error while fetching {url}: {error}"

        extracted = await extract_media_resource(
            resource,
            max_chars=max_chars,
            include_images=True,
        )
        if not extracted.multimodal_content:
            return extracted.text

        context.image_count += sum(
            part.get("type") == "image_url" for part in extracted.multimodal_content
        )
        return ToolResult(extracted.text, extracted.multimodal_content)
