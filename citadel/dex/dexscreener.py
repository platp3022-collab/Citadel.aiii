# -*- coding: utf-8 -*-
"""
DexScreener — витрина пар со всех DEX: поиск, ликвидность, объёмы, возраст,
соцсети, соотношение покупок и продаж. Ключ не нужен, лимит ~300 запросов/мин.

Здесь только доступ к API и нормализация ответа в понятный объект Pair;
решение «торговать или нет» принимает safety.py.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .http import ApiError, HttpClient

log = logging.getLogger("citadel.dex.screener")

BASE = "https://api.dexscreener.com"


@dataclass
class Pair:
    """Пул на DEX в том виде, в каком его отдаёт DexScreener."""
    chain: str                      # solana, ethereum, base, bsc…
    dex: str                        # raydium, uniswap, pancakeswap…
    pair_address: str               # адрес пула — по нему берутся свечи
    base_symbol: str
    base_address: str               # mint/контракт токена, который покупаем
    quote_symbol: str
    quote_address: str
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_h24: float = 0.0
    volume_h1: float = 0.0
    txns_h24_buys: int = 0
    txns_h24_sells: int = 0
    price_change_h24: float = 0.0
    fdv: float = 0.0
    created_at_ms: int = 0
    url: str = ""
    socials: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Идентификатор пары внутри бота: chain:pair_address."""
        return f"{self.chain}:{self.pair_address}"

    @property
    def name(self) -> str:
        return f"{self.base_symbol}/{self.quote_symbol}"

    @property
    def age_hours(self) -> float:
        if not self.created_at_ms:
            return 0.0
        return max(0.0, (time.time() * 1000 - self.created_at_ms) / 3_600_000)

    def describe(self) -> str:
        return (f"{self.name} ({self.chain}/{self.dex}) · ${self.price_usd:.8g} · "
                f"ликв ${self.liquidity_usd:,.0f} · объём24ч ${self.volume_h24:,.0f} · "
                f"возраст {self.age_hours / 24:.1f} д")


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_pair(raw: dict) -> Pair | None:
    base = raw.get("baseToken") or {}
    quote = raw.get("quoteToken") or {}
    if not raw.get("pairAddress") or not base.get("address"):
        return None
    info = raw.get("info") or {}
    txns = (raw.get("txns") or {}).get("h24") or {}
    return Pair(
        chain=str(raw.get("chainId", "")).lower(),
        dex=str(raw.get("dexId", "")).lower(),
        pair_address=str(raw["pairAddress"]),
        base_symbol=str(base.get("symbol", "?")),
        base_address=str(base.get("address", "")),
        quote_symbol=str(quote.get("symbol", "?")),
        quote_address=str(quote.get("address", "")),
        price_usd=_f(raw.get("priceUsd")),
        liquidity_usd=_f((raw.get("liquidity") or {}).get("usd")),
        volume_h24=_f((raw.get("volume") or {}).get("h24")),
        volume_h1=_f((raw.get("volume") or {}).get("h1")),
        txns_h24_buys=int(_f(txns.get("buys"))),
        txns_h24_sells=int(_f(txns.get("sells"))),
        price_change_h24=_f((raw.get("priceChange") or {}).get("h24")),
        fdv=_f(raw.get("fdv")),
        created_at_ms=int(_f(raw.get("pairCreatedAt"))),
        url=str(raw.get("url", "")),
        socials=[str(s.get("url", "")) for s in (info.get("socials") or []) if s.get("url")],
        websites=[str(w.get("url", "")) for w in (info.get("websites") or []) if w.get("url")],
    )


class DexScreener:
    def __init__(self, client: HttpClient | None = None):
        self.http = client or HttpClient(min_interval=0.25)

    def search(self, query: str) -> list[Pair]:
        """Поиск пар по тикеру, названию или адресу токена."""
        data = self.http.get_json(f"{BASE}/latest/dex/search", {"q": query})
        return [p for p in (parse_pair(x) for x in (data.get("pairs") or [])) if p]

    def pairs(self, chain: str, addresses: list[str]) -> list[Pair]:
        """Свежие данные по конкретным пулам (до 30 адресов за запрос)."""
        out: list[Pair] = []
        for chunk in (addresses[i:i + 30] for i in range(0, len(addresses), 30)):
            data = self.http.get_json(f"{BASE}/latest/dex/pairs/{chain}/{','.join(chunk)}")
            raw = data.get("pairs") or data.get("pair") or []
            if isinstance(raw, dict):
                raw = [raw]
            out.extend(p for p in (parse_pair(x) for x in raw) if p)
        return out

    def pair(self, chain: str, address: str) -> Pair | None:
        found = self.pairs(chain, [address])
        return found[0] if found else None

    def token_pairs(self, chain: str, token_address: str) -> list[Pair]:
        """Все пулы конкретного токена — берём самый ликвидный."""
        data = self.http.get_json(f"{BASE}/tokens/v1/{chain}/{token_address}")
        raw = data if isinstance(data, list) else (data.get("pairs") or [])
        return [p for p in (parse_pair(x) for x in raw) if p]

    def trending(self, chain: str = "") -> list[Pair]:
        """
        Свежие «продвигаемые» токены (token-boosts). Это не рейтинг качества,
        а платное продвижение — использовать только как источник кандидатов,
        которые дальше обязаны пройти фильтры safety.py.
        """
        try:
            data = self.http.get_json(f"{BASE}/token-boosts/latest/v1")
        except ApiError as e:
            log.warning("token-boosts недоступен: %s", e)
            return []
        rows = data if isinstance(data, list) else (data.get("data") or [])
        out: list[Pair] = []
        for row in rows[:30]:
            token_chain = str(row.get("chainId", "")).lower()
            token = str(row.get("tokenAddress", ""))
            if not token or (chain and token_chain != chain):
                continue
            try:
                out.extend(self.token_pairs(token_chain, token))
            except ApiError as e:
                log.debug("нет пулов для %s: %s", token, e)
        return out
