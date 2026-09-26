"""Fetch public resources and extract their content according to media type."""

import asyncio
import base64
import logging
import mimetypes
import ssl
from dataclasses import dataclass
from email.message import Message
from io import BytesIO
from typing import Any
from urllib.parse import urljoin

import aiohttp
import certifi
import trafilatura
from PIL import Image, ImageOps
from pypdf import PdfReader

from bot.net import is_public_url, public_connector

logger = logging.getLogger(__name__)

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/pdf,image/*,text/*,"
        "application/json,application/xml,*/*;q=0.8"
    ),
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class MediaResource:
    source: str
    content_type: str
    charset: str | None
    data: bytes


@dataclass(frozen=True)
class ExtractedContent:
    text: str
    multimodal_content: list[dict[str, Any]] | None = None


class WebFetchError(Exception):
    pass


async def fetch_web_resource(
    url: str,
    *,
    timeout_seconds: float = 15,
) -> MediaResource:
    if not is_public_url(url):
        raise WebFetchError("Refusing to fetch a non-public URL.")

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    current_url = url

    async with aiohttp.ClientSession(
        headers=FETCH_HEADERS,
        connector=public_connector(ssl_context),
        timeout=timeout,
    ) as session:
        for redirect_count in range(6):
            if not is_public_url(current_url):
                raise WebFetchError("Refusing to fetch a non-public redirect URL.")

            async with session.get(current_url, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise WebFetchError("Redirect has no target.")
                    if redirect_count == 5:
                        raise WebFetchError("Too many redirects.")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status != 200:
                    raise WebFetchError(f"HTTP {response.status}")

                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)

                if not data:
                    raise WebFetchError("The URL returned an empty response.")

                raw_content_type = response.headers.get("Content-Type", "")
                content_header = Message()
                if raw_content_type:
                    content_header["content-type"] = raw_content_type
                return MediaResource(
                    source=current_url,
                    content_type=(
                        content_header.get_content_type().lower()
                        if raw_content_type
                        else "application/octet-stream"
                    ),
                    charset=content_header.get_content_charset(),
                    data=bytes(data),
                )

    raise WebFetchError("Too many redirects.")


def detect_image_mime(data: bytes, default: str = "") -> str:
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
    return default


def prepare_image_for_model(
    data: bytes | bytearray,
    max_dimension: int = 2048,
    jpeg_quality: int = 85,
) -> tuple[bytes, str] | None:
    """Validate, orient, resize, and normalize an image for LLM vision models.

    Converts raw image bytes into a clean, normalized, optimized JPEG or PNG.
    Guarantees that oversized camera photos, unhandled formats (WEBP, BMP, TIFF),
    or corrupt payloads do not crash OpenAI/OpenRouter providers with
    'Provider returned an empty response'.
    """
    if not data:
        return None

    try:
        with Image.open(BytesIO(data)) as img:
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass

            if getattr(img, "is_animated", False):
                try:
                    img.seek(0)
                except Exception:
                    pass

            if img.width > max_dimension or img.height > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

            output_buf = BytesIO()

            # Preserve transparency if PNG with alpha channel
            if img.format == "PNG" and img.mode in ("RGBA", "LA", "P"):
                img.save(output_buf, format="PNG", optimize=True)
                return output_buf.getvalue(), "image/png"

            # If mode has transparency, composite over white background for JPEG
            if img.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                alpha = img.split()[-1]
                bg.paste(img, mask=alpha)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.save(output_buf, format="JPEG", quality=jpeg_quality, optimize=True)
            return output_buf.getvalue(), "image/jpeg"
    except Exception as e:
        logger.debug("Image normalization skipped or not recognized by Pillow: %s", e)
        data_bytes = bytes(data)
        detected = detect_image_mime(data_bytes)
        if detected:
            return data_bytes, detected
        return None


def image_data_part(data: bytes | bytearray, mime_type: str = "image/jpeg") -> dict[str, Any]:
    prepared = prepare_image_for_model(data)
    if prepared is not None:
        data_bytes, final_mime = prepared
    else:
        data_bytes, final_mime = bytes(data), mime_type

    encoded = base64.b64encode(data_bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{final_mime};base64,{encoded}"},
    }


def _media_type(resource: MediaResource) -> str:
    if resource.data.startswith(b"%PDF-"):
        return "application/pdf"

    image_mime = detect_image_mime(resource.data)
    if image_mime:
        return image_mime

    content_type = resource.content_type
    if content_type and content_type != "application/octet-stream":
        return content_type

    guessed, _ = mimetypes.guess_type(resource.source)
    return guessed or content_type or "application/octet-stream"


def _decode_text(data: bytes, charset: str | None) -> str:
    try:
        return data.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _extract_pdf_text(data: bytes, max_chars: int) -> str:
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            return "This PDF is password-protected; its text could not be extracted."

        pages: list[str] = []
        total_chars = 0
        page_limit_reached = False
        for page_number, page in enumerate(reader.pages, start=1):
            if page_number > 200:
                page_limit_reached = True
                break
            page_text = (page.extract_text() or "").strip()
            if not page_text:
                continue
            remaining = max_chars - total_chars
            pages.append(f"Page {page_number}\n{page_text[:remaining]}")
            total_chars += min(len(page_text), remaining)
            if total_chars >= max_chars:
                break

        if not pages:
            return (
                "This PDF contains no extractable text. It may be scanned; OCR is not "
                "available."
            )
        text = "\n\n".join(pages)
        if total_chars >= max_chars:
            text += f"\n\n[Content truncated at {max_chars} characters]"
        elif page_limit_reached:
            text += "\n\n[Content truncated after 200 pages]"
        return text
    except Exception:
        return "Could not extract text from this PDF."


async def extract_media_resource(
    resource: MediaResource,
    *,
    max_chars: int = 6000,
    include_images: bool = True,
) -> ExtractedContent:
    max_chars = max(1, int(max_chars))
    media_type = _media_type(resource)
    if media_type.startswith("image/"):
        image_mime = detect_image_mime(resource.data)
        if not image_mime:
            return ExtractedContent(
                f"The resource contains an unsupported or invalid image format "
                f"({media_type}); image data was not attached."
            )
        text = f"Image input from {resource.source} ({image_mime})."
        if not include_images:
            return ExtractedContent(
                f"{text} Image data was not attached for this request."
            )
        return ExtractedContent(
            text,
            [image_data_part(resource.data, image_mime)],
        )

    if media_type == "application/pdf":
        text = await asyncio.to_thread(_extract_pdf_text, resource.data, max_chars)
        return ExtractedContent(f"### PDF from {resource.source}\n\n{text}")

    if media_type in {"text/html", "application/xhtml+xml"}:
        html = _decode_text(resource.data, resource.charset)
        extracted = await asyncio.to_thread(
            trafilatura.extract,
            html,
            output_format="markdown",
            include_links=True,
        )
        if not extracted or not extracted.strip():
            return ExtractedContent(
                f"Could not extract readable page content from {resource.source}."
            )
        text = extracted.strip()
    elif (
        media_type.startswith("text/")
        or media_type
        in {
            "application/json",
            "application/xml",
            "application/javascript",
        }
        or media_type.endswith(("+json", "+xml"))
    ):
        text = _decode_text(resource.data, resource.charset).strip()
    else:
        return ExtractedContent(
            f"The resource has unsupported content type {media_type}; "
            "binary data was not sent as text."
        )

    if not text:
        return ExtractedContent(f"No readable content found at {resource.source}.")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[Content truncated at {max_chars} characters]"
    return ExtractedContent(f"### Content from {resource.source}:\n\n{text}")
