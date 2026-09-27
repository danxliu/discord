import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import discord

from bot.utils import media as _media
from bot.utils import video as _video
from bot.utils.media import (
    ExtractedContent,
    MediaResource,
    extract_media_resource,
    fetch_web_resource,
    image_data_part,
)
from bot.utils.video import MAX_MEDIA_BYTES, is_animated_gif, is_video_media

is_video_unsupported_error = _video.is_video_unsupported_error
replace_video_parts_with_fallbacks = _video.replace_video_parts_with_fallbacks

logger = logging.getLogger(__name__)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
URL_REGEX = re.compile(r"https?://[^\s<>\"'()]+")


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


def is_image_url(url: str) -> bool:
    """Check if a URL points to an image based on its path extension."""
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        return any(path.endswith(ext) for ext in IMAGE_EXTENSIONS)
    except ValueError:
        return False


async def attachment_to_image_part(
    attachment: discord.Attachment,
) -> dict[str, Any] | None:
    """Download a Discord attachment and convert it to an OpenAI multimodal image_url part."""
    try:
        data = await attachment.read()
        if not data:
            return None

        mime = (
            attachment.content_type.split(";")[0].strip().lower()
            if attachment.content_type
            else None
        )
        if not mime or not mime.startswith("image/"):
            mime = detect_image_mime(
                data, default="image/jpeg", filename_or_url=attachment.filename
            )

        return image_data_part(data, mime)
    except Exception as e:
        logger.warning("Failed to download attachment %s: %s", attachment.filename, e)
        return None


async def url_to_image_part(
    url: str,
) -> dict[str, Any] | None:
    """Download a public image URL and convert it to an image content part."""
    try:
        resource = await fetch_web_resource(
            url, timeout_seconds=10, max_bytes=MAX_MEDIA_BYTES
        )
        mime = detect_image_mime(resource.data, filename_or_url=url, default="")
        if not mime or not mime.startswith("image/"):
            logger.warning("URL %s did not return a supported image", url)
            return None
        return image_data_part(resource.data, mime)
    except Exception as e:
        logger.warning("Failed to download image URL %s: %s", url, e)
        return None


def extract_image_urls_from_text_and_embeds(
    content: str,
    embeds: Sequence[discord.Embed],
    seen_urls: set[str] | None = None,
) -> list[str]:
    """Find image URLs inside message content text and Discord embeds."""
    seen = seen_urls if seen_urls is not None else set()
    found: list[str] = []

    for embed in embeds:
        for u in (
            getattr(embed.image, "url", None),
            getattr(embed.thumbnail, "url", None),
        ):
            if u and u not in seen:
                seen.add(u)
                found.append(u)

    if content:
        for raw_url in URL_REGEX.findall(content):
            url = raw_url.rstrip(".,!?;:")
            if is_image_url(url) and url not in seen:
                seen.add(url)
                found.append(url)

    return found


def extract_video_urls_from_text_and_embeds(
    content: str,
    embeds: Sequence[discord.Embed],
    seen_urls: set[str] | None = None,
) -> list[str]:
    seen = seen_urls if seen_urls is not None else set()
    embed_urls = [
        url
        for embed in embeds
        if (url := getattr(embed.video, "url", None)) is not None
    ]
    embed_url_set = set(embed_urls)
    candidates = list(embed_urls)
    candidates.extend(URL_REGEX.findall(content or ""))
    found: list[str] = []
    extensions = {
        ".mp4",
        ".mov",
        ".m4v",
        ".webm",
        ".mkv",
        ".avi",
        ".mpeg",
        ".mpg",
        ".gif",
    }
    for candidate in candidates:
        if not candidate:
            continue
        url = candidate.rstrip(".,!?;:")
        try:
            suffix = Path(urlparse(url).path).suffix.lower()
        except ValueError:
            continue
        if (suffix in extensions or url in embed_url_set) and url not in seen:
            seen.add(url)
            found.append(url)
    return found


