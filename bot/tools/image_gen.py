import base64
import binascii
import logging
import re
from io import BytesIO
from typing import Any

from openai import AsyncOpenAI
from openai.types.images_response import ImagesResponse
from PIL import Image

from bot.discord.image import url_to_image_part
from bot.tools.base import (
    BaseTool,
    GeneratedImage,
    ToolContext,
    ToolRegistry,
    ToolResult,
)
from bot.utils.media import detect_image_mime, prepare_image_for_model

logger = logging.getLogger(__name__)

DATA_URL_RE = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", re.IGNORECASE)
FILE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
}


def _get(value: Any, key: str, default: Any = None) -> Any:
    return (
        value.get(key, default)
        if isinstance(value, dict)
        else getattr(value, key, default)
    )


def _normalize_generated_image(
    data: bytes, mime_type: str = "", index: int = 1
) -> GeneratedImage | None:
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
    except Exception:
        return None

    prepared = prepare_image_for_model(data)
    if not prepared:
        return None

    normalized, normalized_mime = prepared
    detected_mime = detect_image_mime(normalized, normalized_mime or mime_type)
    extension = FILE_EXTENSIONS.get(detected_mime)
    if not extension:
        return None
    return GeneratedImage(
        normalized, detected_mime, f"generated-image-{index}.{extension}"
    )


def _image_sources(response: Any) -> list[Any]:
    sources = list(_get(response, "data", []) or [])
    choices = _get(response, "choices", []) or []
    message = _get(choices[0], "message") if choices else None

    items = list(_get(message, "images", []) or [])
    content = _get(message, "content", [])
    if isinstance(content, list):
        items.extend(p for p in content if _get(p, "type") in {"image_url", "image"})

    for item in items:
        url = _get(item, "image_url")
        sources.append(_get(url, "url", url) or _get(item, "data") or item)
    return [s for s in sources if s]


def _decode_source(source: Any) -> tuple[bytes, str] | None:
    encoded = _get(source, "b64_json") or _get(source, "data")
    mime = _get(source, "media_type", "")
    if not encoded:
        url = source if isinstance(source, str) else _get(source, "url")
        if not isinstance(url, str) or not (match := DATA_URL_RE.match(url)):
            return None
        mime, encoded = match.group(1).lower(), match.group(2)
    try:
        return base64.b64decode(encoded, validate=True), mime
    except (ValueError, binascii.Error):
        return None


class ImageGenTool(BaseTool):
    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    @property
    def name(self) -> str:
        return "image_gen"

    @property
    def display_name(self) -> str:
        return "Image Generation"

    @property
    def description(self) -> str:
        return (
            "Generate an image from a description, or use images attached to the "
            "current message as references for an edit."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Describe the image to create or how to edit the attached image(s)",
                }
            },
            "required": ["prompt"],
            "additionalProperties": False,
        }

    async def execute(
        self, context: ToolContext, prompt: str, **kwargs: Any
    ) -> str | ToolResult:
        prompt = prompt.strip()
        if not prompt:
            return "Error: An image description is required."

        input_references = [
            url
            for part in context.current_image_parts
            if part.get("type") == "image_url"
            and isinstance(url := part.get("image_url", {}).get("url"), str)
        ]
        body = {"model": self.model, "prompt": prompt}
        if input_references:
            body["input_references"] = input_references

        try:
            response = await self.client.post(
                "/images",
                body=body,
                cast_to=ImagesResponse,
            )
        except Exception as error:
            logger.exception(
                "Image generation failed request_id=%s model=%s",
                context.request_id,
                self.model,
            )
            return f"Error: Image generation failed: {error}"

        images: list[GeneratedImage] = []
        for source in _image_sources(response):
            if isinstance(source, str) and source.startswith(("http://", "https://")):
                image_part = await url_to_image_part(source)
                source = (
                    image_part.get("image_url", {}).get("url") if image_part else None
                )
            if (decoded := _decode_source(source)) and (
                image := _normalize_generated_image(*decoded, index=len(images) + 1)
            ):
                images.append(image)

        if not images:
            return "Error: The image model did not return a valid image."

        context.generated_images.extend(images)
        return ToolResult(
            f"Generated {len(images)} image(s). They will be attached to your Discord response."
        )


def register_image_gen_tool(
    registry: ToolRegistry,
    model: str,
    api_key: str,
    base_url: str,
) -> bool:
    if not (model := model.strip()):
        return False
    registry.register(ImageGenTool(api_key=api_key, base_url=base_url, model=model))
    return True
