"""Приём мыслей с айфона без публичного адреса.

Полка стоит дома за роутером, у неё нет адреса, по которому её найдёт телефон.
Поэтому связь идёт наоборот: Полка сама держит открытое соединение с
публичной доской объявлений (ntfy), а команда быстрого доступа кладёт туда
надиктованный текст. Адрес доски постоянный, значит команду на айфоне
достаточно собрать один раз.

Имя доски и есть секрет: кто его знает, тот может писать. Поэтому оно
длинное и случайное.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from .config import Config
from .flow import Flow

log = logging.getLogger("polka.relay")

RECONNECT_MIN = 3
RECONNECT_MAX = 60


async def run(cfg: Config, flow: Flow, stop: asyncio.Event) -> None:
    if not cfg.relay_topic:
        return
    url = f"{cfg.relay_url}/{cfg.relay_topic}/json"
    log.info("Слушаю доску для двойного касания")
    delay = RECONNECT_MIN

    async with aiohttp.ClientSession() as session:
        while not stop.is_set():
            try:
                await listen(session, url, flow, stop)
                delay = RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — обрыв связи это норма
                log.warning("Доска отвалилась: %s. Переподключусь через %d с", exc, delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, RECONNECT_MAX)


async def listen(session: aiohttp.ClientSession, url: str, flow: Flow,
                 stop: asyncio.Event) -> None:
    """Держать открытым поток и разбирать приходящие строки."""
    # Поток живёт часами, поэтому общего ограничения по времени нет.
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=None)
    async with session.get(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"доска ответила {response.status}")
        log.info("Двойное касание на связи")
        async for raw in response.content:
            if stop.is_set():
                return
            text = extract(raw)
            if text:
                log.info("С айфона: %.60s", text)
                try:
                    await flow.capture(text, source="shortcut")
                except Exception:  # noqa: BLE001 — одна кривая мысль не рвёт поток
                    log.exception("Не разобрал мысль с айфона")


def extract(raw: bytes) -> str | None:
    """Вытащить текст мысли из строки потока.

    В потоке кроме сообщений идут служебные события: открытие подписки и
    периодические пинги. Их пропускаем.
    """
    line = raw.decode("utf-8", "replace").strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("event") != "message":
        return None
    text = str(event.get("message") or "").strip()
    return text or None
