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
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("dashboard")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "host": "127.0.0.1",        # только этот компьютер; 0.0.0.0 откроет в локальную сеть
    "port": 8420,
    "storage_path": "data/memebot.db",

    # Открыть страницу внутри Telegram. Мини-апп работает только по публичному
    # HTTPS-адресу, localhost туда не пускают, поэтому нужен туннель наружу.
    "tunnel": True,             # поднять cloudflared и получить https-адрес
    "public_url": "",           # или впиши свой адрес, если он уже есть
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
        self.tunnel_proc: asyncio.subprocess.Process | None = None
        self.public_url = (os.environ.get("DASHBOARD_PUBLIC_URL", "").strip()
                           or str(self.conf.get("public_url", "")).strip())
        self.token = self._load_token()
        self.reason = ""          # почему мини-апп недоступен снаружи

    def _load_token(self) -> str:
        """Секрет для публичного адреса: без него страницу увидит любой,
        кто узнает ссылку туннеля. Берём из .env или заводим свой и храним."""
        token = os.environ.get("DASHBOARD_TOKEN", "").strip()
        if token:
            return token
        path = Path(self.conf.get("storage_path", "data/memebot.db"))
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        keyfile = path.parent / "dashboard.key"
        if keyfile.exists():
            return keyfile.read_text(encoding="utf-8").strip()
        token = secrets.token_urlsafe(18)
        keyfile.write_text(token, encoding="utf-8")
        return token

    def _allowed(self, request: web.Request) -> bool:
        """Локальную страницу открываем свободно, публичную — только с токеном."""
        if request.query.get("k") == self.token:
            return True
        peer = request.transport.get_extra_info("peername") if request.transport else None
        host = peer[0] if peer else ""
        return host in ("127.0.0.1", "::1", "localhost")

    def link(self, base: str = "") -> str:
        base = (base or self.public_url).rstrip("/")
        if not base:
            return self.url
        # путь обязателен: адрес вида "https://host?k=..." Telegram считает
        # некорректным для кнопки мини-аппа и отбивает всё сообщение
        return f"{base}/?k={self.token}"

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
        if not self._allowed(request):
            raise web.HTTPForbidden(text="нужен ключ доступа")
        if not self.page.exists():
            return web.Response(text="dashboard.html рядом не найден", status=500)
        return web.Response(text=self.page.read_text(encoding="utf-8"),
                            content_type="text/html", charset="utf-8")

    async def _asset(self, request: web.Request) -> web.Response:
        """Шрифты лежат рядом — страница не зависит от интернета и CDN."""
        name = request.match_info.get("name", "")
        # только простые имена: никаких путей наружу
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.woff2", name):
            raise web.HTTPNotFound()
        path = ROOT / "assets" / "fonts" / name
        if not path.exists():
            raise web.HTTPNotFound()
        return web.Response(body=path.read_bytes(), content_type="font/woff2",
                            headers={"Cache-Control": "public, max-age=604800"})

    async def _state(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            raise web.HTTPForbidden(text="нужен ключ доступа")
        return web.json_response(self.data.state(),
                                 dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def start_tunnel(self) -> str:
        """Публичный https-адрес через cloudflared — без него Telegram
        мини-апп не откроет. Аккаунт и домен не нужны."""
        if self.public_url:
            return self.public_url
        if not self.conf.get("tunnel", True):
            self.reason = "туннель выключен в настройках"
            return ""
        if not self.runner:
            # без этого туннель уводил бы на мёртвый порт, и мини-апп
            # открывался бы в пустоту
            self.reason = "локальный сервер не поднялся — порт занят другим окном бота?"
            log.warning("Туннель не запускаю: %s", self.reason)
            return ""
        exe = shutil.which("cloudflared")
        if not exe:
            self.reason = "cloudflared не установлен"
            log.warning("cloudflared не найден — мини-апп в Telegram не поднять. "
                        "Установка: winget install Cloudflare.cloudflared "
                        "(или brew install cloudflared)")
            return ""
        try:
            self.tunnel_proc = await asyncio.create_subprocess_exec(
                exe, "tunnel", "--url", f"http://127.0.0.1:{int(num(self.conf.get('port'), 8420))}",
                "--no-autoupdate",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except Exception as e:  # noqa: BLE001
            log.warning("не смог запустить cloudflared: %s", e)
            return ""

        # Адрес печатается в вывод в первые секунды. Служебный api.trycloudflare.com
        # мелькает там раньше настоящего — его надо пропустить, иначе кнопка
        # уводит на API Cloudflare, который отвечает "Method Not Allowed".
        # Адрес туннеля всегда из нескольких слов через дефис.
        pattern = re.compile(r"https://(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
        try:
            for _ in range(60):
                line = await asyncio.wait_for(self.tunnel_proc.stdout.readline(), timeout=30)
                if not line:
                    break
                found = pattern.search(line.decode("utf-8", "ignore"))
                if found:
                    self.public_url = found.group(0)
                    asyncio.create_task(self._drain_tunnel())
                    if await self.selfcheck():
                        log.info("Туннель поднят и отвечает: %s", self.public_url)
                        return self.public_url
                    log.warning("Туннель поднялся, но страница через него не открылась: %s",
                                self.reason)
                    return ""
        except asyncio.TimeoutError:
            self.reason = "cloudflared не отдал адрес за отведённое время"
            log.warning("Туннель не поднялся: %s", self.reason)
        except Exception as e:  # noqa: BLE001
            self.reason = f"ошибка туннеля: {e}"
            log.warning("Туннель не поднялся: %s", e)
        return ""

    async def selfcheck(self) -> bool:
        """Проверяем публичный адрес снаружи: открывается ли страница на самом деле.
        Иначе кнопка в Telegram ведёт в никуда, а понять это можно только по факту."""
        url = self.link()
        for attempt in range(6):
            await asyncio.sleep(2)
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        body = (await r.text())[:4000]
                        if r.status != 200:
                            self.reason = f"адрес отвечает {r.status}"
                            continue
                        # статус 200 сам по себе ничего не значит: по ошибке можно
                        # попасть на чужой сервис, который бодро отвечает своим JSON
                        if "Citadel" not in body:
                            self.reason = "по адресу отвечает не наша страница"
                            log.warning("Самопроверка: %s вернул чужой ответ: %s",
                                        url, body[:120])
                            return False
                        self.reason = ""
                        return True
            except Exception as e:  # noqa: BLE001
                self.reason = f"адрес не отвечает ({type(e).__name__})"
        return False

    def status_line(self) -> str:
        if self.public_url:
            return f"Мини-апп: {self.public_url} (открывается в Telegram)"
        if not self.runner:
            return "Мини-апп не работает: " + (self.reason or "сервер не поднялся")
        return (f"Мини-апп: {self.url} — только на этом компьютере"
                + (f" ({self.reason})" if self.reason else ""))

    async def _drain_tunnel(self) -> None:
        """Читаем вывод дальше, иначе буфер переполнится и cloudflared встанет."""
        try:
            while self.tunnel_proc and self.tunnel_proc.stdout:
                if not await self.tunnel_proc.stdout.readline():
                    break
        except Exception:  # noqa: BLE001
            pass

    async def start(self) -> None:
        if not self.conf.get("enabled", True):
            return
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/state", self._state)
        app.router.add_get("/assets/fonts/{name}", self._asset)

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
        if self.tunnel_proc and self.tunnel_proc.returncode is None:
            self.tunnel_proc.terminate()
            self.tunnel_proc = None
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
