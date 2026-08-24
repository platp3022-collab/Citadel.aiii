# -*- coding: utf-8 -*-
"""
Поток сделок с биржи по вебсокету — настоящие тики, а не опрос раз в N секунд.

Binance отдаёт публичный поток без ключей и лимитов на подключение: каждая
сделка приходит отдельным сообщением. Из них панель и собирает секундные свечи,
которые иначе неоткуда взять — таких свечей нет ни в одном REST-API.

Для остальных бирж и для DEX поток не поднимается: там работает обычный опрос
(citadel/web/feed.py), и панель честно пишет, чем именно рисует.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

log = logging.getLogger("citadel.web.stream")

BINANCE_WS = "wss://stream.binance.com:9443/stream?streams="

#: биржи, для которых умеем поток сделок
SUPPORTED = {"binance"}


def supports(exchange: str, mode: str) -> bool:
    return mode == "cex" and str(exchange).lower() in SUPPORTED


class BinanceStream(threading.Thread):
    """Живые сделки Binance → буфер тиков панели."""

    def __init__(self, feed, symbols: list[str]):
        super().__init__(daemon=True)
        self.feed = feed
        self.symbols = list(symbols)
        self.error = ""
        self.connected = False
        self.messages = 0
        self.last_msg = 0.0
        # имя не должно совпадать с внутренним Thread._stop(), иначе ломается is_alive()
        self.stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── управление ──────────────────────────────────────────────────────────
    def stop(self) -> None:
        self.stop_event.set()
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)

    def alive(self) -> bool:
        """Поток считается живым, пока сообщения приходят.

        Тишина дольше минуты означает, что рынок стоит или соединение умерло, —
        в обоих случаях панель откатывается на обычный опрос цен.
        """
        return self.connected and (time.time() - self.last_msg) < 60

    # ── работа ──────────────────────────────────────────────────────────────
    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as e:                       # noqa: BLE001 — поток не должен ронять панель
            self.error = str(e)[:200]
            log.debug("поток сделок остановлен: %s", e)

    async def _main(self) -> None:
        try:
            import aiohttp                           # noqa: PLC0415 — зависимость необязательная
        except ImportError:
            self.error = "нет aiohttp — поток сделок недоступен, работает опрос цен"
            log.info(self.error)
            return

        self._loop = asyncio.get_running_loop()
        back_to_symbol = {s.replace("/", "").lower(): s for s in self.symbols}
        streams = "/".join(f"{name}@trade" for name in back_to_symbol)
        if not streams:
            return
        url = BINANCE_WS + streams
        delay = 1.0

        while not self.stop_event.is_set():
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=90)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(url, heartbeat=20) as ws:
                        self.connected = True
                        self.error = ""
                        delay = 1.0
                        log.info("поток сделок Binance подключён: %s", ", ".join(self.symbols))
                        async for msg in ws:
                            if self.stop_event.is_set():
                                break
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            self._handle(msg.data, back_to_symbol)
            except Exception as e:                   # noqa: BLE001 — сеть, переподключаемся
                self.error = str(e)[:200]
                log.debug("поток сделок оборвался: %s", e)
            self.connected = False
            if self.stop_event.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)             # не долбим биржу при обрыве

    def _handle(self, raw: str, back_to_symbol: dict[str, str]) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        data = payload.get("data") or payload
        name = str(payload.get("stream", "")).split("@")[0]
        symbol = back_to_symbol.get(name) or back_to_symbol.get(
            str(data.get("s", "")).lower())
        price = data.get("p") or data.get("c")
        if not symbol or price is None:
            return
        try:
            self.feed.push({symbol: float(price)})
        except (TypeError, ValueError):
            return
        self.messages += 1
        self.last_msg = time.time()
