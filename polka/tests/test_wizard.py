"""Проверка мастера настройки на локальном макете Telegram Bot API."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polka import wizard
from polka.wizard import check_bot, find_chat_id, write_env


class FakeBotApi:
    def __init__(self, token: str = "111:TEST"):
        self.token = token
        self.updates: list[dict] = []

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def stop(self) -> None:
        await self.runner.cleanup()

    async def handle(self, request: web.Request) -> web.Response:
        if request.match_info["token"] != self.token:
            return web.json_response({"ok": False, "description": "Unauthorized"})
        method = request.match_info["method"]
        if method == "getMe":
            return web.json_response({"ok": True, "result": {"username": "polka_test_bot"}})
        if method == "getUpdates":
            return web.json_response({"ok": True, "result": self.updates})
        return web.json_response({"ok": True, "result": True})


class BotCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiohttp
        self.api = FakeBotApi()
        self.base = await self.api.start()
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.api.stop()

    async def test_good_token_returns_name(self):
        self.assertEqual(await check_bot(self.session, self.base, "111:TEST"),
                         "polka_test_bot")

    async def test_bad_token_returns_none(self):
        self.assertIsNone(await check_bot(self.session, self.base, "999:NOPE"))

    async def test_unreachable_api_returns_none(self):
        self.assertIsNone(
            await check_bot(self.session, "http://127.0.0.1:1", "111:TEST"))

    async def test_chat_id_is_taken_from_latest_message(self):
        self.api.updates = [
            {"message": {"chat": {"id": 111}}},
            {"message": {"chat": {"id": 222}}},
        ]
        self.assertEqual(await find_chat_id(self.session, self.base, "111:TEST"), "222")

    async def test_no_messages_gives_none(self):
        self.assertIsNone(await find_chat_id(self.session, self.base, "111:TEST"))

    async def test_updates_without_chat_are_skipped(self):
        self.api.updates = [{"edited_message": {}}, {"message": {}}]
        self.assertIsNone(await find_chat_id(self.session, self.base, "111:TEST"))


class WriteEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"
        self.original = wizard.ENV
        wizard.ENV = self.env

    def tearDown(self):
        wizard.ENV = self.original
        for key in ("TELEGRAM_BOT_TOKEN", "POLKA_CHAT_ID", "ANTHROPIC_API_KEY"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_creates_file_when_missing(self):
        write_env({"TELEGRAM_BOT_TOKEN": "1:AA", "POLKA_CHAT_ID": "42"})
        text = self.env.read_text()
        self.assertIn("TELEGRAM_BOT_TOKEN=1:AA", text)
        self.assertIn("POLKA_CHAT_ID=42", text)

    def test_replaces_in_place_and_keeps_the_rest(self):
        self.env.write_text(
            "# комментарий\nTELEGRAM_BOT_TOKEN=старый\nPOLKA_QUIET_FROM=23\n",
            encoding="utf-8")
        write_env({"TELEGRAM_BOT_TOKEN": "новый", "POLKA_CHAT_ID": "42"})
        text = self.env.read_text()
        self.assertIn("TELEGRAM_BOT_TOKEN=новый", text)
        self.assertNotIn("старый", text)
        self.assertIn("# комментарий", text)
        self.assertIn("POLKA_QUIET_FROM=23", text)
        self.assertIn("POLKA_CHAT_ID=42", text)

    def test_values_reach_the_environment(self):
        write_env({"POLKA_CHAT_ID": "42"})
        self.assertEqual(os.environ["POLKA_CHAT_ID"], "42")


if __name__ == "__main__":
    unittest.main(verbosity=2)
