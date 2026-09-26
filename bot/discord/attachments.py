import logging
from typing import Any

from bot.utils.media import MediaResource, extract_media_resource

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
