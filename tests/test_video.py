import base64
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from PIL import Image

from bot.agent.loop import AgenticLoop
from bot.discord.image import (
    format_turn_content,
    is_video_unsupported_error,
    merge_turn_contents,
    replace_video_parts_with_fallbacks,
    to_text_summary,
)
from bot.tools.base import ToolContext, ToolRegistry, ToolResult
from bot.tools.scrape import WebScrapeTool
from bot.utils.media import MediaResource, extract_media_resource
from bot.utils.video import (
    MAX_MEDIA_BYTES,
    MediaProcessingError,
    is_animated_gif,
    is_video_media,
    prepare_video,
)


def animated_gif(duration: int = 100) -> bytes:
    frames = [
        Image.new("RGB", (16, 16), "red"),
        Image.new("RGB", (16, 16), "blue"),
    ]
    output = BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0,
    )
    return output.getvalue()


class VideoUtilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_animated_gif_becomes_native_video_with_frame_fallback(self):
        data = animated_gif()
        self.assertTrue(is_animated_gif(data))

        prepared = await prepare_video(data, "image/gif", "animation.gif")

        self.assertEqual(prepared.part["type"], "video_url")
        payload = prepared.part["video_url"]["url"]
        self.assertTrue(payload.startswith("data:video/mp4;base64,"))
        self.assertEqual(base64.b64decode(payload.split(",", 1)[1])[4:8], b"ftyp")
        self.assertGreaterEqual(len(prepared.fallback_frames), 1)
        self.assertTrue(all(frame["type"] == "image_url" for frame in prepared.fallback_frames))

    async def test_extractor_preserves_video_and_fallback_frames(self):
        extracted = await extract_media_resource(
            MediaResource("clip.gif", "image/gif", None, animated_gif())
        )

        self.assertEqual(extracted.multimodal_content[0]["type"], "video_url")
        self.assertEqual(len(extracted.video_fallbacks), 1)

    async def test_oversized_video_is_rejected_before_conversion(self):
        with self.assertRaisesRegex(MediaProcessingError, "25 MB limit"):
            await prepare_video(b"x" * (MAX_MEDIA_BYTES + 1), "video/mp4", "clip.mp4")

    async def test_invalid_video_is_rejected(self):
        with self.assertRaises(MediaProcessingError):
            await prepare_video(b"not a video", "video/mp4", "clip.mp4")

    async def test_overlong_gif_is_rejected(self):
        with self.assertRaisesRegex(MediaProcessingError, "60-second limit"):
            await prepare_video(animated_gif(duration=61_000), "image/gif", "long.gif")

    async def test_still_gif_remains_an_image(self):
        output = BytesIO()
        Image.new("RGB", (16, 16), "green").save(output, format="GIF")
        self.assertFalse(is_animated_gif(output.getvalue()))
        extracted = await extract_media_resource(
            MediaResource("still.gif", "image/gif", None, output.getvalue())
        )
        self.assertEqual(extracted.multimodal_content[0]["type"], "image_url")

    def test_media_detection_uses_mime_and_extension(self):
        self.assertTrue(is_video_media("video/mp4", "unknown.bin"))
        self.assertTrue(is_video_media("", "clip.webm"))
        self.assertFalse(is_video_media("image/gif", "still.gif"))


class VideoContentTests(unittest.IsolatedAsyncioTestCase):
    def test_video_parts_survive_content_formatting_and_history_summary(self):
        video = {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AA=="}}
        content = format_turn_content("Inspect this", [video])
        merged = merge_turn_contents(content, "More context")

        self.assertIn(video, merged)
        self.assertIn("[1 Video(s)]", to_text_summary(merged))

    def test_unsupported_video_error_replaces_video_with_frames(self):
        self.assertTrue(is_video_unsupported_error("video_url is not supported"))
        frames = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}]
        video_url = "data:video/mp4;base64,AA=="
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": "Look"}, {
                "type": "video_url", "video_url": {"url": video_url}
            }],
        }]

        replaced = replace_video_parts_with_fallbacks(messages, {video_url: frames})

        self.assertTrue(replaced)
        self.assertEqual(messages[0]["content"][1:], frames)

    def test_unrelated_errors_do_not_trigger_video_fallback(self):
        self.assertFalse(is_video_unsupported_error("request timed out"))

    async def test_agent_retries_video_request_with_frames_once(self):
        video_url = "data:video/mp4;base64,AA=="
        frames = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}]
        messages = [{
            "role": "user",
            "content": [{"type": "video_url", "video_url": {"url": video_url}}],
        }]
        context = ToolContext(
            user_id=1,
            user_name="test",
            channel_id=2,
            video_fallbacks={video_url: frames},
        )
        loop = AgenticLoop("key", "https://example.org/v1", "model", ToolRegistry(), stream=False)
        loop._non_stream_step = AsyncMock(
            side_effect=[Exception("video_url is not supported"), ("answer", [])]
        )

        answer = await loop.run(messages, context)

        self.assertEqual(answer, "answer")
        self.assertTrue(context.video_input_fallback_used)
        self.assertEqual(loop._non_stream_step.await_count, 2)
        self.assertEqual(messages[0]["content"], frames)

    async def test_scrape_tool_returns_video_and_frame_fallbacks(self):
        resource = MediaResource("https://example.org/clip.gif", "image/gif", None, animated_gif())
        context = ToolContext(user_id=1, user_name="test", channel_id=2)
        with patch("bot.tools.scrape.fetch_web_resource", new_callable=AsyncMock, return_value=resource):
            result = await WebScrapeTool().execute(
                context, url="https://example.org/clip.gif"
            )

        self.assertIsInstance(result, ToolResult)
        self.assertEqual(result.multimodal_content[0]["type"], "video_url")
        self.assertEqual(len(result.video_fallbacks), 1)


if __name__ == "__main__":
    unittest.main()
