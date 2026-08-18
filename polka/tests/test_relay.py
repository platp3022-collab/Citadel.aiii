"""Приём мыслей с айфона через доску объявлений."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polka import relay
from polka.config import Config
from polka.relay import extract


def make_config(**overrides) -> Config:
    base = dict(
        bot_token="1:AA", chat_id="42", anthropic_key="", model="claude-opus-5",
        openai_key="", groq_key="", capture_token="", public_url="",
        host="127.0.0.1", port=0, quiet_from=0, quiet_to=0, timezone_offset=3,
        telegram_api="https://api.telegram.org", voice_api="", voice_reply=False,
        tts_api="", relay_url="", relay_topic="polka-test",
    )
    base.update(overrides)
    return Config(**base)


class ExtractTests(unittest.TestCase):
    """В потоке кроме мыслей идут служебные события, их нельзя принимать за текст."""

    def test_message_is_taken(self):
        line = json.dumps({"event": "message", "message": "  продлить домен  "})
        self.assertEqual(extract(line.encode()), "продлить домен")

    def test_open_event_is_skipped(self):
        self.assertIsNone(extract(json.dumps({"event": "open"}).encode()))

    def test_keepalive_is_skipped(self):
        self.assertIsNone(extract(json.dumps({"event": "keepalive"}).encode()))

    def test_empty_message_is_skipped(self):
        line = json.dumps({"event": "message", "message": "   "})
        self.assertIsNone(extract(line.encode()))

    def test_blank_line_is_skipped(self):
        self.assertIsNone(extract(b"\n"))

    def test_broken_json_does_not_raise(self):
        self.assertIsNone(extract("{ это не json".encode()))

    def test_non_utf8_does_not_raise(self):
        self.assertIsNone(extract(b"\xff\xfe\x00"))


class FakeBoard:
    """Доска объявлений: отдаёт заранее подготовленные строки и закрывает поток."""

    def __init__(self, lines: list[bytes], status: int = 200):
        self.lines = lines
        self.status = status
        self.requests = 0

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/{topic}/json", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def stop(self) -> None:
        await self.runner.cleanup()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests += 1
        if self.status != 200:
            return web.Response(status=self.status)
        response = web.StreamResponse()
        await response.prepare(request)
        for line in self.lines:
            await response.write(line + b"\n")
        await response.write_eof()
        return response


class CapturingFlow:
    def __init__(self, explode: bool = False):
        self.captured: list[tuple[str, str]] = []
        self.explode = explode

    async def capture(self, text: str, source: str = "telegram", notify: bool = True):
        if self.explode:
            raise RuntimeError("разбор сломался")
        self.captured.append((text, source))
        return {"id": "1"}


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def listen_once(self, board: FakeBoard, flow: CapturingFlow) -> None:
        import aiohttp
        url_base = await board.start()
        try:
            async with aiohttp.ClientSession() as session:
                await relay.listen(session, f"{url_base}/polka-test/json",
                                   flow, asyncio.Event())
        finally:
            await board.stop()

    async def test_dictated_thought_reaches_the_shelf(self):
        board = FakeBoard([
            json.dumps({"event": "open"}).encode(),
            json.dumps({"event": "message", "message": "продлить домен"}).encode(),
        ])
        flow = CapturingFlow()
        await self.listen_once(board, flow)
        self.assertEqual(flow.captured, [("продлить домен", "shortcut")])

    async def test_several_thoughts_all_arrive(self):
        board = FakeBoard([
            json.dumps({"event": "message", "message": "первая"}).encode(),
            json.dumps({"event": "keepalive"}).encode(),
            json.dumps({"event": "message", "message": "вторая"}).encode(),
        ])
        flow = CapturingFlow()
        await self.listen_once(board, flow)
        self.assertEqual([text for text, _ in flow.captured], ["первая", "вторая"])

    async def test_broken_thought_does_not_break_the_stream(self):
        board = FakeBoard([
            json.dumps({"event": "message", "message": "первая"}).encode(),
            json.dumps({"event": "message", "message": "вторая"}).encode(),
        ])
        flow = CapturingFlow(explode=True)
        # Разбор падает на каждой мысли, но поток дочитывается до конца.
        await self.listen_once(board, flow)
        self.assertEqual(flow.captured, [])

    async def test_refusing_board_is_reported(self):
        board = FakeBoard([], status=404)
        with self.assertRaises(RuntimeError):
            await self.listen_once(board, CapturingFlow())


class RelayLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_topic_relay_does_nothing(self):
        flow = CapturingFlow()
        stop = asyncio.Event()
        await relay.run(make_config(relay_topic=""), flow, stop)
        self.assertEqual(flow.captured, [])

    async def test_loop_reconnects_after_the_stream_ends(self):
        board = FakeBoard([
            json.dumps({"event": "message", "message": "мысль"}).encode(),
        ])
        base = await board.start()
        flow = CapturingFlow()
        stop = asyncio.Event()
        cfg = make_config(relay_url=base)
        task = asyncio.create_task(relay.run(cfg, flow, stop))
        # Поток закрывается сразу, поэтому цикл обязан подключиться заново.
        for _ in range(40):
            if board.requests >= 2:
                break
            await asyncio.sleep(0.05)
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await board.stop()
        self.assertGreaterEqual(board.requests, 2)
        self.assertGreaterEqual(len(flow.captured), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
