# -*- coding: utf-8 -*-
"""
Локальная веб-панель: запускаешь python webui.py — открывается браузер, в нём
терминал с кнопками, состояние счёта, позиции, стратегии и график эквити.

Как устроено:
  • сервер на stdlib (http.server), слушает только 127.0.0.1;
  • команды бота запускаются настоящими процессами (tradebot.py / dexbot.py),
    их вывод построчно течёт в браузер — то есть это буквально тот же терминал;
  • состояние читается из sqlite бота, поэтому панель не мешает торговле и
    показывает то же самое, что `report`;
  • доступ по токену в адресе: без него запросы отклоняются.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..candlecache import path_for, read as read_cache
from ..config import Config
from ..storage import Storage

log = logging.getLogger("citadel.web")

ROOT = Path(__file__).resolve().parent.parent.parent
UI_FILE = Path(__file__).resolve().parent / "ui.html"

#: какие команды панель имеет право запускать
COMMANDS = {
    "cex": {
        "fetch": ["fetch"], "evolve": ["evolve"], "backtest": ["backtest", "--trades"],
        "report": ["report"], "trade": ["trade"], "pine": ["pine"],
    },
    "dex": {
        "discover": ["discover", "--show-rejected"], "fetch": ["fetch"], "evolve": ["evolve"],
        "backtest": ["backtest", "--trades"], "pairs": ["pairs"], "report": ["report"],
        "trade": ["trade"], "pine": ["pine"],
    },
}
ENTRYPOINT = {"cex": "tradebot.py", "dex": "dexbot.py"}

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="7" fill="#0b0f14"/>'
           '<path d="M6 22l5-8 5 5 4-9 6 12" fill="none" stroke="#3fd0c9" stroke-width="2.6" '
           'stroke-linejoin="round" stroke-linecap="round"/></svg>').encode()

#: настройки, которые можно править прямо из панели
EDITABLE = {
    "CITADEL_SYMBOLS": "Пары (биржа)", "CITADEL_TIMEFRAME": "Таймфрейм",
    "CITADEL_EXCHANGE": "Биржа", "CITADEL_CHAIN": "Сеть (DEX)",
    "CITADEL_START_BALANCE": "Стартовый баланс", "CITADEL_MAX_POSITIONS": "Позиций одновременно",
    "CITADEL_MAX_DRAWDOWN_STOP": "Стоп по просадке", "CITADEL_DAILY_LOSS_STOP": "Дневной стоп",
    "CITADEL_UNIVERSE_SIZE": "Пар в работе (DEX)", "CITADEL_MIN_LIQUIDITY_USD": "Мин. ликвидность (DEX)",
    "CITADEL_POPULATION": "Популяция поиска", "CITADEL_GENERATIONS": "Поколений поиска",
}


class Runner:
    """Один запущенный процесс бота и кольцевой буфер его вывода."""

    def __init__(self, limit: int = 4000):
        self.lines: deque[tuple[int, str]] = deque(maxlen=limit)
        self.counter = 0
        self.proc: subprocess.Popen | None = None
        self.command = ""
        self.started_at = 0.0
        self.lock = threading.Lock()

    # ── лог ─────────────────────────────────────────────────────────────────
    def emit(self, text: str) -> None:
        with self.lock:
            for line in text.rstrip("\n").split("\n"):
                self.counter += 1
                self.lines.append((self.counter, line))

    def tail(self, since: int) -> tuple[list[str], int]:
        with self.lock:
            fresh = [line for n, line in self.lines if n > since]
            return fresh, self.counter

    # ── процесс ─────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, mode: str, cmd: str, common: list[str], cmd_flags: list[str],
              env: dict) -> str:
        """
        common — флаги парсера (--dry, --offline): argparse требует их ДО команды;
        cmd_flags — флаги самой команды (--live, --yes): они идут после неё.
        """
        if self.running:
            return f"уже выполняется: {self.command}"
        args = COMMANDS[mode].get(cmd)
        if args is None:
            return f"неизвестная команда: {cmd}"
        parts = [*common, *args, *cmd_flags]
        argv = [sys.executable, "-u", str(ROOT / ENTRYPOINT[mode]), *parts]
        self.command = f"{ENTRYPOINT[mode]} {' '.join(parts)}"
        self.started_at = time.time()
        self.emit(f"$ python {self.command}")
        try:
            self.proc = subprocess.Popen(
                argv, cwd=str(ROOT), env={**os.environ, **env}, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                bufsize=1)
        except OSError as e:
            self.emit(f"не удалось запустить: {e}")
            return str(e)
        threading.Thread(target=self._pump, daemon=True).start()
        return ""

    def _pump(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            self.emit(line.rstrip("\n"))
        code = proc.wait()
        self.emit(f"— команда завершена (код {code}) —")

    def stop(self) -> str:
        if not self.running:
            return "нечего останавливать"
        assert self.proc is not None
        self.emit("— останавливаю —")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return ""


class Panel:
    """Состояние панели: конфиг, база бота, запущенный процесс."""

    def __init__(self, mode: str = "cex", allow_live: bool = False):
        self.mode = mode
        self.allow_live = allow_live
        self.runner = Runner()
        self.overrides: dict[str, str] = {}

    # ── конфиг ──────────────────────────────────────────────────────────────
    def config(self) -> Config:
        env_backup = dict(os.environ)
        os.environ.update(self.overrides)
        try:
            if self.mode == "dex":
                from ..dex.config import DexConfig                    # noqa: PLC0415
                return DexConfig.from_env()
            return Config.from_env()
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

    def settings(self) -> list[dict]:
        cfg = self.config()
        out = []
        for key, title in EDITABLE.items():
            field = key.removeprefix("CITADEL_").lower()
            value = self.overrides.get(key)
            if value is None:
                value = getattr(cfg, field, "")
                if isinstance(value, tuple):
                    value = ",".join(value)
            out.append({"key": key, "title": title, "value": str(value)})
        return out

    # ── данные для панели ───────────────────────────────────────────────────
    def state(self) -> dict:
        cfg = self.config()
        try:
            store = Storage(cfg.db_path)
        except Exception as e:                                        # noqa: BLE001
            return {"error": f"не открывается база {cfg.db_path}: {e}"}
        try:
            return self._state(cfg, store)
        finally:
            store.close()

    def _state(self, cfg: Config, store: Storage) -> dict:
        pairs = self._pairs(cfg)                      # читаем метаданные пар один раз
        labels = {key: f"{p.get('base_symbol', '?')}/{p.get('quote_symbol', '?')}"
                  for key, p in pairs.items()}
        url_of = lambda symbol: (pairs.get(symbol) or {}).get("url", "")
        prices: dict[str, float] = {}
        positions = []
        for row in store.all_positions():
            symbol = row["symbol"]
            price = self._last_price(cfg, pairs, symbol) or float(row["entry_price"])
            prices[symbol] = price
            entry = float(row["entry_price"])
            positions.append({
                "symbol": symbol, "label": labels.get(symbol, symbol),
                "qty": float(row["qty"]), "entry": entry, "price": price,
                "change": (price / entry - 1) * 100 if entry else 0.0,
                "stop": float(row["stop"] or 0.0), "take": float(row["take"] or 0.0),
                "url": url_of(symbol),
            })

        curve = [[int(r["ts"]) * 1000, float(r["equity"])] for r in store.equity_curve(400)]
        cash = float(store.get("paper_cash", cfg.start_balance) or cfg.start_balance)
        equity = cash + sum(p["qty"] * p["price"] for p in positions)
        start = float(store.get("paper_start", cfg.start_balance) or cfg.start_balance)

        strategies = []
        for symbol in cfg.symbols or [p["symbol"] for p in positions]:
            row = store.active_strategy(symbol)
            item = {"symbol": symbol, "label": labels.get(symbol, symbol),
                    "url": url_of(symbol)}
            if row:
                from ..genome import Genome                           # noqa: PLC0415
                g = Genome.from_json(row["genome"])
                item.update({"id": row["id"], "score": float(row["score"] or 0.0),
                             "describe": g.describe(),
                             "entry": list(g.entry), "exit": list(g.exit),
                             "metrics": json.loads(row["metrics"] or "{}")})
            strategies.append(item)

        trades = [{
            "ts": int(t["ts"]) * 1000, "side": t["side"], "symbol": t["symbol"],
            "label": labels.get(t["symbol"], t["symbol"]), "qty": float(t["qty"] or 0.0),
            "price": float(t["price"] or 0.0), "pnl": float(t["pnl"] or 0.0),
            "reason": t["reason"], "live": bool(t["live"]),
            "tx": t["order_id"] or "", "url": url_of(t["symbol"]),
        } for t in store.recent_trades(40)]

        return {
            "mode": self.mode, "allow_live": self.allow_live, "running": self.runner.running, "command": self.runner.command,
            "since": self.runner.started_at,
            "equity": equity, "cash": cash, "start": start,
            "pnl_pct": (equity / start - 1) * 100 if start else 0.0,
            "positions": positions, "strategies": strategies, "trades": trades,
            "curve": curve, "paused": store.get("paused_reason"),
            "settings": self.settings(),
            "symbols": list(cfg.symbols), "timeframe": cfg.timeframe,
            "quote": cfg.quote, "db": cfg.db_path,
        }

    # ── вспомогательное ─────────────────────────────────────────────────────
    def _pairs(self, cfg: Config) -> dict:
        if self.mode != "dex":
            return {}
        try:
            return json.loads(Path(cfg.pairs_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            return {}

    def _last_price(self, cfg: Config, pairs: dict, symbol: str) -> float:
        """Последняя известная цена: из метаданных пары или из кэша свечей."""
        pair = pairs.get(symbol)
        if pair and float(pair.get("price_usd") or 0) > 0:
            return float(pair["price_usd"])
        prefix = "dex" if self.mode == "dex" else getattr(cfg, "exchange", "cex")
        rows = read_cache(path_for(cfg.cache_dir, prefix, symbol, cfg.timeframe))
        return float(rows[-1][4]) if rows else 0.0


class Handler(BaseHTTPRequestHandler):
    panel: Panel
    token: str
    server_version = "CitadelPanel/1.0"

    def log_message(self, fmt, *args):                      # тише в консоли
        log.debug(fmt, *args)

    # ── ответы ──────────────────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _authorized(self, query: dict) -> bool:
        given = (query.get("token") or [""])[0] or self.headers.get("X-Token", "")
        # сравниваем байтами: compare_digest не принимает строки с не-ASCII,
        # а в адресе может оказаться что угодно
        return secrets.compare_digest(given.encode("utf-8", "replace"),
                                      self.token.encode("utf-8"))

    # ── маршруты ────────────────────────────────────────────────────────────
    def do_GET(self) -> None:                                # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/favicon.ico":               # без токена: иконка вкладки
            self._send(200, FAVICON, "image/svg+xml")
            return
        if url.path in ("/", "/index.html"):
            if not self._authorized(query):
                self._send(403, "Нужен токен из адресной строки, который напечатал запуск."
                                .encode(), "text/plain; charset=utf-8")
                return
            html = UI_FILE.read_text(encoding="utf-8").replace("__TOKEN__", self.token)
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return
        if not self._authorized(query):
            self._json({"error": "нет доступа"}, 403)
            return
        if url.path == "/api/state":
            self._json(self.panel.state())
        elif url.path == "/api/log":
            since = int((query.get("since") or ["0"])[0] or 0)
            lines, counter = self.panel.runner.tail(since)
            self._json({"lines": lines, "counter": counter,
                        "running": self.panel.runner.running,
                        "command": self.panel.runner.command})
        else:
            self._json({"error": "не найдено"}, 404)

    def do_POST(self) -> None:                               # noqa: N802
        url = urlparse(self.path)
        if not self._authorized(parse_qs(url.query)):
            self._json({"error": "нет доступа"}, 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"error": "неверный JSON"}, 400)
            return
        panel = self.panel

        if url.path == "/api/run":
            mode = payload.get("mode", panel.mode)
            if mode not in COMMANDS:
                self._json({"error": "неизвестный режим"}, 400)
                return
            panel.mode = mode
            cmd = str(payload.get("cmd", ""))
            common = ["--dry"] if payload.get("dry") else []
            if payload.get("offline"):
                common.append("--offline")
            cmd_flags: list[str] = []
            if cmd == "trade" and payload.get("live"):
                if not panel.allow_live:
                    self._json({"error": "реальная торговля из панели запрещена: "
                                         "перезапусти webui.py с флагом --allow-live"}, 403)
                    return
                cmd_flags += ["--live", "--yes"]
            error = panel.runner.start(mode, cmd, common, cmd_flags, panel.overrides)
            self._json({"error": error} if error else {"ok": True})
        elif url.path == "/api/stop":
            self._json({"error": panel.runner.stop()})
        elif url.path == "/api/mode":
            mode = payload.get("mode", "cex")
            if mode not in COMMANDS:
                self._json({"error": "неизвестный режим"}, 400)
                return
            panel.mode = mode
            self._json({"ok": True, "mode": mode})
        elif url.path == "/api/settings":
            for key, value in (payload.get("settings") or {}).items():
                if key in EDITABLE:
                    text = str(value).strip()
                    if text:
                        panel.overrides[key] = text
                    else:
                        panel.overrides.pop(key, None)
            self._json({"ok": True, "settings": panel.settings()})
        else:
            self._json({"error": "не найдено"}, 404)


def serve(host: str = "127.0.0.1", port: int = 8765, mode: str = "cex",
          allow_live: bool = False) -> tuple[ThreadingHTTPServer, str]:
    """Поднимает сервер и возвращает (сервер, адрес с токеном)."""
    token = secrets.token_urlsafe(16)
    handler = type("BoundHandler", (Handler,), {"panel": Panel(mode, allow_live), "token": token})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{httpd.server_address[1]}/?token={token}"
    return httpd, url
