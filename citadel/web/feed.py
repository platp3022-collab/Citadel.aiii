# -*- coding: utf-8 -*-
"""
Поток данных для панели: живые цены, секундные свечи из тиков и свечи с рынка.

Три источника, и панель честно говорит, каким пользуется:
  • «тики» — свечи, собранные из собственного опроса цен (1с, 5с, 15с, 30с);
  • «рынок» — свечи прямо с биржи (ccxt) или из GeckoTerminal (пул на DEX);
  • «кэш» — то, что бот уже скачал на диск, если сети нет.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

from ..candlecache import path_for, read as read_cache
from ..config import Config

log = logging.getLogger("citadel.web.feed")

#: секундные таймфреймы собираются из тиков, остальные тянутся с рынка
SECOND_TFS = {"1s": 1, "5s": 5, "15s": 15, "30s": 30}
MARKET_TFS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")


def tf_seconds(tf: str) -> int:
    if tf in SECOND_TFS:
        return SECOND_TFS[tf]
    unit, value = tf[-1], tf[:-1]
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit, 60)
    try:
        return int(value) * mult
    except ValueError:
        return 60


class Feed:
    """Общий доступ к рынку для панели: клиенты, кэш свечей, буфер тиков."""

    def __init__(self, ttl: float = 20.0):
        self.ticks: dict[str, deque] = {}
        self.lock = threading.Lock()
        self.ttl = ttl                      # сколько секунд держим свечи с рынка
        self._cache: dict[tuple, tuple[float, list]] = {}
        self._fail_until: dict[tuple, float] = {}   # куда сейчас бесполезно ходить
        self._clients: dict[str, object] = {}
        self._client_lock = threading.Lock()

    # ── тики ────────────────────────────────────────────────────────────────
    def push(self, prices: dict[str, float]) -> None:
        now = time.time() * 1000
        with self.lock:
            for symbol, price in prices.items():
                buf = self.ticks.setdefault(symbol, deque(maxlen=20000))
                if buf and abs(buf[-1][1] - price) < 1e-12 and now - buf[-1][0] < 30_000:
                    continue
                buf.append((now, float(price)))

    def tail(self, symbol: str, since: float = 0.0) -> list[list[float]]:
        with self.lock:
            buf = list(self.ticks.get(symbol) or ())
        return [[t, p] for t, p in buf if t > since]

    def last(self, symbol: str) -> tuple[float, float] | None:
        with self.lock:
            buf = self.ticks.get(symbol)
            return tuple(buf[-1]) if buf else None

    def tick_candles(self, symbol: str, seconds: int, limit: int = 240) -> list[list[float]]:
        """
        Свечи из тиков. Открытие — первая цена в интервале, максимум/минимум —
        крайние, закрытие — последняя. Объёма у такой свечи нет: мы видим цену,
        а не сделки.
        """
        with self.lock:
            buf = list(self.ticks.get(symbol) or ())
        if not buf:
            return []
        step = seconds * 1000
        out: list[list[float]] = []
        for ts, price in buf:
            bucket = int(ts // step) * step
            if out and out[-1][0] == bucket:
                candle = out[-1]
                candle[2] = max(candle[2], price)
                candle[3] = min(candle[3], price)
                candle[4] = price
            else:
                out.append([bucket, price, price, price, price, 0.0])
        return out[-limit:]

    # ── свечи с рынка ───────────────────────────────────────────────────────
    def market_candles(self, cfg: Config, mode: str, symbol: str, tf: str,
                       limit: int = 240) -> tuple[list[list[float]], str]:
        key = (mode, symbol, tf, limit)
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1], "рынок"
        if now < self._fail_until.get(key, 0.0):
            # рынок только что не ответил: не заставляем панель ждать повторно
            return (hit[1], "рынок") if hit else self._from_cache(cfg, mode, symbol, tf, limit)
        try:
            rows = (self._dex_candles(symbol, tf, limit) if mode == "dex"
                    else self._cex_candles(cfg, symbol, tf, limit))
            if rows:
                self._cache[key] = (now, rows)
                return rows, "рынок"
        except Exception as e:                      # noqa: BLE001 — сеть/биржа, идём в кэш
            log.debug("свечи %s %s не пришли: %s", symbol, tf, e)
            self._fail_until[key] = now + 30.0
        if hit:
            return hit[1], "рынок"
        return self._from_cache(cfg, mode, symbol, tf, limit)

    def _from_cache(self, cfg: Config, mode: str, symbol: str, tf: str,
                    limit: int) -> tuple[list[list[float]], str]:
        prefix = "dex" if mode == "dex" else getattr(cfg, "exchange", "cex")
        rows = read_cache(path_for(cfg.cache_dir, prefix, symbol, tf))
        if not rows and tf != cfg.timeframe:        # хотя бы то, что бот уже скачал
            rows = read_cache(path_for(cfg.cache_dir, prefix, symbol, cfg.timeframe))
        return [[int(r[0])] + [float(x) for x in r[1:6]] for r in rows[-limit:]], "кэш"

    def _cex_candles(self, cfg: Config, symbol: str, tf: str, limit: int) -> list[list[float]]:
        from ..market import Market                                  # noqa: PLC0415

        with self._client_lock:
            client = self._clients.get("cex")
            if client is None:
                client = Market(cfg)
                self._clients["cex"] = client
            ex = client.ex
            if ex is None:
                return []
            rows = ex.fetch_ohlcv(symbol, tf, limit=limit)            # включая текущий бар
        return [[int(r[0])] + [float(x or 0) for x in r[1:6]] for r in (rows or [])]

    def _dex_candles(self, symbol: str, tf: str, limit: int) -> list[list[float]]:
        from ..dex.geckoterminal import GeckoTerminal                 # noqa: PLC0415
        from ..dex.http import HttpClient                             # noqa: PLC0415

        chain, _, pool = symbol.partition(":")
        if not pool:
            return []
        with self._client_lock:
            client = self._clients.get("dex")
            if client is None:
                # панель не должна ждать длинных ретраев: страница живая, а не пакетная
                client = GeckoTerminal(HttpClient(
                    min_interval=1.0, timeout=8.0, retries=1,
                    headers={"Accept": "application/json;version=20230302"}))
                self._clients["dex"] = client
        return client.ohlcv(chain, pool, tf, limit=limit)

    # ── цены для опроса ─────────────────────────────────────────────────────
    def fetch_prices(self, cfg: Config, mode: str, symbols: list[str]) -> dict[str, float]:
        if mode == "dex":
            return self._dex_prices(symbols)
        return self._cex_prices(cfg, symbols)

    def _cex_prices(self, cfg: Config, symbols: list[str]) -> dict[str, float]:
        from ..market import Market                                   # noqa: PLC0415

        with self._client_lock:
            client = self._clients.get("cex")
            if client is None:
                client = Market(cfg)
                self._clients["cex"] = client
        ex = client.ex
        if ex is None:
            return {}
        out: dict[str, float] = {}
        try:
            for symbol, t in (ex.fetch_tickers(symbols) or {}).items():
                price = t.get("last") or t.get("close")
                if price:
                    out[symbol] = float(price)
        except Exception:                                             # noqa: BLE001
            for symbol in symbols:                                    # биржа не умеет пачкой
                try:
                    out[symbol] = client.last_price(symbol)
                except Exception:                                     # noqa: BLE001
                    continue
        return out

    def _dex_prices(self, symbols: list[str]) -> dict[str, float]:
        from ..dex.dexscreener import DexScreener                     # noqa: PLC0415

        with self._client_lock:
            client = self._clients.get("screener")
            if client is None:
                client = DexScreener()
                self._clients["screener"] = client
        by_chain: dict[str, list[str]] = {}
        for symbol in symbols:
            chain, _, pool = symbol.partition(":")
            if pool:
                by_chain.setdefault(chain, []).append(pool)
        out: dict[str, float] = {}
        for chain, pools in by_chain.items():
            for pair in client.pairs(chain, pools):
                if pair.price_usd > 0:
                    out[pair.key] = pair.price_usd
        return out


class PricePoller(threading.Thread):
    """Фоновый опрос цен: панель живая независимо от торгового цикла."""

    def __init__(self, panel, interval: float = 4.0):
        super().__init__(daemon=True)
        self.panel = panel
        self.interval = max(0.5, interval)
        self.stop_event = threading.Event()
        self.error = ""
        self.last_ok = 0.0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                cfg = self.panel.config()
                symbols = list(cfg.symbols)
                focus = getattr(self.panel, "focus_symbol", "")
                if focus and focus not in symbols:      # смотрим монету из ленты
                    symbols.append(focus)
                if symbols:
                    prices = self.panel.feed.fetch_prices(cfg, self.panel.mode, symbols)
                    if prices:
                        self.panel.feed.push(prices)
                        self.last_ok = time.time()
                        self.error = ""
            except Exception as e:                                    # noqa: BLE001
                self.error = str(e)[:200]
                log.debug("опрос цен не удался: %s", e)
                self.stop_event.wait(self.interval * 2)
            step = self.interval
            if hasattr(self.panel, "poll_interval"):     # темп зависит от того, что смотрят
                try:
                    step = max(0.5, float(self.panel.poll_interval()))
                except Exception:                        # noqa: BLE001
                    pass
            self.stop_event.wait(step)
