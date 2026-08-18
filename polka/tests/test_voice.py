"""Расшифровка голосовых: выбор провайдера и разбор ответа."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polka.bot import Bot
from polka.config import Config


def make_config(**overrides) -> Config:
    base = dict(
        bot_token="1:AA", chat_id="42", anthropic_key="", model="claude-opus-5",
        openai_key="", groq_key="", capture_token="", public_url="",
        host="127.0.0.1", port=0, quiet_from=0, quiet_to=0, timezone_offset=3,
        telegram_api="https://api.telegram.org", voice_api="",
    )
    base.update(overrides)
    return Config(**base)


class ProviderChoiceTests(unittest.TestCase):
    def test_nothing_configured_means_no_voice(self):
        self.assertIsNone(make_config().voice)

    def test_groq_is_preferred_because_it_is_free(self):
        cfg = make_config(groq_key="gsk_1", openai_key="sk_1")
        url, key, model = cfg.voice
        self.assertIn("groq.com", url)
        self.assertEqual(key, "gsk_1")
        self.assertEqual(model, "whisper-large-v3-turbo")

    def test_openai_is_used_when_alone(self):
        url, key, model = make_config(openai_key="sk_1").voice
        self.assertIn("openai.com", url)
        self.assertEqual(model, "whisper-1")


class FakeVoiceService:
    """Отвечает как Whisper и запоминает, что ему прислали."""

    def __init__(self):
        self.status = 200
        self.model: str | None = None
        self.audio: bytes | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/transcriptions", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}/transcriptions"

    async def stop(self) -> None:
        await self.runner.cleanup()

    async def handle(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "model":
                self.model = (await part.read()).decode()
            elif part.name == "file":
                self.audio = await part.read()
        if self.status != 200:
            return web.Response(status=self.status, text="нет")
        return web.json_response({"text": "  надо продлить домен  "})


class FakeTelegram:
    def __init__(self, audio: bytes | None):
        self.audio = audio

    async def download_file(self, file_id: str) -> bytes | None:
        return self.audio


class TranscribeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = FakeVoiceService()
        self.url = await self.service.start()
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.service.stop()

    def make_bot(self, audio: bytes | None = b"OggS-fake", **cfg_overrides) -> Bot:
        cfg = make_config(voice_api=self.url, **cfg_overrides)
        return Bot(cfg, store=None, flow=None, tg=FakeTelegram(audio),
                   session=self.session)

    async def test_text_is_returned_and_trimmed(self):
        bot = self.make_bot(groq_key="gsk_1")
        self.assertEqual(await bot.transcribe("file-1"), "надо продлить домен")
        self.assertEqual(self.service.model, "whisper-large-v3-turbo")
        self.assertEqual(self.service.audio, b"OggS-fake")

    async def test_openai_key_sends_its_own_model(self):
        bot = self.make_bot(openai_key="sk_1")
        await bot.transcribe("file-1")
        self.assertEqual(self.service.model, "whisper-1")

    async def test_no_key_means_no_call(self):
        self.assertIsNone(await self.make_bot().transcribe("file-1"))
        self.assertIsNone(self.service.audio)

    async def test_missing_audio_is_handled(self):
        bot = self.make_bot(audio=None, groq_key="gsk_1")
        self.assertIsNone(await bot.transcribe("file-1"))

    async def test_service_error_does_not_raise(self):
        self.service.status = 500
        bot = self.make_bot(groq_key="gsk_1")
        self.assertIsNone(await bot.transcribe("file-1"))

    async def test_empty_file_id_is_ignored(self):
        bot = self.make_bot(groq_key="gsk_1")
        self.assertIsNone(await bot.transcribe(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
