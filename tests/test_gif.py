import unittest
from unittest.mock import patch

from bot.tools import GifSendTool, ToolContext, ToolRegistry, register_gif_send_tool
from bot.tools.gif import GIPHY_SEARCH_URL, MAX_GIF_BYTES

GIF_BYTES = b"GIF89a" + b"\x00" * 6
GIF_URL = "https://media.giphy.com/media/example/giphy-downsized.gif"


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, chunks=()):
        self.status = status
        self.payload = payload
        self.headers = headers or {}
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class GifSendToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = ToolContext(user_id=1, user_name="tester", channel_id=2)
        self.payload = {
            "data": [
                {"images": {"downsized_medium": {"url": GIF_URL}}},
                {"images": {"downsized_medium": {"url": "https://media.giphy.com/other.gif"}}},
            ]
        }

    async def test_sends_top_gif_and_searches_with_expected_parameters(self):
        session = FakeSession(
            [
                FakeResponse(payload=self.payload),
                FakeResponse(chunks=[GIF_BYTES]),
            ]
        )
        with (
            patch("bot.tools.gif.aiohttp.ClientSession", return_value=session),
            patch("bot.tools.gif.public_connector", return_value=None),
        ):
            result = await GifSendTool("secret").execute(self.context, "  dancing  ")

        self.assertIn("will be attached", result)
        self.assertEqual(session.calls[0][0], GIPHY_SEARCH_URL)
        self.assertEqual(
            session.calls[0][1]["params"],
            {"api_key": "secret", "q": "dancing", "limit": 1, "rating": "r"},
        )
        self.assertEqual(session.calls[1][0], GIF_URL)
        self.assertEqual(self.context.generated_images[0].data, GIF_BYTES)
        self.assertEqual(self.context.generated_images[0].mime_type, "image/gif")
        self.assertEqual(self.context.generated_images[0].filename, "giphy.gif")

    async def test_handles_no_results_and_does_not_download(self):
        session = FakeSession([FakeResponse(payload={"data": []})])
        with (
            patch("bot.tools.gif.aiohttp.ClientSession", return_value=session),
            patch("bot.tools.gif.public_connector", return_value=None),
        ):
            result = await GifSendTool("secret").execute(self.context, "missing")

        self.assertIn("No GIPHY results", result)
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(self.context.generated_images)

    async def test_handles_search_http_failure(self):
        session = FakeSession([FakeResponse(status=403)])
        with (
            patch("bot.tools.gif.aiohttp.ClientSession", return_value=session),
            patch("bot.tools.gif.public_connector", return_value=None),
        ):
            result = await GifSendTool("secret").execute(self.context, "cat")

        self.assertTrue(result.startswith("Error:"))
        self.assertFalse(self.context.generated_images)

    async def test_rejects_untrusted_rendition_url(self):
        payload = {"data": [{"images": {"downsized_medium": {"url": "http://example.com/gif.gif"}}}]}
        session = FakeSession([FakeResponse(payload=payload)])
        with (
            patch("bot.tools.gif.aiohttp.ClientSession", return_value=session),
            patch("bot.tools.gif.public_connector", return_value=None),
        ):
            result = await GifSendTool("secret").execute(self.context, "cat")

        self.assertIn("No GIPHY results", result)
        self.assertEqual(len(session.calls), 1)

    async def test_handles_timeout_without_raising(self):
        session = FakeSession([TimeoutError()])
        with (
            patch("bot.tools.gif.aiohttp.ClientSession", return_value=session),
            patch("bot.tools.gif.public_connector", return_value=None),
        ):
            result = await GifSendTool("secret").execute(self.context, "cat")

        self.assertTrue(result.startswith("Error:"))
        self.assertFalse(self.context.generated_images)

    async def test_rejects_external_redirect(self):
        session = FakeSession(
            [
                FakeResponse(payload=self.payload),
                FakeResponse(status=302, headers={"Location": "https://example.com/gif.gif"}),
            ]
        )
        with (
            patch("bot.tools.gif.aiohttp.ClientSession", return_value=session),
            patch("bot.tools.gif.public_connector", return_value=None),
        ):
            result = await GifSendTool("secret").execute(self.context, "cat")

        self.assertTrue(result.startswith("Error:"))
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(self.context.generated_images)

    async def test_rejects_oversized_gif(self):
        session = FakeSession(
            [
                FakeResponse(payload=self.payload),
                FakeResponse(chunks=[b"GIF89a", b"\x00" * MAX_GIF_BYTES]),
            ]
        )
        with (
            patch("bot.tools.gif.aiohttp.ClientSession", return_value=session),
            patch("bot.tools.gif.public_connector", return_value=None),
        ):
            result = await GifSendTool("secret").execute(self.context, "cat")

        self.assertTrue(result.startswith("Error:"))
        self.assertFalse(self.context.generated_images)

    async def test_rejects_invalid_query(self):
        result = await GifSendTool("secret").execute(self.context, "  ")
        self.assertTrue(result.startswith("Error:"))
        self.assertFalse(self.context.generated_images)

    def test_tool_schema_requires_query(self):
        schema = GifSendTool("secret").to_openai_schema()["function"]
        self.assertEqual(schema["name"], "gif_send")
        self.assertEqual(schema["parameters"]["required"], ["query"])

    def test_registration_requires_api_key(self):
        registry = ToolRegistry()
        self.assertFalse(register_gif_send_tool(registry, "  "))
        self.assertIsNone(registry.get("gif_send"))

        self.assertTrue(register_gif_send_tool(registry, "secret"))
        self.assertIsInstance(registry.get("gif_send"), GifSendTool)
