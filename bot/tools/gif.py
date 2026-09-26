import logging
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from bot.net import is_public_url, public_connector
from bot.tools.base import BaseTool, GeneratedImage, ToolContext, ToolRegistry

logger = logging.getLogger(__name__)

GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
GIPHY_RATING = "r"
MAX_QUERY_LENGTH = 200
MAX_GIF_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
CDN_HOST_RE = re.compile(r"^media\d*\.giphy\.com$", re.IGNORECASE)


class GifSendTool(BaseTool):
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    @property
    def name(self) -> str:
        return "gif_send"

    @property
    def display_name(self) -> str:
        return "Send GIF"

    @property
    def description(self) -> str:
        return "Search GIPHY and attach the most relevant animated GIF for a query."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the GIF should show or express",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(
        self, context: ToolContext, query: str, **kwargs: Any
    ) -> str:
        query = query.strip() if isinstance(query, str) else ""
        if not query:
            return "Error: A GIF search query is required."
        if len(query) > MAX_QUERY_LENGTH:
            return f"Error: GIF search queries must be {MAX_QUERY_LENGTH} characters or fewer."

        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(
                connector=public_connector(), timeout=timeout
            ) as session:
                async with session.get(
                    GIPHY_SEARCH_URL,
                    params={
                        "api_key": self.api_key,
                        "q": query,
                        "limit": 1,
                        "rating": GIPHY_RATING,
                    },
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            "GIPHY search failed request_id=%s status=%d",
                            context.request_id,
                            response.status,
                        )
                        return "Error: GIPHY could not search for a GIF right now."
                    payload = await response.json()

                gif_url = self._first_gif_url(payload)
                if not gif_url:
                    return f'No GIPHY results found for "{query}".'

                gif_data = await self._download_gif(session, gif_url)
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            logger.warning(
                "GIPHY request failed request_id=%s error=%s",
                context.request_id,
                type(error).__name__,
            )
            return "Error: GIPHY could not retrieve a GIF right now."
        except Exception as error:
            logger.warning(
                "GIPHY tool failed request_id=%s error=%s",
                context.request_id,
                type(error).__name__,
            )
            return "Error: GIPHY could not retrieve a GIF right now."

        context.generated_images.append(
            GeneratedImage(gif_data, "image/gif", "giphy.gif")
        )
        return f'Found a GIF for "{query}". It will be attached to the response.'

    @staticmethod
    def _first_gif_url(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        images = data[0].get("images")
        if not isinstance(images, dict):
            return None
        rendition = images.get("downsized_medium")
        if not isinstance(rendition, dict):
            return None
        url = rendition.get("url")
        if not isinstance(url, str) or not GifSendTool._is_allowed_cdn_url(url):
            return None
        return url

    @staticmethod
    def _is_allowed_cdn_url(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme == "https"
                and parsed.username is None
                and parsed.password is None
                and parsed.port in (None, 443)
                and parsed.hostname is not None
                and bool(CDN_HOST_RE.fullmatch(parsed.hostname))
                and is_public_url(url)
            )
        except ValueError:
            return False

    async def _download_gif(self, session: aiohttp.ClientSession, url: str) -> bytes:
        for redirect_count in range(MAX_REDIRECTS + 1):
            if not self._is_allowed_cdn_url(url):
                raise ValueError("GIPHY returned an invalid GIF URL")

            async with session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location or redirect_count == MAX_REDIRECTS:
                        raise ValueError("GIPHY GIF redirect limit exceeded")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ValueError("GIPHY GIF download failed")

                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        content_size = int(content_length)
                    except ValueError as error:
                        raise ValueError(
                            "GIPHY returned an invalid content length"
                        ) from error
                    if content_size > MAX_GIF_BYTES:
                        raise ValueError("GIPHY GIF exceeds the download limit")

                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_GIF_BYTES:
                        raise ValueError("GIPHY GIF exceeds the download limit")

                if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
                    raise ValueError("GIPHY returned an invalid GIF")
                return bytes(data)

        raise ValueError("GIPHY GIF redirect limit exceeded")


def register_gif_send_tool(registry: ToolRegistry, api_key: str) -> bool:
    if not (api_key := api_key.strip()):
        return False
    registry.register(GifSendTool(api_key))
    return True