def _media_result_parts(
    extracted: ExtractedContent,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    parts = list(extracted.multimodal_content or [])
    if not extracted.multimodal_content and extracted.text.startswith(
        "Could not process"
    ):
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

    target_attachments = [
        att
        for att in msg.attachments
        if att.url not in seen
        and (is_image_attachment(att) or is_video_attachment(att))
    ]
    for att in target_attachments:
        seen.add(att.url)

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
                if is_video_attachment(attachment):
                    local_parts.append(
                        {
                            "type": "text",
                            "text": (
                                f"Video {attachment.filename} was not attached because it "
                                "exceeds the 25 MB limit."
                            ),
                        }
                    )
                return local_parts, local_fallbacks

            data = await attachment.read()
            if not data or len(data) > MAX_MEDIA_BYTES:
                if is_video_attachment(attachment):
                    local_parts.append(
                        {
                            "type": "text",
                            "text": (
                                f"Video {attachment.filename} was not attached because it "
                                "is empty or exceeds the 25 MB limit."
                            ),
                        }
                    )
                return local_parts, local_fallbacks

            if is_video_attachment(attachment) or is_animated_gif(data):
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
            else:
                local_parts.append(
                    image_data_part(
                        data,
                        detect_image_mime(data, filename_or_url=attachment.filename),
                    )
                )
        except Exception:
            logger.exception(
                "Failed to load Discord media filename=%s", attachment.filename
            )
            if is_video_attachment(attachment):
                local_parts.append(
                    {
                        "type": "text",
                        "text": f"Video {attachment.filename} could not be processed.",
                    }
                )
        return local_parts, local_fallbacks

    video_urls = extract_video_urls_from_text_and_embeds(msg.content, msg.embeds, seen)
    image_urls = extract_image_urls_from_text_and_embeds(msg.content, msg.embeds, seen)

    async def _process_video_url(
        url: str,
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        local_parts: list[dict[str, Any]] = []
        local_fallbacks: dict[str, list[dict[str, Any]]] = {}
        try:
            resource = await fetch_web_resource(url, max_bytes=MAX_MEDIA_BYTES)
            extracted = await extract_media_resource(resource)
            extracted_parts, extracted_fallbacks = _media_result_parts(extracted)
            local_parts.extend(extracted_parts)
            local_fallbacks.update(extracted_fallbacks)
        except Exception as error:
            logger.warning("Failed to load linked video URL=%s error=%s", url, error)
            local_parts.append(
                {
                    "type": "text",
                    "text": f"Linked video {url} could not be loaded: {error}",
                }
            )
        return local_parts, local_fallbacks

    for attachment in target_attachments:
        attachment_parts, attachment_fallbacks = await _process_attachment(attachment)
        parts.extend(attachment_parts)
        fallbacks.update(attachment_fallbacks)

    for url in image_urls:
        if part := await url_to_image_part(url):
            parts.append(part)

    for url in video_urls:
        video_parts, video_fallbacks = await _process_video_url(url)
        parts.extend(video_parts)
        fallbacks.update(video_fallbacks)

    return parts, fallbacks


async def load_images_from_message(
    msg: discord.Message,
    seen_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    parts, _ = await load_media_from_message(msg, seen_urls)
    return [part for part in parts if part.get("type") == "image_url"]


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


def merge_turn_contents(
    c1: str | list[dict[str, Any]],
    c2: str | list[dict[str, Any]],
) -> str | list[dict[str, Any]]:
    """Merge two turn contents cleanly, preserving both text and images."""
    if isinstance(c1, str) and isinstance(c2, str):
        return f"{c1}\n\n{c2}"

    t1 = (
        c1
        if isinstance(c1, str)
        else "\n".join(p["text"] for p in c1 if p.get("type") == "text")
    )
    imgs1 = (
        []
        if isinstance(c1, str)
        else [p for p in c1 if p.get("type") in {"image_url", "video_url"}]
    )

    t2 = (
        c2
        if isinstance(c2, str)
        else "\n".join(p["text"] for p in c2 if p.get("type") == "text")
    )
    imgs2 = (
        []
        if isinstance(c2, str)
        else [p for p in c2 if p.get("type") in {"image_url", "video_url"}]
    )

    combined_text = f"{t1}\n\n{t2}".strip() if t1 and t2 else (t1 or t2)
    combined_images = imgs1 + imgs2

    return format_turn_content(combined_text, combined_images)


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
