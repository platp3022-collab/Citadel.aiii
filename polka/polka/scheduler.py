"""Планировщик: раз в полминуты смотрит, чему пора прозвенеть."""
from __future__ import annotations

import asyncio
import logging
import time

from .db import Store
from .flow import Flow

log = logging.getLogger("polka.scheduler")

TICK_SECONDS = 30


async def run(store: Store, flow: Flow, stop: asyncio.Event) -> None:
    log.info("Планировщик запущен, шаг %d с", TICK_SECONDS)
    while not stop.is_set():
        try:
            due = await store.due_reminders(time.time())
            for reminder in due:
                await flow.fire_reminder(reminder)
                await asyncio.sleep(0.3)          # не упираемся в лимиты Telegram
        except Exception:  # noqa: BLE001
            log.exception("Тик планировщика")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
