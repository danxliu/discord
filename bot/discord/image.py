"""Image extraction, validation, and encoding utilities for multimodal AI interactions."""

import asyncio
import base64
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

logger = logging.getLogger(__name__)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
URL_REGEX = re.compile(r"https?://[^\s<>\"'()]+")


def is_image_attachment(attachment: discord.Attachment) -> bool:
    """Check if a Discord attachment is an image based on content type or filename extension."""
    if attachment.content_type:
        mime = attachment.content_type.split(";")[0].strip().lower()
        if mime.startswith("image/"):
            return True
    ext = Path(attachment.filename).suffix.lower()
    return ext in IMAGE_EXTENSIONS


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
    encoded = base64.b64encode(data).decode("utf-8")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


async def attachment_to_image_part(
    attachment: discord.Attachment,
    max_size_bytes: int = 20 * 1024 * 1024,
) -> dict[str, Any] | None:
    """Download a Discord attachment and convert it to an OpenAI multimodal image_url part."""
    if attachment.size and attachment.size > max_size_bytes:
        logger.warning(
            "Attachment %s exceeds size limit (%d > %d bytes)",
            attachment.filename,
            attachment.size,
            max_size_bytes,
        )
        return None

    try:
        data = await attachment.read()
        if not data or len(data) > max_size_bytes:
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
    max_size_bytes: int = 20 * 1024 * 1024,
) -> dict[str, Any] | None:
    """Download a public image URL and convert it to an image content part."""
    session = None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        connector = public_connector()
        session = aiohttp.ClientSession(connector=connector, timeout=timeout)
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
                    return None
                if (
                    resp.content_length is not None
                    and resp.content_length > max_size_bytes
                ):
                    logger.warning("Image URL %s exceeds size limit", url)
                    return None

                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if len(data) + len(chunk) > max_size_bytes:
                        logger.warning("Image URL %s exceeds size limit", url)
                        return None
                    data.extend(chunk)

                if not data:
                    return None

                content_type = resp.headers.get("Content-Type", "")
                mime = (
                    content_type.split(";")[0].strip().lower() if content_type else None
                )
                if not mime or not mime.startswith("image/"):
                    mime = detect_image_mime(data, url)

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


async def load_images_from_message(
    msg: discord.Message,
    max_images: int = 5,
    max_size_bytes: int = 20 * 1024 * 1024,
    seen_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Extract and download images from a single message up to max_images limit."""
    if max_images <= 0:
        return []

    seen = seen_urls if seen_urls is not None else set()
    tasks = []

    for a in msg.attachments:
        if len(tasks) >= max_images:
            break
        if is_image_attachment(a) and a.url not in seen:
            seen.add(a.url)
            tasks.append(attachment_to_image_part(a, max_size_bytes=max_size_bytes))

    if len(tasks) < max_images:
        url_targets = extract_image_urls_from_text_and_embeds(
            msg.content, msg.embeds, seen
        )
        for u in url_targets[: max_images - len(tasks)]:
            tasks.append(url_to_image_part(u, max_size_bytes=max_size_bytes))

    if not tasks:
        return []

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict) and r.get("type") == "image_url"]


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
        return f"{c1}\n{c2}"

    t1 = (
        c1
        if isinstance(c1, str)
        else "\n".join(p["text"] for p in c1 if p.get("type") == "text")
    )
    imgs1 = (
        [] if isinstance(c1, str) else [p for p in c1 if p.get("type") == "image_url"]
    )

    t2 = (
        c2
        if isinstance(c2, str)
        else "\n".join(p["text"] for p in c2 if p.get("type") == "text")
    )
    imgs2 = (
        [] if isinstance(c2, str) else [p for p in c2 if p.get("type") == "image_url"]
    )

    combined_text = f"{t1}\n{t2}".strip() if t1 and t2 else (t1 or t2)
    combined_images = imgs1 + imgs2

    return format_turn_content(combined_text, combined_images)


def to_text_summary(content: str | list[dict[str, Any]]) -> str:
    """Convert turn content into plain text for in-memory channel history or logging without large base64 data."""
    if isinstance(content, str):
        return content
    texts = [p.get("text", "") for p in content if p.get("type") == "text"]
    num_images = sum(1 for p in content if p.get("type") == "image_url")
    if num_images > 0 and not any("[Image" in t for t in texts):
        texts.append(f"[{num_images} Image(s)]")
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
