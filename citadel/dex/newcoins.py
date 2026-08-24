# -*- coding: utf-8 -*-
"""
Лента новых монет: только что созданные пулы на DEX.

Откуда данные. У Axiom публичного API нет, поэтому берём тот же поток, что
показывают он и DexScreener, из документированного источника — GeckoTerminal
(`new_pools` и `trending_pools`), а метаданные (соцсети, точный баланс покупок
и продаж) добираем из DexScreener по адресу пула.

Зачем отдельный модуль. Обычные фильтры безопасности требуют, чтобы пулу было
хотя бы трое суток — новая монета их не пройдёт никогда. Здесь другой подход:
пару не отбраковывают молча, а показывают с разметкой рисков, чтобы решение
принимал человек, а не молчаливый фильтр.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .dexscreener import DexScreener, Pair
from .geckoterminal import GeckoTerminal
from .http import ApiError

log = logging.getLogger("citadel.dex.new")


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts_ms(iso: str) -> int:
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def pair_from_pool(raw: dict, chain: str = "") -> Pair | None:
    """Пул из ленты GeckoTerminal → тот же объект Pair, что и у DexScreener."""
    attrs = raw.get("attributes") or {}
    address = attrs.get("address")
    if not address:
        return None
    pool_id = str(raw.get("id", ""))
    network = pool_id.split("_", 1)[0] if "_" in pool_id else chain
    rel = raw.get("relationships") or {}

    def token_of(key: str) -> str:
        token = ((rel.get(key) or {}).get("data") or {}).get("id", "")
        return token.split("_", 1)[1] if "_" in token else token

    name = str(attrs.get("name") or "")
    base_symbol, _, quote_symbol = (x.strip() for x in name.partition("/"))
    txns = (attrs.get("transactions") or {}).get("h24") or {}
    volume = attrs.get("volume_usd") or {}
    changes = attrs.get("price_change_percentage") or {}
    dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")

    return Pair(
        chain=_gecko_to_chain(network),
        dex=str(dex),
        pair_address=str(address),
        base_symbol=base_symbol or "?",
        base_address=token_of("base_token"),
        quote_symbol=quote_symbol or "?",
        quote_address=token_of("quote_token"),
        price_usd=_f(attrs.get("base_token_price_usd")),
        liquidity_usd=_f(attrs.get("reserve_in_usd")),
        volume_h24=_f(volume.get("h24")),
        volume_h1=_f(volume.get("h1")),
        txns_h24_buys=int(_f(txns.get("buys"))),
        txns_h24_sells=int(_f(txns.get("sells"))),
        price_change_h24=_f(changes.get("h24")),
        fdv=_f(attrs.get("fdv_usd")),
        created_at_ms=_ts_ms(str(attrs.get("pool_created_at") or "")),
    )


#: обратная карта сетей GeckoTerminal → chainId, как их называет DexScreener
_BACK = {"eth": "ethereum", "polygon_pos": "polygon", "avax": "avalanche",
         "ftm": "fantom", "xdai": "gnosis", "sui-network": "sui", "hyperevm": "hyperliquid"}


def _gecko_to_chain(network: str) -> str:
    return _BACK.get(network, network)


@dataclass
class NewCoin:
    """Новая монета с разметкой рисков — решение принимает человек."""
    pair: Pair
    flags: list[str] = field(default_factory=list)      # что настораживает
    good: list[str] = field(default_factory=list)       # что в плюс
    score: float = 0.0

    @property
    def age_hours(self) -> float:
        return self.pair.age_hours

    def describe(self) -> str:
        age = (f"{self.age_hours * 60:.0f} мин" if self.age_hours < 1
               else f"{self.age_hours:.1f} ч" if self.age_hours < 48
               else f"{self.age_hours / 24:.1f} д")
        return (f"{self.pair.name} ({self.pair.chain}/{self.pair.dex}) · возраст {age} · "
                f"ликв ${self.pair.liquidity_usd:,.0f} · объём24ч ${self.pair.volume_h24:,.0f} · "
                f"${self.pair.price_usd:.8g}")


def assess(p: Pair, min_liquidity: float = 10_000.0,
           min_volume: float = 20_000.0) -> NewCoin:
    """Раскладывает пару на «что настораживает» и «что в плюс»."""
    flags, good = [], []
    if p.liquidity_usd < min_liquidity:
        flags.append(f"ликвидность ${p.liquidity_usd:,.0f} — выйти будет дорого")
    elif p.liquidity_usd > min_liquidity * 10:
        good.append(f"ликвидность ${p.liquidity_usd:,.0f}")
    if p.volume_h24 < min_volume:
        flags.append(f"оборот ${p.volume_h24:,.0f} за сутки — почти не торгуют")
    elif p.volume_h24 > min_volume * 5:
        good.append(f"оборот ${p.volume_h24:,.0f}")
    if p.age_hours < 1:
        flags.append("пулу меньше часа — на этом сроке уходит в ноль большинство")
    elif p.age_hours > 24:
        good.append(f"пережил {p.age_hours / 24:.1f} суток")
    if p.txns_h24_sells == 0 and p.txns_h24_buys > 20:
        flags.append("продаж нет вообще — возможен honeypot")
    elif p.txns_h24_sells:
        skew = p.txns_h24_buys / p.txns_h24_sells
        if skew > 5:
            flags.append(f"покупок в {skew:.1f}× больше продаж")
        elif 0.7 <= skew <= 1.6:
            good.append("покупки и продажи сбалансированы")
    if p.liquidity_usd > 0:
        v2l = p.volume_h24 / p.liquidity_usd
        if v2l > 30:
            flags.append(f"оборот в {v2l:.0f}× ликвидности — похоже на накрутку")
        if p.fdv and p.fdv / p.liquidity_usd > 500:
            flags.append(f"FDV в {p.fdv / p.liquidity_usd:.0f}× ликвидности")
    if p.socials or p.websites:
        good.append("есть сайт или соцсети")
    else:
        flags.append("ни сайта, ни соцсетей")

    score = len(good) - 1.6 * len(flags)
    if p.liquidity_usd > 0:
        score += min(2.0, p.liquidity_usd / 250_000)
    return NewCoin(pair=p, flags=flags, good=good, score=score)


class NewCoinScanner:
    def __init__(self, gecko: GeckoTerminal | None = None,
                 screener: DexScreener | None = None):
        self.gecko = gecko or GeckoTerminal()
        self.screener = screener or DexScreener()

    def fetch(self, chain: str = "solana", pages: int = 1,
              trending: bool = False, enrich: bool = True) -> list[NewCoin]:
        """Тянет ленту, добирает метаданные и раскладывает риски."""
        try:
            rows = (self.gecko.trending_pools(chain, pages) if trending
                    else self.gecko.new_pools(chain, pages))
        except ApiError as e:
            log.warning("лента пулов недоступна: %s", e)
            return []
        pairs = [p for p in (pair_from_pool(r, chain) for r in rows) if p]
        if enrich and pairs:
            pairs = self._enrich(pairs)
        coins = [assess(p) for p in pairs]
        coins.sort(key=lambda c: c.score, reverse=True)
        return coins

    def _enrich(self, pairs: list[Pair]) -> list[Pair]:
        """DexScreener знает про соцсети и точнее считает сделки — спрашиваем его."""
        by_chain: dict[str, list[str]] = {}
        for p in pairs:
            by_chain.setdefault(p.chain, []).append(p.pair_address)
        fresh: dict[str, Pair] = {}
        for chain, addresses in by_chain.items():
            try:
                for p in self.screener.pairs(chain, addresses):
                    fresh[p.key] = p
            except ApiError as e:
                log.debug("DexScreener не ответил по %s: %s", chain, e)
        return [fresh.get(p.key, p) for p in pairs]
