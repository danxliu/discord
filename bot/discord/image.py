import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import discord

from bot.utils import media as _media
from bot.utils import video as _video
from bot.utils.media import (
    ExtractedContent,
    MediaResource,
    extract_media_resource,
)
from bot.utils.video import MAX_MEDIA_BYTES, is_video_media

is_video_unsupported_error = _video.is_video_unsupported_error
replace_video_parts_with_fallbacks = _video.replace_video_parts_with_fallbacks

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DISCORD_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}


def is_discord_attachment_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in DISCORD_CDN_HOSTS
            and parsed.path.startswith("/attachments/")
        )
    except Exception:
        return False


def canonical_attachment_url(url: str) -> str:
    if not is_discord_attachment_url(url):
        return url
    try:
        parsed = urlsplit(url)
        return urlunsplit(("https", "cdn.discordapp.com", parsed.path, "", ""))
    except Exception:
        return url


def detect_image_mime(
    data: bytes, filename_or_url: str = "", default: str = "image/jpeg"
) -> str:
    return _media.detect_image_mime(
        data, default=default, filename_or_url=filename_or_url
    )


def is_image_attachment(attachment: discord.Attachment) -> bool:
    """Check if a Discord attachment is an image based on content type or filename extension."""
    if attachment.content_type:
        mime = attachment.content_type.split(";")[0].strip().lower()
        if mime.startswith("image/"):
            return True
    if getattr(attachment, "width", None) and getattr(attachment, "height", None):
        return True
    ext = Path(attachment.filename).suffix.lower()
    return ext in IMAGE_EXTENSIONS


def is_video_attachment(attachment: discord.Attachment) -> bool:
    return (
        is_video_media(
            getattr(attachment, "content_type", None) or "",
            getattr(attachment, "filename", ""),
        )
        or Path(getattr(attachment, "filename", "")).suffix.lower() == ".gif"
    )


fetch_web_resource = _media.fetch_web_resource
url_to_image_part = _media.url_to_image_part


def _media_result_parts(
    extracted: ExtractedContent,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    parts = list(extracted.multimodal_content or [])
    if not extracted.multimodal_content and extracted.text:
        parts.append({"type": "text", "text": extracted.text})
    return parts, extracted.video_fallbacks or {}


async def load_media_from_message(
    msg: discord.Message,
    seen_urls: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Extract media from a message and retain frame fallbacks for videos."""
    seen = seen_urls if seen_urls is not None else set()
    parts: list[dict[str, Any]] = []
    fallbacks: dict[str, list[dict[str, Any]]] = {}

    target_attachments: list[discord.Attachment] = []
    for att in msg.attachments:
        canon_url = canonical_attachment_url(att.url)
        if canon_url not in seen and (
            is_image_attachment(att) or is_video_attachment(att)
        ):
            seen.add(canon_url)
            target_attachments.append(att)

    async def _process_attachment(
        attachment: discord.Attachment,
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        local_parts: list[dict[str, Any]] = []
        local_fallbacks: dict[str, list[dict[str, Any]]] = {}
        try:
            if getattr(attachment, "size", 0) > MAX_MEDIA_BYTES:
                logger.warning(
                    "Skipping oversized Discord media filename=%s", attachment.filename
                )
                media_type = "Video" if is_video_attachment(attachment) else "Image"
                local_parts.append(
                    {
                        "type": "text",
                        "text": (
                            f"{media_type} {attachment.filename} was not attached because it "
                            "exceeds the 25 MB limit."
                        ),
                    }
                )
                return local_parts, local_fallbacks

            data = await attachment.read()
            if not data or len(data) > MAX_MEDIA_BYTES:
                media_type = "Video" if is_video_attachment(attachment) else "Image"
                local_parts.append(
                    {
                        "type": "text",
                        "text": (
                            f"{media_type} {attachment.filename} was not attached because it "
                            "is empty or exceeds the 25 MB limit."
                        ),
                    }
                )
                return local_parts, local_fallbacks

            extracted = await extract_media_resource(
                MediaResource(
                    f"Discord attachment {attachment.filename}",
                    getattr(attachment, "content_type", None) or "",
                    None,
                    data,
                )
            )
            extracted_parts, extracted_fallbacks = _media_result_parts(extracted)
            local_parts.extend(extracted_parts)
            local_fallbacks.update(extracted_fallbacks)
        except Exception:
            logger.exception(
                "Failed to load Discord media filename=%s", attachment.filename
            )
            media_type = "Video" if is_video_attachment(attachment) else "Image"
            local_parts.append(
                {
                    "type": "text",
                    "text": f"{media_type} {attachment.filename} could not be processed.",
                }
            )
        return local_parts, local_fallbacks

    for attachment in target_attachments:
        attachment_parts, attachment_fallbacks = await _process_attachment(attachment)
        parts.extend(attachment_parts)
        fallbacks.update(attachment_fallbacks)

    return parts, fallbacks


def format_turn_content(
    text: str,
    image_parts: Sequence[dict[str, Any]] | None = None,
) -> str | list[dict[str, Any]]:
    """Format turn content as a string if no images, or as a list of content parts if images are present."""
    if not image_parts:
        return text
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(image_parts)
    return parts


def to_text_summary(content: str | list[dict[str, Any]]) -> str:
    """Convert turn content into plain text for in-memory channel history or logging without large base64 data."""
    if isinstance(content, str):
        return content
    texts = [p.get("text", "") for p in content if p.get("type") == "text"]
    num_images = sum(1 for p in content if p.get("type") == "image_url")
    num_videos = sum(1 for p in content if p.get("type") == "video_url")
    if num_images > 0 and not any("[Image" in t for t in texts):
        texts.append(f"[{num_images} Image(s)]")
    if num_videos > 0:
        texts.append(f"[{num_videos} Video(s)]")
    return "\n".join(t for t in texts if t).strip()


def is_vision_unsupported_error(err: str) -> bool:
    """Detect if an API error occurred because the target model lacks vision/multimodal capabilities."""
    err_lower = err.lower()
    unsupported_messages = (
        "does not support image",
        "doesn't support image",
        "image input is not supported",
        "image_url is not supported",
        "does not support vision",
        "not a vision model",
        "multimodal is not supported",
    )
    is_schema_mismatch = (
        "expected a string" in err_lower and "array" in err_lower
    ) or ("expected string" in err_lower and "list" in err_lower)
    return is_schema_mismatch or any(
        message in err_lower for message in unsupported_messages
    )


def convert_messages_to_text_only(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert a message list containing multimodal image parts into pure text-only messages."""
    converted: list[dict[str, Any]] = []
    for msg in messages:
        item = dict(msg)
        content = item.get("content")
        if isinstance(content, list):
            item["content"] = to_text_summary(content)
        converted.append(item)
    return converted
