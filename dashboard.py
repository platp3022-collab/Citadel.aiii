#!/usr/bin/env python3
"""
Мини-апп: живой дашборд бота в браузере.

Поднимает локальный веб-сервер и отдаёт одну страницу с текущим состоянием:
сколько в плюсе, что держим прямо сейчас, кривая доходности, последние сделки.
Данные берутся из той же базы, куда пишет трейдер, и обновляются сами.

Открывается на http://localhost:8420 — наружу ничего не торчит, страница
доступна только с этого компьютера.

Отдельно от бота:  python dashboard.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("dashboard")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "host": "127.0.0.1",        # только этот компьютер; 0.0.0.0 откроет в локальную сеть
    "port": 8420,
    "storage_path": "data/memebot.db",
}


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class DashboardData:
    """Читает состояние из базы. Только чтение — торговлю не трогаем."""

    def __init__(self, path: str | Path = "data/memebot.db"):
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        self.path = p

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        if not self.path.exists():
            return []
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.Error as e:
            log.debug("чтение базы: %s", e)
            return []
        finally:
            conn.close()

    def state(self) -> dict:
        closed = self._rows(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts ASC")
        open_rows = self._rows(
            "SELECT * FROM trades WHERE status='open' ORDER BY opened_ts DESC")

        day_ago = time.time() - 86400
        today = [r for r in closed if num(r.get("exit_ts")) >= day_ago]
        wins = [r for r in closed if num(r.get("pnl_sol")) > 0]

        # кривая накопленного результата
        equity, cum = [], 0.0
        for r in closed:
            cum += num(r.get("pnl_sol"))
            equity.append({"ts": num(r.get("exit_ts")), "cum": round(cum, 6),
                           "pnl": round(num(r.get("pnl_sol")), 6),
                           "symbol": r.get("symbol") or ""})

        # незакрытый результат по тому, что держим
        floating = 0.0
        positions = []
        for r in open_rows:
            entry, last = num(r.get("entry_price")), num(r.get("last_price"))
            change = ((last / entry - 1) * 100.0) if entry > 0 and last > 0 else 0.0
            high = num(r.get("high_price"))
            size = num(r.get("size_sol"))
            floating += size * change / 100.0
            positions.append({
                "symbol": r.get("symbol") or "—",
                "mint": r.get("mint") or "",
                "launchpad": r.get("launchpad") or "",
                "size_sol": size,
                "change_pct": change,
                "high_pct": ((high / entry - 1) * 100.0) if entry > 0 and high > 0 else 0.0,
                "minutes": (time.time() - num(r.get("opened_ts"))) / 60.0,
                "score": num(r.get("score")),
                "entry": entry,
                "last": last,
            })

        return {
            "mode": (closed + open_rows or [{}])[-1].get("mode") or "paper",
            "totals": {
                "pnl_sol": round(sum(num(r.get("pnl_sol")) for r in closed), 6),
                "invested": round(sum(num(r.get("size_sol")) for r in closed), 4),
                "trades": len(closed),
                "wins": len(wins),
                "winrate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
                "avg_minutes": (sum((num(r.get("exit_ts")) - num(r.get("opened_ts"))) / 60.0
                                    for r in closed) / len(closed)) if closed else 0.0,
                "best": max((num(r.get("pnl_pct")) for r in closed), default=0.0),
                "worst": min((num(r.get("pnl_pct")) for r in closed), default=0.0),
            },
            "today": {
                "pnl_sol": round(sum(num(r.get("pnl_sol")) for r in today), 6),
                "trades": len(today),
            },
            "floating_sol": round(floating, 6),
            "open": positions,
            "equity": equity,
            "recent": [{
                "symbol": r.get("symbol") or "—",
                "pnl_sol": num(r.get("pnl_sol")),
                "pnl_pct": num(r.get("pnl_pct")),
                "reason": r.get("exit_reason") or "",
                "minutes": (num(r.get("exit_ts")) - num(r.get("opened_ts"))) / 60.0,
                "ts": num(r.get("exit_ts")),
                "score": num(r.get("score")),
            } for r in reversed(closed[-40:])],
            "updated": time.time(),
        }


class Dashboard:
    """Веб-сервер мини-аппа."""

    def __init__(self, conf: dict[str, Any] | None = None):
        self.conf = {**DEFAULTS, **(conf or {})}
        self.data = DashboardData(self.conf.get("storage_path", "data/memebot.db"))
        self.page = ROOT / "dashboard.html"
        self.runner: web.AppRunner | None = None

    @property
    def host(self) -> str:
        # в контейнере слушать надо на 0.0.0.0, иначе порт наружу не пробросится
        return os.environ.get("DASHBOARD_HOST", "").strip() \
            or str(self.conf.get("host", "127.0.0.1"))

    @property
    def url(self) -> str:
        host = self.host
        shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
        return f"http://{shown}:{int(num(self.conf.get('port'), 8420))}"

    async def _index(self, request: web.Request) -> web.Response:
        if not self.page.exists():
            return web.Response(text="dashboard.html рядом не найден", status=500)
        return web.Response(text=self.page.read_text(encoding="utf-8"),
                            content_type="text/html", charset="utf-8")

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(self.data.state(),
                                 dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def start(self) -> None:
        if not self.conf.get("enabled", True):
            return
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/state", self._state)

        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host,
                           int(num(self.conf.get("port"), 8420)))
        try:
            await site.start()
            log.info("Дашборд открыт: %s", self.url)
        except OSError as e:
            log.warning("Дашборд не поднялся (%s) — порт занят? Бот работает дальше", e)
            self.runner = None

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None


async def amain() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dash = Dashboard()
    await dash.start()
    print(f"\nДашборд: {dash.url}   (Ctrl+C — выход)\n")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await dash.stop()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
