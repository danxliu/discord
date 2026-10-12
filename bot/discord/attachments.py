import logging
from typing import Any

import aiohttp

from bot.net import get_public_session
from bot.utils.media import MediaResource, extract_media_resource
from config import settings

logger = logging.getLogger(__name__)


async def attachment_to_text(
    attachment: Any,
    *,
    max_chars: int = 6000,
) -> str:
    filename = getattr(attachment, "filename", "attachment")

    try:
        data = await attachment.read()
    except Exception:
        logger.exception("Failed to read Discord attachment filename=%s", filename)
        return f"Could not read attachment {filename}."

    if not data:
        return f"Attachment {filename} was empty."

    extracted = await extract_media_resource(
        MediaResource(
            source=f"Discord attachment {filename}",
            content_type=getattr(attachment, "content_type", None) or "",
            charset=None,
            data=data,
        ),
        max_chars=max_chars,
        include_images=False,
    )
    return extracted.text


DISCORD_API_REFRESH_URL = "https://discord.com/api/v10/attachments/refresh-urls"


async def refresh_discord_attachment_urls(
    urls: list[str],
    bot_token: str | None = None,
) -> dict[str, str]:
    token = bot_token or getattr(settings, "discord_token", None)
    if not token or not urls:
        return {}

    url_list = list(dict.fromkeys(urls))
    refreshed_map: dict[str, str] = {}
    timeout = aiohttp.ClientTimeout(total=10)

    try:
        session = await get_public_session()
        for i in range(0, len(url_list), 50):
            batch = url_list[i : i + 50]
            headers = {
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
            }
            async with session.post(
                DISCORD_API_REFRESH_URL,
                headers=headers,
                json={"attachment_urls": batch},
                timeout=timeout,
            ) as response:
                if response.status == 200:
                    payload = await response.json()
                    for item in payload.get("refreshed_urls", []):
                        orig = item.get("original")
                        refreshed = item.get("refreshed")
                        if orig and refreshed:
                            refreshed_map[orig] = refreshed
                else:
                    logger.warning(
                        "Discord attachment URL refresh failed status=%d",
                        response.status,
                    )
    except Exception:
        logger.exception("Error while refreshing Discord attachment URLs")

    return refreshed_map
