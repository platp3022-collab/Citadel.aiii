# -*- coding: utf-8 -*-
"""
DexMarket — источник данных для DEX в том же интерфейсе, что и биржевой Market,
поэтому движок (индикаторы, бэктест, поиск стратегии, торговый цикл) работает
без единой правки.

Разделение источников:
  • свечи для истории и индикаторов — GeckoTerminal (по адресу пула);
  • текущая цена, ликвидность, объёмы, возраст пары — DexScreener.

«Символ» на DEX — это строка `chain:адрес_пула`, например
`solana:8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj`. Человекочитаемые имена
и метаданные пар хранятся рядом, в dex_pairs.json.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .. import candlecache
from ..config import TIMEFRAME_SECONDS
from ..features import Candles
from .config import DexConfig
from .dexscreener import DexScreener, Pair
from .geckoterminal import GeckoTerminal
from .http import ApiError

log = logging.getLogger("citadel.dex.market")


def split_key(symbol: str) -> tuple[str, str]:
    """`solana:POOL` → ('solana', 'POOL')."""
    chain, _, pool = symbol.partition(":")
    if not pool:
        raise ValueError(f"ожидался ключ вида 'chain:адрес_пула', получено '{symbol}'. "
                         f"Адрес пула — то, что в ссылке DexScreener после сети")
    return chain.lower(), pool


class DexMarket:
    def __init__(self, cfg: DexConfig, offline: bool = False,
                 screener: DexScreener | None = None, gecko: GeckoTerminal | None = None):
        self.cfg = cfg
        self.offline = offline
        self.ex = None                                  # ccxt здесь не участвует
        self.screener = screener or DexScreener()
        self.gecko = gecko or GeckoTerminal()
        self.pairs: dict[str, Pair] = {}
        self._load_pairs()
        if offline:
            log.info("офлайн-режим: только кэш свечей и сохранённые метаданные пар")

    # ── метаданные пар ──────────────────────────────────────────────────────
    def _load_pairs(self) -> None:
        try:
            raw = json.loads(Path(self.cfg.pairs_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for key, data in raw.items():
            try:
                self.pairs[key] = Pair(**data)
            except TypeError:                            # формат поменялся — перезапишем
                continue

    def save_pairs(self) -> None:
        path = Path(self.cfg.pairs_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: vars(p) for k, p in self.pairs.items()}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def remember(self, pair: Pair) -> None:
        self.pairs[pair.key] = pair
        self.save_pairs()

    def pair(self, symbol: str) -> Pair | None:
        return self.pairs.get(symbol)

    def name(self, symbol: str) -> str:
        p = self.pairs.get(symbol)
        return f"{p.name} [{symbol[:14]}…]" if p else symbol

    def refresh_pair(self, symbol: str) -> Pair | None:
        """Свежие ликвидность/объём/цена по пулу. В офлайне — что сохранено."""
        if self.offline:
            return self.pairs.get(symbol)
        chain, pool = split_key(symbol)
        try:
            fresh = self.screener.pair(chain, pool)
        except ApiError as e:
            log.warning("%s: DexScreener недоступен: %s", symbol, e)
            return self.pairs.get(symbol)
        if fresh:
            self.pairs[fresh.key] = fresh
            self.save_pairs()
        return fresh

    # ── интерфейс Market ────────────────────────────────────────────────────
    def cache_path(self, symbol: str, timeframe: str) -> Path:
        return candlecache.path_for(self.cfg.cache_dir, "dex", symbol, timeframe)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int,
                    offline: bool = False, use_cache: bool = True) -> Candles:
        offline = offline or self.offline
        split_key(symbol)                      # ключ проверяем сразу, а не в недрах запроса
        path = self.cache_path(symbol, timeframe)
        cached = candlecache.read(path) if use_cache else []
        if offline:
            if not cached:
                raise SystemExit(f"нет кэша свечей для {symbol} {timeframe} — "
                                 f"сначала запусти `python dexbot.py fetch`")
            return Candles.from_ohlcv(cached[-limit:])

        chain, pool = split_key(symbol)
        try:
            rows = self.gecko.ohlcv(chain, pool, timeframe, limit=limit)
        except ApiError as e:
            log.warning("%s: свечи не пришли (%s)", symbol, e)
            if cached:
                return Candles.from_ohlcv(cached[-limit:])
            raise
        merged = candlecache.merge(cached, rows)
        if use_cache and merged:
            candlecache.write(path, merged)
        step_ms = TIMEFRAME_SECONDS.get(timeframe, 900) * 1000
        closed = [r for r in merged if r[0] + step_ms <= time.time() * 1000]
        return Candles.from_ohlcv(closed[-limit:])

    def last_price(self, symbol: str) -> float:
        """Текущая цена токена в долларах."""
        if not self.offline:
            fresh = self.refresh_pair(symbol)
            if fresh and fresh.price_usd > 0:
                return fresh.price_usd
        p = self.pairs.get(symbol)
        if p and p.price_usd > 0:
            return p.price_usd
        cached = candlecache.read(self.cache_path(symbol, self.cfg.timeframe))
        if cached:
            return float(cached[-1][4])
        raise ApiError(f"нет цены для {symbol}")

    def liquidity(self, symbol: str) -> float:
        p = self.pairs.get(symbol)
        return p.liquidity_usd if p else 0.0

    def min_notional(self, symbol: str) -> float:
        return self.cfg.min_notional

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return amount                                    # на DEX дробим токен как угодно

    def fetch_balance(self) -> dict:
        return {}
