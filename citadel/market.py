# -*- coding: utf-8 -*-
"""
Доступ к бирже через ccxt + кэш свечей на диске.

Кэш нужен по двум причинам: не долбить биржу одинаковыми запросами при поиске
стратегии и уметь считать бэктест офлайн (`--offline`).
"""
from __future__ import annotations

import csv
import logging
import time
from pathlib import Path

from .config import TIMEFRAME_SECONDS, Config
from .features import Candles

log = logging.getLogger("citadel.market")

try:
    import ccxt
except ImportError:                                    # ccxt нужен только для реальных данных
    ccxt = None


class Market:
    def __init__(self, cfg: Config, need_keys: bool = False, offline: bool = False):
        self.cfg = cfg
        self.ex = None
        self.offline = offline
        self._markets: dict = {}
        if offline:
            log.info("офлайн-режим: работаю только по кэшу свечей")
            return
        if ccxt is None:
            log.warning("ccxt не установлен — доступны только офлайн-данные (pip install ccxt)")
            return
        if not hasattr(ccxt, cfg.exchange):
            raise SystemExit(f"биржа '{cfg.exchange}' не найдена в ccxt")
        params = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
        if need_keys:
            if not cfg.api_key or not cfg.api_secret:
                raise SystemExit("для реальной торговли нужны EXCHANGE_API_KEY и EXCHANGE_API_SECRET в .env")
            params["apiKey"] = cfg.api_key
            params["secret"] = cfg.api_secret
        self.ex = getattr(ccxt, cfg.exchange)(params)

    # ── свечи ───────────────────────────────────────────────────────────────
    def cache_path(self, symbol: str, timeframe: str) -> Path:
        safe = symbol.replace("/", "-").replace(":", "-")
        return Path(self.cfg.cache_dir) / f"{self.cfg.exchange}_{safe}_{timeframe}.csv"

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int,
                    offline: bool = False, use_cache: bool = True) -> Candles:
        offline = offline or self.offline
        """Тянет `limit` последних свечей (постранично), склеивает с кэшем."""
        cached = self._read_cache(symbol, timeframe) if use_cache else []
        if offline or self.ex is None:
            if not cached:
                raise SystemExit(f"нет кэша свечей для {symbol} {timeframe} — сначала запусти `fetch`")
            return Candles.from_ohlcv(cached[-limit:])

        step_ms = TIMEFRAME_SECONDS.get(timeframe, 3600) * 1000
        since = int(time.time() * 1000) - step_ms * (limit + 5)
        rows: list[list[float]] = []
        while True:
            try:
                batch = self.ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            except Exception as e:                     # noqa: BLE001 — сеть/биржа, показываем и падаем мягко
                log.warning("не удалось получить свечи %s: %s", symbol, e)
                if cached:
                    log.warning("работаю на кэше (%d свечей)", len(cached))
                    return Candles.from_ohlcv(cached[-limit:])
                raise
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 2:
                break
            since = batch[-1][0] + step_ms
            if since > time.time() * 1000 or len(rows) >= limit + 1000:
                break
            time.sleep(self.ex.rateLimit / 1000.0)

        merged = self._merge(cached, rows)
        if use_cache and merged:
            self._write_cache(symbol, timeframe, merged)
        # последняя свеча ещё формируется — в расчёты её не берём
        closed = [r for r in merged if r[0] + step_ms <= time.time() * 1000]
        return Candles.from_ohlcv(closed[-limit:])

    @staticmethod
    def _merge(a: list, b: list) -> list:
        by_ts = {int(r[0]): [int(r[0])] + [float(x) for x in r[1:6]] for r in a}
        by_ts.update({int(r[0]): [int(r[0])] + [float(x) for x in r[1:6]] for r in b})
        return [by_ts[k] for k in sorted(by_ts)]

    def _read_cache(self, symbol: str, timeframe: str) -> list[list[float]]:
        p = self.cache_path(symbol, timeframe)
        if not p.exists():
            return []
        out = []
        with p.open(newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if not row or row[0].startswith("ts"):
                    continue
                try:
                    out.append([int(row[0])] + [float(x) for x in row[1:6]])
                except ValueError:
                    continue
        return out

    def _write_cache(self, symbol: str, timeframe: str, rows: list[list[float]]) -> None:
        p = self.cache_path(symbol, timeframe)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ts", "open", "high", "low", "close", "volume"])
            w.writerows(rows)
        tmp.replace(p)

    # ── справочная информация ───────────────────────────────────────────────
    def load_markets(self) -> dict:
        if not self._markets and self.ex is not None and not self.offline:
            self._markets = self.ex.load_markets()
        return self._markets

    def market(self, symbol: str) -> dict:
        return self.load_markets().get(symbol, {})

    def min_notional(self, symbol: str) -> float:
        m = self.market(symbol)
        limits = (m.get("limits") or {}).get("cost") or {}
        return float(limits.get("min") or self.cfg.min_notional)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        if self.ex is None:
            return amount
        try:
            return float(self.ex.amount_to_precision(symbol, amount))
        except Exception:                              # noqa: BLE001
            return amount

    def last_price(self, symbol: str) -> float:
        if self.ex is None or self.offline:
            raise RuntimeError("нет подключения к бирже")
        t = self.ex.fetch_ticker(symbol)
        return float(t.get("last") or t.get("close") or 0.0)

    def fetch_balance(self) -> dict:
        return self.ex.fetch_balance() if self.ex is not None else {}
