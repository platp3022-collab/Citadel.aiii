#!/usr/bin/env python3
"""
Поток запусков в реальном времени.

Главная разница между ботом и человеком за терминалом — не анализ, а место
в очереди. Опрос лент раз в 40 секунд означает, что монету мы видим на
третьей-пятой минуте жизни: первые свечи уже прошли, и заходим мы туда, где
человек с горячей клавишей уже фиксирует прибыль.

Здесь бот подключается к потоку событий pump.fun и узнаёт о запуске в ту же
секунду, когда он произошёл. Ни ключей, ни оплаты: publicWebSocket отдаёт
события всем. Поток приносит и то, чего нет в лентах, — сколько SOL вложил
в свой запуск сам разработчик (Dev Buy из разбора).

Соединение рвётся регулярно — это нормально: поднимаемся заново с растущей
паузой, а обычный опрос лент продолжает работать как страховка.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger("stream")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "url": "wss://pumpportal.fun/api/data",
    "subscribe": ["subscribeNewToken"],
    "max_age_seconds": 90,       # событие старше — уже не «в момент запуска»
    "min_dev_buy_sol": 0.0,      # 0 = не смотрим, сколько вложил дев
    "max_dev_buy_sol": 25.0,     # дев закупился сам на слишком много — бандл
    "queue_limit": 400,
    "reconnect_min": 2,
    "reconnect_max": 60,
}


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class NewToken:
    """Запуск, увиденный в момент, когда он произошёл."""
    mint: str
    symbol: str = ""
    name: str = ""
    creator: str = ""
    dev_buy_sol: float = 0.0       # сколько SOL вложил в свой запуск сам дев
    mcap_sol: float = 0.0
    pool: str = "pump"
    uri: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.ts)


class LaunchStream:
    """Держит соединение с потоком запусков и складывает их в очередь."""

    def __init__(self, session: aiohttp.ClientSession | None = None,
                 conf: dict[str, Any] | None = None,
                 on_token: Callable[[NewToken], Awaitable[Any]] | None = None):
        self.conf = {**DEFAULTS, **(conf or {})}
        self.session = session
        self.on_token = on_token
        self.queue: asyncio.Queue[NewToken] = asyncio.Queue(
            maxsize=int(num(self.conf.get("queue_limit"), 400)))
        self.seen: set[str] = set()
        self.connected = False
        self.events = 0
        self.dropped = 0
        self.last_event = 0.0
        self.last_error = ""

    # ---------- разбор события ----------

    @staticmethod
    def parse(data: dict) -> NewToken | None:
        """Событие создания токена → наша запись. Прочее пропускаем."""
        if not isinstance(data, dict):
            return None
        if data.get("txType") not in ("create", None):
            return None
        mint = str(data.get("mint") or "").strip()
        if not (32 <= len(mint) <= 44):
            return None
        return NewToken(
            mint=mint,
            symbol=str(data.get("symbol") or "")[:16],
            name=str(data.get("name") or "")[:64],
            creator=str(data.get("traderPublicKey") or ""),
            # initialBuy приходит в токенах, solAmount — в SOL: нам нужен SOL
            dev_buy_sol=num(data.get("solAmount")) or num(data.get("initialBuySol")),
            mcap_sol=num(data.get("marketCapSol")),
            pool=str(data.get("pool") or "pump"),
            uri=str(data.get("uri") or ""),
        )

    def wanted(self, t: NewToken) -> str:
        """Пустая строка — берём в работу, иначе причина отказа."""
        if t.mint in self.seen:
            return "уже видели"
        top = num(self.conf.get("max_dev_buy_sol"), 25)
        if top and t.dev_buy_sol > top:
            # дев выкупил свой же запуск на пол-банка: это бандл, а не лонч
            return f"дев закупился сам на {t.dev_buy_sol:.1f} SOL"
        low = num(self.conf.get("min_dev_buy_sol"))
        if low and t.dev_buy_sol < low:
            return f"дев вложил всего {t.dev_buy_sol:.2f} SOL"
        return ""

    # ---------- соединение ----------

    async def _pump(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        for method in (self.conf.get("subscribe") or ["subscribeNewToken"]):
            await ws.send_json({"method": method})
        self.connected = True
        log.info("Поток запусков подключён — вижу монеты в момент создания")

        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            try:
                data = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            token = self.parse(data)
            if token is None:
                continue
            self.events += 1
            self.last_event = time.time()

            skip = self.wanted(token)
            self.seen.add(token.mint)
            if len(self.seen) > 20000:
                self.seen = set(list(self.seen)[-10000:])
            if skip:
                log.debug("поток: $%s пропускаю — %s", token.symbol, skip)
                continue

            if self.on_token is not None:
                # разбор не должен тормозить чтение сокета
                asyncio.create_task(self._safe_call(token))
            else:
                try:
                    self.queue.put_nowait(token)
                except asyncio.QueueFull:
                    self.dropped += 1

    async def _safe_call(self, token: NewToken) -> None:
        try:
            await self.on_token(token)
        except Exception as e:  # noqa: BLE001
            log.warning("обработка $%s из потока: %s", token.symbol, e)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Держит соединение живым, пока бот работает."""
        if not self.conf.get("enabled", True) or self.session is None:
            return
        delay = num(self.conf.get("reconnect_min"), 2)
        while not (stop and stop.is_set()):
            try:
                async with self.session.ws_connect(
                        str(self.conf.get("url")), heartbeat=30,
                        timeout=aiohttp.ClientTimeout(total=30)) as ws:
                    delay = num(self.conf.get("reconnect_min"), 2)
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {str(e)[:100]}"
                log.warning("поток запусков оборвался: %s", self.last_error)
            self.connected = False
            if stop and stop.is_set():
                return
            # рвётся регулярно — поднимаемся заново, но без штурма сервера
            with contextlib.suppress(asyncio.TimeoutError):
                if stop:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                    return
                await asyncio.sleep(delay)
            delay = min(num(self.conf.get("reconnect_max"), 60), delay * 2)

    def drain(self, limit: int = 50) -> list[NewToken]:
        """Забрать накопившиеся запуски (когда обработчик не задан)."""
        out: list[NewToken] = []
        max_age = num(self.conf.get("max_age_seconds"), 90)
        while len(out) < limit:
            try:
                token = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if max_age and token.age_seconds > max_age:
                continue          # протухло, пока стояли в очереди
            out.append(token)
        return out

    def status_line(self) -> str:
        if not self.conf.get("enabled", True):
            return "Поток запусков: выключен"
        if not self.connected:
            return ("Поток запусков: нет связи"
                    + (f" ({self.last_error})" if self.last_error else "")
                    + " — работает обычный опрос лент")
        ago = (time.time() - self.last_event) if self.last_event else -1
        return (f"Поток запусков: на связи, событий {self.events}"
                + (f", последнее {ago:.0f} с назад" if ago >= 0 else "")
                + (f", потеряно {self.dropped}" if self.dropped else ""))
