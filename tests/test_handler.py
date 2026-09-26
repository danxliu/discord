import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.discord.handler import _execute_chat_pipeline, handle_message_event
from bot.tools.base import GeneratedImage


class FakeStreamer:
    def __init__(self):
        self.feed = AsyncMock()
        self.finalize = AsyncMock()
        self.stop = AsyncMock()


class HandlerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_answer_is_finalized_before_generated_images(self):
        streamer = FakeStreamer()
        answer = "Here is the image."
        order = []
        streamer.finalize.side_effect = lambda text: order.append(("finalize", text))

        async def deliver(_images):
            order.append(("images", None))

        user = SimpleNamespace(
            id=1, name="tester", display_name="Tester", status=None
        )
        loop = SimpleNamespace(tool_registry=None)

        async def run(messages, context, **kwargs):
            context.generated_images.append(
                GeneratedImage(b"image", "image/png", "generated.png")
            )
            return answer

        loop.run = AsyncMock(side_effect=run)
        with patch("bot.discord.handler.build_system_prompt", return_value="system"):
            await _execute_chat_pipeline(
                user=user,
                channel=None,
                guild=None,
                client_user=None,
                prompt="make an image",
                request_id="request",
                streamer=streamer,
                agent_loop=loop,
                on_status=AsyncMock(),
                on_error=AsyncMock(),
                on_generated_images=deliver,
            )

        self.assertEqual(order, [("finalize", answer), ("images", None)])
        streamer.finalize.assert_awaited_once_with(answer)
        streamer.stop.assert_not_awaited()

    async def test_error_text_is_reported_before_generated_images_are_attached(self):
        user = SimpleNamespace(
            id=1, name="tester", display_name="Tester", status=None
        )
        loop = SimpleNamespace(tool_registry=None)
        streamer = FakeStreamer()
        events = []

        async def run(messages, context, **kwargs):
            context.generated_images.append(
                GeneratedImage(b"image", "image/png", "generated.png")
            )
            raise RuntimeError("response failed")

        async def on_error(text):
            events.append(("error", text))

        async def on_generated_images(_images):
            events.append(("images", None))

        loop.run = AsyncMock(side_effect=run)
        with patch("bot.discord.handler.build_system_prompt", return_value="system"):
            result = await _execute_chat_pipeline(
                user=user,
                channel=None,
                guild=None,
                client_user=None,
                prompt="make an image",
                request_id="request",
                streamer=streamer,
                agent_loop=loop,
                on_status=AsyncMock(),
                on_error=on_error,
                on_generated_images=on_generated_images,
            )

        self.assertFalse(result)
        self.assertEqual(events, [("error", "response failed"), ("images", None)])
        streamer.stop.assert_awaited_once_with(delete_followups=True)

    async def test_non_streamed_long_answer_is_sent_before_generated_images(self):
        answer = "x" * 2101
        user = SimpleNamespace(
            id=1, name="tester", display_name="Tester", status=None
        )
        loop = SimpleNamespace(tool_registry=None)

        async def run(messages, context, **kwargs):
            context.generated_images.append(
                GeneratedImage(b"image", "image/png", "generated.png")
            )
            return answer

        loop.run = AsyncMock(side_effect=run)
        on_generated_images = AsyncMock()
        on_fallback_send = AsyncMock()
        with patch("bot.discord.handler.build_system_prompt", return_value="system"):
            await _execute_chat_pipeline(
                user=user,
                channel=None,
                guild=None,
                client_user=None,
                prompt="make an image",
                request_id="request",
                streamer=None,
                agent_loop=loop,
                on_status=AsyncMock(),
                on_error=AsyncMock(),
                on_fallback_send=on_fallback_send,
                on_generated_images=on_generated_images,
            )

        chunks = on_fallback_send.await_args.args[0]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), answer)

    async def _run_message_delivery(self, *, upload_fails=False):
        user = SimpleNamespace(id=1, name="tester", display_name="Tester", status=None)
        status = SimpleNamespace(id=22, edit=AsyncMock())
        if upload_fails:
            status.edit.side_effect = RuntimeError("upload failed")
        channel = SimpleNamespace(id=2)
        message = SimpleNamespace(
            id=11,
            author=user,
            channel=channel,
            guild=None,
            content="make an image",
            attachments=[],
            reference=None,
        )

        async def execute(**kwargs):
            await kwargs["on_generated_images"](
                [GeneratedImage(b"image", "image/png", "generated.png")]
            )

        with (
            patch("bot.discord.handler.should_respond", return_value=True),
            patch("bot.discord.handler.clean_prompt", return_value="make an image"),
            patch(
                "bot.discord.handler.load_media_from_message",
                new_callable=AsyncMock,
                return_value=([], {}),
            ),
            patch(
                "bot.discord.handler.DiscordMessenger.safe_send",
                new_callable=AsyncMock,
                return_value=status,
            ) as safe_send,
            patch(
                "bot.discord.handler._execute_chat_pipeline",
                new_callable=AsyncMock,
                side_effect=execute,
            ),
        ):
            await handle_message_event(message, None, SimpleNamespace())

        return status, safe_send

    async def test_generated_files_are_added_without_clearing_response_content(self):
        status, _ = await self._run_message_delivery()

        status.edit.assert_awaited_once()
        kwargs = status.edit.await_args.kwargs
        self.assertNotIn("content", kwargs)
        self.assertEqual(kwargs["attachments"][0].filename, "generated.png")

    async def test_upload_failure_warns_separately_without_replacing_response(self):
        status, safe_send = await self._run_message_delivery(upload_fails=True)

        status.edit.assert_awaited_once()
        self.assertNotIn("content", status.edit.await_args.kwargs)
        self.assertEqual(safe_send.await_count, 2)
        warning = safe_send.await_args_list[1].args[1]
        self.assertIn("could not attach", warning)
