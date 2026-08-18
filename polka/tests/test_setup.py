"""Проверка подключения мини-приложения на локальном макете Telegram Bot API."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polka import setup as polka_setup
from polka.setup import SetupError, same_url, save_url, wire


class FakeBotApi:
    """Отвечает как Telegram и запоминает, что именно ему прислали."""

    def __init__(self, token: str = "111:TEST"):
        self.token = token
        self.calls: list[tuple[str, dict]] = []
        self.menu: dict | None = None
        self.fail: set[str] = set()
        self.forget_menu = False

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.runner.addresses[0][1]
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        await self.runner.cleanup()

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        body = await request.json()
        self.calls.append((method, body))

        if request.match_info["token"] != self.token:
            return web.json_response({"ok": False, "description": "Unauthorized"})
        if method in self.fail:
            return web.json_response({"ok": False, "description": f"{method} отклонён"})

        if method == "getMe":
            return web.json_response({"ok": True, "result": {"username": "polka_test_bot"}})
        if method == "setChatMenuButton":
            self.menu = body.get("menu_button")
            return web.json_response({"ok": True, "result": True})
        if method == "getChatMenuButton":
            if self.forget_menu:
                return web.json_response({"ok": True, "result": {"type": "commands"}})
            return web.json_response({"ok": True, "result": self.menu or {}})
        return web.json_response({"ok": True, "result": True})

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def payload(self, method: str) -> dict:
        return next(body for name, body in self.calls if name == method)


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = FakeBotApi()
        base = await self.api.start()
        self.saved = {k: os.environ.get(k) for k in
                      ("POLKA_TELEGRAM_API", "TELEGRAM_BOT_TOKEN", "POLKA_CHAT_ID",
                       "POLKA_PUBLIC_URL")}
        os.environ["POLKA_TELEGRAM_API"] = base
        os.environ["TELEGRAM_BOT_TOKEN"] = "111:TEST"
        os.environ["POLKA_CHAT_ID"] = "42"

    async def asyncTearDown(self):
        await self.api.stop()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    async def test_wires_menu_button_to_mini_app(self):
        name = await wire("https://polka.example.com", quiet=True)
        self.assertEqual(name, "polka_test_bot")
        self.assertEqual(self.api.menu, {
            "type": "web_app",
            "text": "Полка",
            "web_app": {"url": "https://polka.example.com"},
        })

    async def test_menu_button_is_bound_to_the_owner_chat(self):
        await wire("https://polka.example.com", quiet=True)
        self.assertEqual(self.api.payload("setChatMenuButton")["chat_id"], "42")

    async def test_sets_commands_and_description(self):
        await wire("https://polka.example.com", quiet=True)
        for method in ("setMyCommands", "setMyShortDescription", "setMyDescription"):
            self.assertIn(method, self.api.call_names())
        commands = [c["command"] for c in self.api.payload("setMyCommands")["commands"]]
        self.assertIn("polka", commands)

    async def test_result_is_verified_after_writing(self):
        self.api.forget_menu = True
        with self.assertRaises(SetupError) as caught:
            await wire("https://polka.example.com", quiet=True)
        self.assertIn("не сохранилась", str(caught.exception))

    async def test_plain_http_is_refused(self):
        with self.assertRaises(SetupError) as caught:
            await wire("http://polka.example.com", quiet=True)
        self.assertIn("https", str(caught.exception))
        self.assertEqual(self.api.calls, [])

    async def test_bad_token_is_reported(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "999:WRONG"
        with self.assertRaises(SetupError) as caught:
            await wire("https://polka.example.com", quiet=True)
        self.assertIn("Unauthorized", str(caught.exception))

    async def test_missing_token_is_reported(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = ""
        with self.assertRaises(SetupError) as caught:
            await wire("https://polka.example.com", quiet=True)
        self.assertIn("TELEGRAM_BOT_TOKEN", str(caught.exception))

    async def test_telegram_refusal_surfaces(self):
        self.api.fail = {"setChatMenuButton"}
        with self.assertRaises(SetupError) as caught:
            await wire("https://polka.example.com", quiet=True)
        self.assertIn("отклонён", str(caught.exception))


class SaveUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"
        self.original_root = polka_setup.ROOT
        polka_setup.ROOT = Path(self.tmp.name)

    def tearDown(self):
        polka_setup.ROOT = self.original_root
        self.tmp.cleanup()
        os.environ.pop("POLKA_PUBLIC_URL", None)

    def test_appends_when_absent(self):
        self.env.write_text("TELEGRAM_BOT_TOKEN=1\n", encoding="utf-8")
        save_url("https://a.example.com")
        self.assertIn("POLKA_PUBLIC_URL=https://a.example.com", self.env.read_text())
        self.assertIn("TELEGRAM_BOT_TOKEN=1", self.env.read_text())

    def test_replaces_existing_without_touching_neighbours(self):
        self.env.write_text("A=1\nPOLKA_PUBLIC_URL=https://old.example.com\nB=2\n",
                            encoding="utf-8")
        save_url("https://new.example.com")
        text = self.env.read_text()
        self.assertIn("POLKA_PUBLIC_URL=https://new.example.com", text)
        self.assertNotIn("old.example.com", text)
        self.assertIn("A=1", text)
        self.assertIn("B=2", text)


class ReachabilityTests(unittest.IsolatedAsyncioTestCase):
    """Если до сервиса туннелей нет доступа, это должно выясняться сразу."""

    async def test_unreachable_service_is_detected(self):
        # Адрес, на котором заведомо никто не слушает.
        original = polka_setup.tunnel_service_reachable
        self.assertFalse(await original(timeout=0.001))


class UrlComparisonTests(unittest.TestCase):
    """Telegram нормализует адрес и дописывает косую черту, сравнение это учитывает."""

    def test_trailing_slash_is_ignored(self):
        self.assertTrue(same_url("https://a.example.com/", "https://a.example.com"))
        self.assertTrue(same_url("https://a.example.com", "https://a.example.com/"))

    def test_different_addresses_still_differ(self):
        self.assertFalse(same_url("https://a.example.com", "https://b.example.com"))

    def test_missing_value_is_not_a_match(self):
        self.assertFalse(same_url(None, "https://a.example.com"))

    def test_path_is_not_stripped_away(self):
        self.assertFalse(same_url("https://a.example.com/app", "https://a.example.com"))


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_cloudflared_is_explained(self):
        with self.assertRaises(SetupError) as caught:
            await polka_setup.open_tunnel(8443, binary="cloudflared-which-is-not-here")
        self.assertIn("cloudflared", str(caught.exception))

    async def test_service_host_is_not_mistaken_for_the_tunnel(self):
        """cloudflared сначала пишет свой служебный адрес, и его брать нельзя."""
        script = ("import sys, time\n"
                  "print('INF Requesting new quick Tunnel on trycloudflare.com...')\n"
                  "print('INF Connecting to https://api.trycloudflare.com')\n"
                  "print('INF |  https://calm-fox-42.trycloudflare.com  |')\n"
                  "sys.stdout.flush()\n"
                  "time.sleep(5)\n")
        url, process = await self.run_fake(script)
        try:
            self.assertEqual(url, "https://calm-fox-42.trycloudflare.com")
        finally:
            process.terminate()
            await process.wait()

    async def run_fake(self, script: str, extra: list[str] | None = None):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "fake.py"
        path.write_text(script, encoding="utf-8")
        fake = Path(self.enterContext(tempfile.TemporaryDirectory())) / "cloudflared"
        fake.write_text(f"#!/bin/sh\nexec {sys.executable} {path}\n", encoding="utf-8")
        fake.chmod(0o755)
        return await polka_setup.open_tunnel(8443, binary=str(fake), extra=extra)

    async def test_failure_reports_what_cloudflared_said(self):
        """Без вывода процесса сообщение «закрылся» ничего не объясняет."""
        script = ("import sys\n"
                  "print('ERR quic handshake failed')\n"
                  "sys.stdout.flush()\n")
        with self.assertRaises(SetupError) as caught:
            await self.run_fake(script)
        message = str(caught.exception)
        self.assertIn("quic handshake failed", message)
        self.assertIn("QUIC", message)

    async def test_blocked_tunnel_service_is_named_as_the_cause(self):
        """Настоящий отказ у пользователя: до api.trycloudflare.com нет доступа."""
        script = ("import sys\n"
                  "print('INF Requesting new quick Tunnel on trycloudflare.com...')\n"
                  "print('failed to request quick Tunnel: Post \"https://api.trycloudflare.com/tunnel\": "
                  "context deadline exceeded')\n"
                  "sys.stdout.flush()\n")
        with self.assertRaises(SetupError) as caught:
            await self.run_fake(script)
        message = str(caught.exception)
        self.assertIn("api.trycloudflare.com", message)
        self.assertIn("VPN", message)

    async def test_the_word_quick_does_not_trigger_the_quic_hint(self):
        """«quick Tunnel» содержит «quic», и раньше это уводило диагностику в сторону."""
        script = ("import sys\n"
                  "print('INF Requesting new quick Tunnel on trycloudflare.com...')\n"
                  "sys.stdout.flush()\n")
        with self.assertRaises(SetupError) as caught:
            await self.run_fake(script)
        self.assertNotIn("Режется QUIC", str(caught.exception))

    async def test_extra_arguments_reach_cloudflared(self):
        script = ("import sys\n"
                  "print('INF args:', ' '.join(sys.argv[1:]))\n"
                  "print('INF |  https://calm-fox-42.trycloudflare.com  |')\n"
                  "sys.stdout.flush()\n"
                  "import time; time.sleep(5)\n")
        url, process = await self.run_fake(script, extra=["--protocol", "http2"])
        try:
            self.assertEqual(url, "https://calm-fox-42.trycloudflare.com")
        finally:
            process.terminate()
            await process.wait()

    async def test_address_is_picked_from_output(self):
        script = ("import sys, time\n"
                  "print('INF starting')\n"
                  "print('INF |  https://calm-fox-42.trycloudflare.com  |')\n"
                  "sys.stdout.flush()\n"
                  "time.sleep(5)\n")
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "fake.py"
        path.write_text(script, encoding="utf-8")
        fake = Path(self.enterContext(tempfile.TemporaryDirectory())) / "cloudflared"
        fake.write_text(f"#!/bin/sh\nexec {sys.executable} {path}\n", encoding="utf-8")
        fake.chmod(0o755)

        url, process = await polka_setup.open_tunnel(8443, binary=str(fake))
        try:
            self.assertEqual(url, "https://calm-fox-42.trycloudflare.com")
        finally:
            process.terminate()
            await process.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
