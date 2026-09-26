"""Video normalization and frame extraction for multimodal model inputs."""

import base64
import json
import logging
import mimetypes
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_VIDEO_DURATION_SECONDS = 60
MAX_VIDEO_FRAMES = 12
MAX_VIDEO_DIMENSION = 1280
FFMPEG_TIMEOUT_SECONDS = 45
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg"}


class MediaProcessingError(ValueError):
    """Raised when media is unsupported or exceeds processing limits."""


@dataclass(frozen=True)
class PreparedVideo:
    part: dict[str, Any]
    fallback_frames: list[dict[str, Any]]


def is_animated_gif(data: bytes) -> bool:
    try:
        with Image.open(BytesIO(data)) as image:
            return image.format == "GIF" and getattr(image, "n_frames", 1) > 1
    except Exception:
        return False


def is_video_media(content_type: str = "", source: str = "") -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime.startswith("video/"):
        return True
    return Path(source.split("?", 1)[0]).suffix.lower() in VIDEO_EXTENSIONS


def _video_mime(data: bytes, source: str = "", content_type: str = "") -> str:
    content_mime = content_type.split(";", 1)[0].strip().lower()
    if content_mime.startswith("video/"):
        return content_mime
    suffix = Path(source.split("?", 1)[0]).suffix.lower()
    if suffix:
        guessed, _ = mimetypes.guess_type(source)
        if guessed and guessed.startswith("video/"):
            return guessed
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "video/x-msvideo"
    return content_mime or "application/octet-stream"


def video_data_part(data: bytes, mime_type: str = "video/mp4") -> dict[str, Any]:
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "video_url",
        "video_url": {"url": f"data:{mime_type};base64,{encoded}"},
    }


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise MediaProcessingError("FFmpeg is not installed.") from error
    except subprocess.TimeoutExpired as error:
        raise MediaProcessingError("Video processing exceeded its time limit.") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-500:]
        logger.debug("Media command failed: %s", detail)
        raise MediaProcessingError("The video could not be decoded or converted.")
    return result


def _duration_seconds(input_path: Path) -> float:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(input_path),
        ]
    )
    try:
        duration = float(json.loads(result.stdout).get("format", {}).get("duration"))
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise MediaProcessingError("The video duration could not be determined.") from error
    if duration <= 0:
        raise MediaProcessingError("The video has no playable duration.")
    if duration > MAX_VIDEO_DURATION_SECONDS:
        raise MediaProcessingError(
            f"The video exceeds the {MAX_VIDEO_DURATION_SECONDS}-second limit."
        )
    return duration


def _sample_frames(video_path: Path, duration: float) -> list[dict[str, Any]]:
    fps = min(MAX_VIDEO_FRAMES / duration, 30.0)
    result = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps:.8f},scale={MAX_VIDEO_DIMENSION}:{MAX_VIDEO_DIMENSION}:force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-frames:v",
            str(MAX_VIDEO_FRAMES),
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-pix_fmt",
            "yuvj420p",
            "pipe:1",
        ]
    )
    from bot.utils.media import image_data_part

    frames: list[dict[str, Any]] = []
    data = result.stdout
    start = 0
    while start < len(data):
        image_start = data.find(b"\xff\xd8", start)
        if image_start < 0:
            break
        image_end = data.find(b"\xff\xd9", image_start + 2)
        if image_end < 0:
            break
        frames.append(image_data_part(data[image_start : image_end + 2], "image/jpeg"))
        start = image_end + 2
    if not frames:
        raise MediaProcessingError("No readable frames could be extracted from the video.")
    return frames


def _prepare_video(data: bytes, content_type: str, source: str) -> PreparedVideo:
    if not data:
        raise MediaProcessingError("The video was empty.")
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaProcessingError(
            f"The media exceeds the {MAX_MEDIA_BYTES // (1024 * 1024)} MB limit."
        )
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise MediaProcessingError("FFmpeg is not installed.")

    mime = _video_mime(data, source, content_type)
    if not mime.startswith("video/"):
        mime = "image/gif" if is_animated_gif(data) else mime
    if not (mime.startswith("video/") or is_animated_gif(data)):
        raise MediaProcessingError("The media is not a supported video or animated GIF.")

    animated = is_animated_gif(data)
    suffix = ".gif" if animated else (Path(source.split("?", 1)[0]).suffix or ".video")
    with tempfile.TemporaryDirectory(prefix="bot-video-") as temp_dir:
        input_path = Path(temp_dir) / f"input{suffix}"
        output_path = Path(temp_dir) / "output.mp4"
        input_path.write_bytes(data)
        duration = _duration_seconds(input_path)
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-threads",
                "1",
                "-max_pixels",
                "8294400",
                "-i",
                str(input_path),
                "-t",
                str(MAX_VIDEO_DURATION_SECONDS),
                "-vf",
                f"scale={MAX_VIDEO_DIMENSION}:{MAX_VIDEO_DIMENSION}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                "-fs",
                str(MAX_MEDIA_BYTES),
                str(output_path),
            ]
        )
        if not output_path.exists() or output_path.stat().st_size > MAX_MEDIA_BYTES:
            raise MediaProcessingError("The converted video exceeds the 25 MB limit.")
        video_data = output_path.read_bytes()
        if not video_data:
            raise MediaProcessingError("Video conversion produced an empty file.")
        frames = _sample_frames(output_path, duration)
    return PreparedVideo(video_data_part(video_data), frames)


async def prepare_video(
    data: bytes,
    content_type: str = "",
    source: str = "",
) -> PreparedVideo:
    import asyncio

    return await asyncio.to_thread(_prepare_video, data, content_type, source)
