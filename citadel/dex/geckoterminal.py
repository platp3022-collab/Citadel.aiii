# -*- coding: utf-8 -*-
"""
GeckoTerminal — свечи (OHLCV) по конкретному пулу DEX. Ключ не нужен,
лимит ~30 запросов в минуту, до 1000 свечей за запрос.

DexScreener показывает состояние пары «сейчас», но истории свечей в его
публичном API нет — поэтому историю для поиска стратегии берём здесь.
"""
from __future__ import annotations

import logging
import time

from .http import ApiError, HttpClient

log = logging.getLogger("citadel.dex.gecko")

BASE = "https://api.geckoterminal.com/api/v2"

#: chainId в DexScreener → network в GeckoTerminal
NETWORKS = {
    "solana": "solana", "ethereum": "eth", "bsc": "bsc", "base": "base",
    "arbitrum": "arbitrum", "polygon": "polygon_pos", "avalanche": "avax",
    "optimism": "optimism", "ton": "ton", "sui": "sui-network", "tron": "tron",
    "blast": "blast", "linea": "linea", "scroll": "scroll", "zksync": "zksync",
    "sonic": "sonic", "berachain": "berachain", "hyperliquid": "hyperevm",
    "pulsechain": "pulsechain", "cronos": "cronos", "fantom": "ftm",
    "celo": "celo", "moonbeam": "moonbeam", "gnosis": "xdai", "mantle": "mantle",
    "abstract": "abstract", "unichain": "unichain",
}

#: таймфрейм бота → (эндпоинт GeckoTerminal, агрегация)
TIMEFRAMES = {
    "1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15),
    "1h": ("hour", 1), "4h": ("hour", 4), "12h": ("hour", 12), "1d": ("day", 1),
}


def network_of(chain: str) -> str:
    net = NETWORKS.get(chain.lower())
    if not net:
        raise ApiError(f"сеть '{chain}' не поддерживается GeckoTerminal — "
                       f"добавь её в citadel/dex/geckoterminal.py::NETWORKS")
    return net


class GeckoTerminal:
    def __init__(self, client: HttpClient | None = None):
        # версия API фиксируется заголовком, иначе ответ может поменять форму
        self.http = client or HttpClient(
            min_interval=2.1, headers={"Accept": "application/json;version=20230302"})

    def ohlcv(self, chain: str, pool: str, timeframe: str, limit: int = 1000,
              currency: str = "usd") -> list[list[float]]:
        """
        Свечи по пулу, по возрастанию времени, в формате ccxt:
        [ts_ms, open, high, low, close, volume].
        """
        if timeframe not in TIMEFRAMES:
            raise ApiError(f"таймфрейм {timeframe} не поддерживается GeckoTerminal "
                           f"(есть: {', '.join(TIMEFRAMES)})")
        unit, aggregate = TIMEFRAMES[timeframe]
        network = network_of(chain)
        url = f"{BASE}/networks/{network}/pools/{pool}/ohlcv/{unit}"

        rows: dict[int, list[float]] = {}
        before = int(time.time())
        while len(rows) < limit:
            params = {"aggregate": aggregate, "limit": 1000, "currency": currency,
                      "before_timestamp": before}
            data = self.http.get_json(url, params)
            chunk = (((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
            if not chunk:
                break
            for r in chunk:                      # ответ идёт от новых к старым
                if len(r) < 6:
                    continue
                ts = int(r[0])
                rows[ts] = [ts * 1000, float(r[1]), float(r[2]), float(r[3]),
                            float(r[4]), float(r[5] or 0.0)]
            oldest = min(int(r[0]) for r in chunk)
            if oldest >= before:                 # API перестал отдавать более старые свечи
                break
            before = oldest
            if len(chunk) < 1000:                # история пула закончилась
                break
        return [rows[k] for k in sorted(rows)][-limit:]

    # ── ленты пулов ─────────────────────────────────────────────────────────
    def new_pools(self, chain: str = "", pages: int = 1) -> list[dict]:
        """
        Только что созданные пулы — то же, что показывают «новые пары» в
        Axiom и DexScreener. Без ключа, до 20 пулов на страницу.
        """
        return self._pool_feed("new_pools", chain, pages)

    def trending_pools(self, chain: str = "", pages: int = 1) -> list[dict]:
        """Пулы, вокруг которых сейчас движение."""
        return self._pool_feed("trending_pools", chain, pages)

    def _pool_feed(self, feed: str, chain: str, pages: int) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max(1, pages) + 1):
            url = (f"{BASE}/networks/{network_of(chain)}/{feed}" if chain
                   else f"{BASE}/networks/{feed}")
            data = self.http.get_json(url, {"page": page})
            rows = data.get("data") or []
            if not rows:
                break
            out.extend(rows)
            if len(rows) < 20:
                break
        return out

    def pool(self, chain: str, pool: str) -> dict:
        """Справка по пулу: цена, ликвидность, объём — как её видит GeckoTerminal."""
        data = self.http.get_json(f"{BASE}/networks/{network_of(chain)}/pools/{pool}")
        return ((data.get("data") or {}).get("attributes") or {})
