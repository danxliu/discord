"""Image extraction, validation, and encoding utilities for multimodal AI interactions."""

import logging
import mimetypes
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
import discord

from bot.net import is_public_url, public_connector
from bot.utils.media import (
    MediaResource,
    extract_media_resource,
    fetch_web_resource,
    image_data_part,
)
from bot.utils.video import MAX_MEDIA_BYTES, is_animated_gif, is_video_media

logger = logging.getLogger(__name__)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
URL_REGEX = re.compile(r"https?://[^\s<>\"'()]+")
IMAGE_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
}


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
    return is_video_media(
        getattr(attachment, "content_type", None) or "",
        getattr(attachment, "filename", ""),
    ) or Path(getattr(attachment, "filename", "")).suffix.lower() == ".gif"


def is_image_url(url: str) -> bool:
    """Check if a URL points to an image based on its path extension."""
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        return any(path.endswith(ext) for ext in IMAGE_EXTENSIONS)
    except ValueError:
        return False


def detect_image_mime(
    data: bytes, filename_or_url: str = "", default: str = "image/jpeg"
) -> str:
    """Detect image MIME type from magic bytes with fallback to filename extension."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"

    guessed, _ = mimetypes.guess_type(filename_or_url)
    if guessed and guessed.startswith("image/"):
        return guessed
    return default


def _image_part(data: bytes | bytearray, mime: str) -> dict[str, Any]:
    return image_data_part(data, mime)


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
            mime = detect_image_mime(data, attachment.filename)

        return _image_part(data, mime)
    except Exception as e:
        logger.warning("Failed to download attachment %s: %s", attachment.filename, e)
        return None


async def url_to_image_part(
    url: str,
) -> dict[str, Any] | None:
    """Download a public image URL and convert it to an image content part."""
    session = None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        connector = public_connector()
        session = aiohttp.ClientSession(
            headers=IMAGE_FETCH_HEADERS, connector=connector, timeout=timeout
        )
        for _ in range(6):
            if not is_public_url(url):
                logger.warning("Refusing to fetch a non-public image URL")
                return None
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("Location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                if resp.status != 200:
                    logger.warning(
                        "Image URL fetch returned HTTP %d for %s", resp.status, url
                    )
                    return None

                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_MEDIA_BYTES:
                        logger.warning("Image URL exceeded the media size limit: %s", url)
                        return None

                if not data:
                    return None

                mime = detect_image_mime(data, default="")
                if not mime:
                    logger.warning("URL %s did not return a supported image", url)
                    return None

                return _image_part(data, mime)
        return None
    except Exception as e:
        logger.warning("Failed to download image URL: %s", e)
        return None
    finally:
        if session:
            await session.close()


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
    embed_urls = {getattr(embed.video, "url", None) for embed in embeds}
    embed_urls.discard(None)
    candidates = list(embed_urls)
    candidates.extend(URL_REGEX.findall(content or ""))
    found: list[str] = []
    extensions = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".gif"}
    for candidate in candidates:
        if not candidate:
            continue
        url = candidate.rstrip(".,!?;:")
        try:
            suffix = Path(urlparse(url).path).suffix.lower()
        except ValueError:
            continue
        if (suffix in extensions or url in embed_urls) and url not in seen:
            seen.add(url)
            found.append(url)
    return found


async def load_media_from_message(
    msg: discord.Message,
    seen_urls: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Extract media from a message and retain frame fallbacks for videos."""
    seen = seen_urls if seen_urls is not None else set()
    parts: list[dict[str, Any]] = []
    fallbacks: dict[str, list[dict[str, Any]]] = {}

    def add_note(text: str) -> None:
        parts.append({"type": "text", "text": text})

    for attachment in msg.attachments:
        if attachment.url in seen or not (
            is_image_attachment(attachment) or is_video_attachment(attachment)
        ):
            continue
        seen.add(attachment.url)
        try:
            if getattr(attachment, "size", 0) > MAX_MEDIA_BYTES:
                logger.warning("Skipping oversized Discord media filename=%s", attachment.filename)
                if is_video_attachment(attachment):
                    add_note(
                        f"Video {attachment.filename} was not attached because it exceeds the 25 MB limit."
                    )
                continue
            data = await attachment.read()
            if not data or len(data) > MAX_MEDIA_BYTES:
                if is_video_attachment(attachment):
                    add_note(
                        f"Video {attachment.filename} was not attached because it is empty or exceeds the 25 MB limit."
                    )
                continue
            if is_video_attachment(attachment) or is_animated_gif(data):
                extracted = await extract_media_resource(
                    MediaResource(
                        f"Discord attachment {attachment.filename}",
                        getattr(attachment, "content_type", None) or "",
                        None,
                        data,
                    )
                )
                parts.extend(extracted.multimodal_content or [])
                if not extracted.multimodal_content and extracted.text.startswith("Could not process"):
                    add_note(extracted.text)
                fallbacks.update(extracted.video_fallbacks or {})
            else:
                parts.append(_image_part(data, detect_image_mime(data, attachment.filename)))
        except Exception:
            logger.exception("Failed to load Discord media filename=%s", attachment.filename)
            if is_video_attachment(attachment):
                add_note(f"Video {attachment.filename} could not be processed.")

    video_urls = extract_video_urls_from_text_and_embeds(msg.content, msg.embeds, seen)
    image_urls = extract_image_urls_from_text_and_embeds(msg.content, msg.embeds, seen)
    for url in image_urls:
        part = await url_to_image_part(url)
        if part:
            parts.append(part)
    for url in video_urls:
        try:
            resource = await fetch_web_resource(url, max_bytes=MAX_MEDIA_BYTES)
            extracted = await extract_media_resource(resource)
            parts.extend(extracted.multimodal_content or [])
            if not extracted.multimodal_content and extracted.text.startswith("Could not process"):
                add_note(extracted.text)
            fallbacks.update(extracted.video_fallbacks or {})
        except Exception as error:
            logger.warning("Failed to load linked video URL=%s error=%s", url, error)
            add_note(f"Linked video {url} could not be loaded: {error}")
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


def is_video_unsupported_error(err: str) -> bool:
    err_lower = err.lower()
    return "video_url" in err_lower or any(
        phrase in err_lower
        for phrase in (
            "video input is not supported",
            "does not support video",
            "doesn't support video",
            "video is not supported",
            "unsupported video input",
        )
    )


def replace_video_parts_with_fallbacks(
    messages: list[dict[str, Any]],
    fallbacks: dict[str, list[dict[str, Any]]],
) -> bool:
    replaced = False
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        new_content: list[dict[str, Any]] = []
        for part in content:
            if part.get("type") != "video_url":
                new_content.append(part)
                continue
            url = part.get("video_url", {}).get("url")
            frames = fallbacks.get(url) if isinstance(url, str) else None
            if not frames:
                new_content.append(part)
                continue
            new_content.extend(frames)
            replaced = True
        message["content"] = new_content
    return replaced


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
