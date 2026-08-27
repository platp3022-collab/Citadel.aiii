#!/usr/bin/env python3
"""
Свежие мем-коины с терминалов Axiom / FOMO (Solana-лончпады: pump.fun, bonk.fun и др.).

Модуль ловит монеты в первые минуты жизни и считает по ним ту же математику,
на которую смотрят в терминале Axiom/FOMO: холдеры, приток холдеров, давление
покупок, объём к ликвидности, доля дева, топ-10, прогресс бондинг-кривой,
mint/freeze authority, блокировка LP, история дева (сколько монет он уже слил).

Источники данных (все публичные, без ключей):
  • Jupiter Token API v2 — /tokens/v2/recent: свежесозданные токены Solana
    с холдерами, аудитом и статистикой 5м/1ч (основной источник)
  • pump.fun frontend API — монеты до миграции (опционально, часто под Cloudflare)
  • DexScreener — пул, соцсети, объёмы (обогащение шорт-листа)
  • RugCheck — риск-скор по минту

У самих Axiom и FOMO публичного API нет (их эндпоинты требуют авторизации
аккаунта), поэтому бот собирает те же данные из открытых источников,
а в алерт кладёт прямые ссылки на монету в этих терминалах — открыл и торгуешь.

Запуск отдельно от основного бота:
    python axiom_scout.py --once --dry
    python axiom_scout.py                 # цикл, алерты в Telegram
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import aiohttp

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("axiom_scout")

# ════════════════════════════════════════════════════════════════════════════
#  КОНФИГ — правь прямо здесь (или через CONFIG["fresh"] в memebot.py)
# ════════════════════════════════════════════════════════════════════════════

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 45,          # как часто опрашивать ленту новых монет

    # ---- окно «только что вышедших» ----
    "min_age_minutes": 2,            # раньше 2 минут данных ещё нет
    "max_age_minutes": 240,          # позже 4 часов это уже не «новая» монета

    # ---- жёсткие фильтры (не прошёл — даже не считаем скор) ----
    "min_liquidity_usd": 8000,
    "min_mcap_usd": 15000,
    "max_mcap_usd": 3000000,         # 0 = без верхней границы
    "min_holders": 50,
    "min_buys_5m": 20,
    "min_volume_5m_usd": 4000,
    "min_volume_usd": 0,             # общий объём с запуска (колонка Volume в Axiom)
    "min_fees_sol": 0.0,             # Global Fees Paid — оценка по объёму, см. fee_rate
    "fee_rate": 0.01,                # 1% комиссии протокола+создателя (pump/bonk)
    "quote_tokens": [],              # [] = любые; напр. ["SOL", "USD1"]
    "max_dev_pct": 6.0,              # сколько supply держит создатель
    "max_top10_pct": 30.0,
    "max_dev_migrations": 8,         # сколько монет дев уже успел выпустить
    "require_mint_revoked": True,
    "require_freeze_revoked": True,
    "launchpads": [],                # [] = любые; напр. ["pump.fun", "bonk.fun"]
    "blacklist_words": ["test", "rug", "scam", "airdrop"],

    # ---- алерты ----
    "min_score": 62,
    "cooldown_minutes": 90,          # не брать одну монету чаще, чем раз в N минут
    "rescore_delta": 8,              # повтор только если скор вырос на N
    "max_per_scan": 4,
    "notify": False,                 # разбор монеты в чат. False = только сделки

    # ---- источники и обогащение ----
    "sources": {"jupiter": True, "pumpfun": False, "dexscreener": True},
    "enrich_dexscreener": True,      # догружать пул/соцсети для шорт-листа
    "rugcheck": True,
    "shortlist_limit": 12,           # сколько кандидатов тянуть в тяжёлые проверки

    # ---- ссылки на терминалы ----
    "terminals": {
        "Axiom": "https://axiom.trade/t/{mint}",
        "FOMO": "https://fomo.biz/token/{mint}",
        "GMGN": "https://gmgn.ai/sol/token/{mint}",
        "Photon": "https://photon-sol.tinyastro.io/en/lp/{pool}",
        "BullX": "https://neo.bullx.io/terminal?chainId=1399811149&address={mint}",
    },
    "terminals_shown": ["Axiom", "FOMO", "GMGN", "Photon"],

    # ---- автопилот: бот сам решает, норм монета или нет ----
    "auto": {
        "enabled": True,
        "only_enter": True,        # слать только вердикт «норм», «наблюдать» — молча в базу
        "enter_score": 70,         # от какого скора монета считается «норм»
        "watch_score": 55,         # ниже — «мимо»
        "block_on_red": True,      # любой 🔴-флаг → не «норм»
    },
    "news": {"enabled": True, "bonus_max": 8},
    "llm": {                       # финальное слово нейросети (нужен ANTHROPIC_API_KEY)
        "enabled": True,
        "model": "claude-sonnet-5",
        "min_score": 58,           # гонять LLM только по кандидатам от этого скора
        "max_per_scan": 3,
        "veto_risk": 9,            # отбраковка только при почти явном скаме
    },

    "storage_path": "data/memebot.db",
}


# ════════════════════════════════════════════════════════════════════════════
#  ПРЕСЕТЫ — «кинул боту настройку AXIOM и он сам работает»
# ════════════════════════════════════════════════════════════════════════════

PRESETS: dict[str, dict[str, Any]] = {
    # Ровно те фильтры, что стоят в терминале Axiom (вкладки Protocols + Metrics):
    # Volume от $50, Market Cap от $7K, Global Fees от 0.3 SOL, протоколы Pump и Bonk,
    # квота SOL / USD1, ликвидность и B.curve не ограничены, вкладка Audit пустая.
    # Отсев мусора здесь делает не фильтр, а скоринг и вердикт нейросети.
    "axiom": {
        "min_age_minutes": 1, "max_age_minutes": 240,
        "min_liquidity_usd": 0, "min_mcap_usd": 7000, "max_mcap_usd": 0,
        "min_volume_usd": 50, "min_fees_sol": 0.3, "fee_rate": 0.01,
        "min_holders": 0, "min_buys_5m": 0, "min_volume_5m_usd": 0,
        "launchpads": ["pump", "bonk"], "quote_tokens": ["SOL", "USD1"],
        "max_dev_pct": 100.0, "max_top10_pct": 100.0, "max_dev_migrations": 9999,
        "require_mint_revoked": False, "require_freeze_revoked": False,
        "min_score": 58, "interval_seconds": 40, "max_per_scan": 4,
        "shortlist_limit": 15,
        "terminals_shown": ["Axiom", "FOMO", "GMGN", "Photon"],
        "auto": {"enabled": True, "only_enter": True, "enter_score": 58,
                 "watch_score": 45, "block_on_red": True},
    },
    # То же самое, но с жёстким предотсевом по ончейну — меньше шума, меньше находок.
    "axiom_strict": {
        "min_age_minutes": 3, "max_age_minutes": 180,
        "min_liquidity_usd": 12000, "min_mcap_usd": 25000, "max_mcap_usd": 3000000,
        "min_volume_usd": 50, "min_fees_sol": 0.3,
        "min_holders": 80, "min_buys_5m": 30, "min_volume_5m_usd": 8000,
        "launchpads": ["pump", "bonk"], "quote_tokens": ["SOL", "USD1"],
        "max_dev_pct": 4.0, "max_top10_pct": 25.0, "max_dev_migrations": 6,
        "require_mint_revoked": True, "require_freeze_revoked": True,
        "min_score": 68, "interval_seconds": 45, "max_per_scan": 4,
        "terminals_shown": ["Axiom", "FOMO", "GMGN", "Photon"],
        "auto": {"enabled": True, "only_enter": True, "enter_score": 72,
                 "watch_score": 58, "block_on_red": True},
    },
    # FOMO: ранний вход, ещё на кривой, планка ниже, риск выше.
    "fomo": {
        "min_age_minutes": 2, "max_age_minutes": 90,
        "min_liquidity_usd": 6000, "min_mcap_usd": 15000, "max_mcap_usd": 1500000,
        "min_holders": 40, "min_buys_5m": 25, "min_volume_5m_usd": 4000,
        "max_dev_pct": 5.0, "max_top10_pct": 30.0, "max_dev_migrations": 8,
        "require_mint_revoked": True, "require_freeze_revoked": True,
        "min_score": 60, "interval_seconds": 30, "max_per_scan": 5,
        "terminals_shown": ["FOMO", "Axiom", "GMGN", "BullX"],
        "auto": {"enabled": True, "only_enter": True, "enter_score": 65,
                 "watch_score": 50, "block_on_red": True},
    },
    # Осторожный: только зрелые, уже мигрировавшие, с чистым распределением.
    "safe": {
        "min_age_minutes": 20, "max_age_minutes": 720,
        "min_liquidity_usd": 40000, "min_mcap_usd": 100000, "max_mcap_usd": 8000000,
        "min_holders": 300, "min_buys_5m": 40, "min_volume_5m_usd": 15000,
        "max_dev_pct": 2.0, "max_top10_pct": 18.0, "max_dev_migrations": 3,
        "min_score": 75, "interval_seconds": 90, "max_per_scan": 3,
        "auto": {"enabled": True, "only_enter": True, "enter_score": 78,
                 "watch_score": 62, "block_on_red": True},
    },
    # Дегенский: ловим самый ранний вход, фильтры минимальные. Готовь потери.
    "degen": {
        "min_age_minutes": 1, "max_age_minutes": 45,
        "min_liquidity_usd": 3000, "min_mcap_usd": 8000, "max_mcap_usd": 800000,
        "min_holders": 20, "min_buys_5m": 15, "min_volume_5m_usd": 2000,
        "max_dev_pct": 8.0, "max_top10_pct": 40.0, "max_dev_migrations": 12,
        "min_score": 52, "interval_seconds": 25, "max_per_scan": 6,
        "auto": {"enabled": True, "only_enter": False, "enter_score": 60,
                 "watch_score": 45, "block_on_red": False},
    },
}


def merge_conf(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Слить конфиги на два уровня вглубь (auto/llm/news/sources — словари)."""
    out = dict(base)
    for key, val in (patch or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **val}
        else:
            out[key] = val
    return out


def preset_conf(name: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Готовый конфиг по имени пресета: preset_conf("AXIOM")."""
    key = str(name or "").strip().lower()
    conf = merge_conf(DEFAULTS, PRESETS.get(key, {}))
    conf["preset"] = key if key in PRESETS else "default"
    return merge_conf(conf, extra or {})

JUP_LITE = "https://lite-api.jup.ag"
JUP_PRO = "https://api.jup.ag"
PUMP_API = "https://frontend-api-v3.pump.fun"
DEX_API = "https://api.dexscreener.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
UA = {"User-Agent": "axiom-scout/1.0 (+research bot)", "Accept": "application/json"}

# Названия протоколов в Axiom → как они приходят в API
LAUNCHPAD_ALIASES: dict[str, list[str]] = {
    "pump": ["pump.fun", "pumpfun", "pump", "pumpswap", "pump amm", "pump swap"],
    "bonk": ["bonk.fun", "letsbonk.fun", "letsbonk", "bonk", "raydium launchlab", "launchlab"],
    "bags": ["bags", "bags.fm"],
    "believe": ["believe", "launchacoin"],
    "moonshot": ["moonshot", "moonit"],
    "boop": ["boop", "boop.fun"],
    "heaven": ["heaven", "heaven.xyz"],
    "meteora": ["meteora", "met dbc", "dbc"],
    "raydium": ["raydium", "raydium amm", "raydium cpmm"],
    "jupiter": ["jup studio", "jupiter studio", "jupiter"],
    "moonit": ["moonit"],
    "printr": ["printr"],
    "mayhem": ["mayhem"],
    "bonkers": ["bonkers"],
    "stonkfun": ["stonkfun", "stonk.fun"],
    "surge": ["surge"],
    "soar": ["soar"],
    "liquid": ["liquid"],
    "riserich": ["rise rich", "riserich"],
}


def launchpad_matches(launchpad: str, allowed: list[str]) -> bool:
    """Совпадает ли лончпад монеты с выбранными в настройках протоколами."""
    if not allowed:
        return True
    pad = str(launchpad or "").strip().lower()
    if not pad:
        return True                   # источник не сказал лончпад — не выбрасываем
    for item in allowed:
        key = str(item).strip().lower()
        names = LAUNCHPAD_ALIASES.get(key, [key])
        for name in names:
            if name == pad or name in pad or pad in name:
                return True
    return False


KNOWN_LAUNCHPADS = {
    "pump.fun": "💊", "pumpfun": "💊", "bonk.fun": "🐕", "letsbonk.fun": "🐕",
    "believe": "🟢", "moonshot": "🌙", "raydium": "⚡", "boop": "🔵",
    "jup studio": "🪐", "meteora": "🌊", "heaven": "☁️",
}


# ════════════════════════════════════════════════════════════════════════════
#  МЕЛОЧЁВКА
# ════════════════════════════════════════════════════════════════════════════

def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur if cur is not None else default


def esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_usd(v: float) -> str:
    v = num(v)
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


def fmt_price(v: float) -> str:
    v = num(v)
    if v <= 0:
        return "—"
    if v >= 1:
        return f"${v:,.4f}"
    if v >= 0.0001:
        return f"${v:.6f}".rstrip("0")
    return f"${v:.10f}".rstrip("0")


def fmt_age(minutes: float) -> str:
    minutes = max(0.0, num(minutes))
    if minutes < 60:
        return f"{minutes:.0f} мин"
    if minutes < 1440:
        return f"{minutes/60:.1f} ч"
    return f"{minutes/1440:.1f} д"


def piecewise(x: float, points: list[tuple[float, float]]) -> float:
    """Линейная интерполяция по контрольным точкам [(x, y), ...]."""
    if not points:
        return 0.0
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def iso_ts(value: Any) -> float:
    """ISO-8601 (или unix-время) → unix timestamp. 0, если не разобрали."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    if not isinstance(value, str) or not value.strip():
        return 0.0
    raw = value.strip().replace("Z", "+00:00")
    raw = re.sub(r"\.(\d{6})\d+", r".\1", raw)     # микросекунды сверх 6 знаков
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ════════════════════════════════════════════════════════════════════════════
#  МОДЕЛЬ МОНЕТЫ
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Launch:
    """Свежая монета в том виде, в каком её показывает терминал."""
    mint: str
    symbol: str = ""
    name: str = ""
    launchpad: str = ""
    source: str = ""
    created_ts: float = 0.0

    price_usd: float = 0.0
    mcap: float = 0.0
    fdv: float = 0.0
    liquidity: float = 0.0

    holders: int = 0
    holders_change_5m: float = 0.0        # прирост холдеров за 5 минут
    traders_5m: int = 0
    organic_buyers_5m: int = 0

    buys_5m: int = 0
    sells_5m: int = 0
    buy_vol_5m: float = 0.0
    sell_vol_5m: float = 0.0
    buys_1h: int = 0
    sells_1h: int = 0
    vol_1h: float = 0.0
    vol_24h: float = 0.0              # объём с запуска (для свежака ≈ весь оборот)
    quote_symbol: str = ""            # за что торгуется: SOL / USDC / USD1 ...
    fees_sol: float = 0.0             # Global Fees Paid — оценка из объёма

    price_change_5m: float = 0.0
    price_change_1h: float = 0.0

    dev_pct: float = 0.0                  # доля supply у создателя
    top10_pct: float = 0.0
    dev_migrations: int = 0               # сколько монет дев уже выпускал
    mint_revoked: bool | None = None
    freeze_revoked: bool | None = None
    locked_ratio: float | None = None     # доля залоченного/сожжённого LP (0..1)
    honeypot: bool = False
    rugpull_flag: bool = False

    organic_score: float = 0.0
    organic_label: str = ""
    verified: bool = False

    bonding_curve: float | None = None    # % прогресса до миграции
    graduated: bool = False               # уже переехал на DEX
    pool: str = ""
    dex: str = ""

    twitter: str = ""
    telegram: str = ""
    website: str = ""

    rug_score: float | None = None        # RugCheck score_normalised (0 = чисто)
    rug_flags: list[str] = field(default_factory=list)
    rug_danger: bool = False

    raw: dict = field(default_factory=dict)

    @property
    def age_minutes(self) -> float:
        if not self.created_ts:
            return 0.0
        return max(0.0, (time.time() - self.created_ts) / 60.0)

    @property
    def vol_5m(self) -> float:
        return self.buy_vol_5m + self.sell_vol_5m

    @property
    def vol_total(self) -> float:
        """Оборот монеты: берём самое большое известное окно."""
        return max(self.vol_24h, self.vol_1h, self.vol_5m)

    def estimate_fees_sol(self, fee_rate: float, sol_price: float) -> float:
        """Оценка Global Fees Paid: оборот × комиссия ÷ цена SOL.

        Точного числа в открытых API нет, Axiom считает его по своим данным,
        поэтому это приближение — для отсева совсем мёртвых монет его хватает.
        """
        if sol_price <= 0:
            return 0.0
        self.fees_sol = self.vol_total * max(fee_rate, 0.0) / sol_price
        return self.fees_sol

    @property
    def buy_ratio_5m(self) -> float:
        total = self.buys_5m + self.sells_5m
        return self.buys_5m / total if total else 0.0

    @property
    def title(self) -> str:
        return f"${self.symbol}" if self.symbol else self.mint[:8]

    def merge(self, other: "Launch") -> None:
        """Дозаполнить пустые поля данными из другого источника."""
        for key, val in other.__dict__.items():
            if key in ("mint", "raw"):
                continue
            cur = getattr(self, key, None)
            empty = cur in (None, "", 0, 0.0, [], False)
            if empty and val not in (None, "", 0, 0.0, [], False):
                setattr(self, key, val)


# ════════════════════════════════════════════════════════════════════════════
#  ПАРСЕРЫ ИСТОЧНИКОВ
# ════════════════════════════════════════════════════════════════════════════

def parse_jupiter(item: dict) -> Launch | None:
    """Токен из Jupiter Token API v2 (/tokens/v2/recent | /search)."""
    mint = str(item.get("id") or item.get("address") or item.get("mint") or "").strip()
    if not mint:
        return None

    s5 = item.get("stats5m") or {}
    s1h = item.get("stats1h") or {}
    s24 = item.get("stats24h") or item.get("stats6h") or {}
    audit = item.get("audit") or {}
    first_pool = item.get("firstPool") or {}

    created = iso_ts(first_pool.get("createdAt")) or iso_ts(item.get("createdAt")) \
        or iso_ts(item.get("firstPoolCreatedAt"))

    graduated_at = iso_ts(item.get("graduatedAt"))
    bonding = item.get("bondingCurve")
    bonding_pct = num(bonding, -1)
    if bonding_pct < 0:
        bonding_pct = None
    elif bonding_pct <= 1.0 and bonding_pct > 0:      # доля вместо процентов
        bonding_pct *= 100.0

    locked = audit.get("lpBurnedPct")
    if locked is None:
        locked = audit.get("lockedRatio")
    locked_ratio = None
    if locked is not None:
        locked_ratio = num(locked)
        if locked_ratio > 1.0:
            locked_ratio /= 100.0

    launch = Launch(
        mint=mint,
        symbol=str(item.get("symbol") or "")[:24],
        name=str(item.get("name") or "")[:64],
        launchpad=str(item.get("launchpad") or "")[:32],
        source="jupiter",
        created_ts=created,
        price_usd=num(item.get("usdPrice")) or num(item.get("price")),
        mcap=num(item.get("mcap")) or num(item.get("marketCap")),
        fdv=num(item.get("fdv")),
        liquidity=num(item.get("liquidity")),
        holders=int(num(item.get("holderCount"))),
        holders_change_5m=num(s5.get("holderChange")),
        traders_5m=int(num(s5.get("numTraders"))),
        organic_buyers_5m=int(num(s5.get("numOrganicBuyers"))),
        buys_5m=int(num(s5.get("numBuys"))),
        sells_5m=int(num(s5.get("numSells"))),
        buy_vol_5m=num(s5.get("buyVolume")),
        sell_vol_5m=num(s5.get("sellVolume")),
        buys_1h=int(num(s1h.get("numBuys"))),
        sells_1h=int(num(s1h.get("numSells"))),
        vol_1h=num(s1h.get("buyVolume")) + num(s1h.get("sellVolume")),
        vol_24h=num(s24.get("buyVolume")) + num(s24.get("sellVolume")),
        quote_symbol=str(first_pool.get("quoteAsset") or item.get("quoteSymbol") or ""),
        price_change_5m=num(s5.get("priceChange")),
        price_change_1h=num(s1h.get("priceChange")),
        dev_pct=num(audit.get("devBalancePercentage")),
        top10_pct=num(audit.get("topHoldersPercentage")),
        dev_migrations=int(num(audit.get("devMigrations"))),
        mint_revoked=_flag(audit.get("mintAuthorityDisabled")),
        freeze_revoked=_flag(audit.get("freezeAuthorityDisabled")),
        locked_ratio=locked_ratio,
        honeypot=bool(audit.get("blockaidHoneypot")),
        rugpull_flag=bool(audit.get("blockaidRugpull")),
        organic_score=num(item.get("organicScore")),
        organic_label=str(item.get("organicScoreLabel") or ""),
        verified=bool(item.get("isVerified")),
        bonding_curve=bonding_pct,
        graduated=bool(item.get("graduatedPool") or graduated_at),
        pool=str(item.get("graduatedPool") or first_pool.get("id") or ""),
        twitter=str(item.get("twitter") or ""),
        telegram=str(item.get("telegram") or ""),
        website=str(item.get("website") or ""),
        raw=item,
    )
    if launch.graduated and launch.bonding_curve is None:
        launch.bonding_curve = 100.0
    return launch


def _flag(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        return None
    return bool(value)


def parse_pumpfun(item: dict) -> Launch | None:
    """Монета из ленты pump.fun (ещё на бондинг-кривой)."""
    mint = str(item.get("mint") or "").strip()
    if not mint:
        return None
    created = iso_ts(item.get("created_timestamp"))
    mcap = num(item.get("usd_market_cap")) or num(item.get("market_cap"))
    progress = item.get("bonding_curve_progress")
    bonding = num(progress, -1)
    if bonding < 0:
        # прогресс кривой ≈ собранная часть от порога миграции (~$69K на pump.fun)
        bonding = min(100.0, mcap / 69000.0 * 100.0) if mcap else None
    elif bonding <= 1.0 and bonding > 0:
        bonding *= 100.0

    return Launch(
        mint=mint,
        symbol=str(item.get("symbol") or "")[:24],
        name=str(item.get("name") or "")[:64],
        launchpad="pump.fun",
        source="pumpfun",
        created_ts=created,
        mcap=mcap,
        holders=int(num(item.get("holder_count"))),
        bonding_curve=bonding,
        graduated=bool(item.get("complete") or item.get("raydium_pool")),
        pool=str(item.get("raydium_pool") or ""),
        twitter=str(item.get("twitter") or ""),
        telegram=str(item.get("telegram") or ""),
        website=str(item.get("website") or ""),
        raw=item,
    )


def parse_dexscreener_pair(pair: dict) -> Launch | None:
    """Пара DexScreener → Launch (запасной источник и обогащение)."""
    if str(pair.get("chainId", "")).lower() != "solana":
        return None
    base = pair.get("baseToken") or {}
    mint = str(base.get("address") or "").strip()
    if not mint:
        return None

    txns5 = dig(pair, "txns", "m5", default={}) or {}
    txns1h = dig(pair, "txns", "h1", default={}) or {}
    vol5 = num(dig(pair, "volume", "m5"))
    socials = {}
    for s in (dig(pair, "info", "socials", default=[]) or []):
        if isinstance(s, dict) and s.get("type") and s.get("url"):
            socials[str(s["type"]).lower()] = str(s["url"])
    websites = dig(pair, "info", "websites", default=[]) or []
    website = ""
    if websites and isinstance(websites[0], dict):
        website = str(websites[0].get("url") or "")

    return Launch(
        mint=mint,
        symbol=str(base.get("symbol") or "")[:24],
        name=str(base.get("name") or "")[:64],
        source="dexscreener",
        created_ts=iso_ts(pair.get("pairCreatedAt")),
        price_usd=num(pair.get("priceUsd")),
        mcap=num(pair.get("marketCap")) or num(pair.get("fdv")),
        fdv=num(pair.get("fdv")),
        liquidity=num(dig(pair, "liquidity", "usd")),
        buys_5m=int(num(txns5.get("buys"))),
        sells_5m=int(num(txns5.get("sells"))),
        buy_vol_5m=vol5 / 2.0,               # DexScreener не делит объём — берём пополам
        sell_vol_5m=vol5 / 2.0,
        buys_1h=int(num(txns1h.get("buys"))),
        sells_1h=int(num(txns1h.get("sells"))),
        vol_1h=num(dig(pair, "volume", "h1")),
        vol_24h=num(dig(pair, "volume", "h24")),
        quote_symbol=str(dig(pair, "quoteToken", "symbol", default="") or ""),
        price_change_5m=num(dig(pair, "priceChange", "m5")),
        price_change_1h=num(dig(pair, "priceChange", "h1")),
        pool=str(pair.get("pairAddress") or ""),
        dex=str(pair.get("dexId") or ""),
        twitter=socials.get("twitter") or socials.get("x") or "",
        telegram=socials.get("telegram") or "",
        website=website,
        raw=pair,
    )


# ════════════════════════════════════════════════════════════════════════════
#  СБОР ДАННЫХ
# ════════════════════════════════════════════════════════════════════════════

class LaunchFeed:
    """Тянет свежие монеты и обогащает их данными пула и риск-чеков."""

    def __init__(self, session: aiohttp.ClientSession, conf: dict[str, Any]):
        self.session = session
        self.conf = conf
        self.timeout = aiohttp.ClientTimeout(total=20)
        self.api_key = os.environ.get("JUPITER_API_KEY", "").strip()
        self.jup_base = JUP_PRO if self.api_key else JUP_LITE
        self._sol_price = 0.0
        self._sol_price_ts = 0.0

    async def _get(self, url: str, params: dict | None = None,
                   headers: dict | None = None, retries: int = 2) -> Any:
        for attempt in range(retries + 1):
            try:
                async with self.session.get(url, params=params, headers=headers,
                                            timeout=self.timeout) as r:
                    if r.status == 429:
                        await asyncio.sleep(num(r.headers.get("Retry-After"), 3))
                        continue
                    if r.status >= 500:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    if r.status != 200:
                        log.debug("HTTP %s %s", r.status, url)
                        return None
                    return await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.debug("сеть %s: %s", url, e)
                await asyncio.sleep(1.0 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                log.debug("ответ %s: %s", url, e)
                return None
        return None

    # ---------- Jupiter ----------

    def _jup_headers(self) -> dict:
        return {"x-api-key": self.api_key} if self.api_key else {}

    async def jupiter_recent(self) -> list[Launch]:
        data = await self._get(f"{self.jup_base}/tokens/v2/recent",
                               headers=self._jup_headers())
        items = data if isinstance(data, list) else (data or {}).get("tokens") or []
        out = []
        for it in items:
            if isinstance(it, dict):
                launch = parse_jupiter(it)
                if launch:
                    out.append(launch)
        return out

    async def jupiter_token(self, mint: str) -> Launch | None:
        data = await self._get(f"{self.jup_base}/tokens/v2/search",
                               params={"query": mint}, headers=self._jup_headers())
        items = data if isinstance(data, list) else (data or {}).get("tokens") or []
        for it in items:
            if isinstance(it, dict) and str(it.get("id") or "") == mint:
                return parse_jupiter(it)
        return parse_jupiter(items[0]) if items and isinstance(items[0], dict) else None

    async def sol_price(self) -> float:
        """Цена SOL в долларах (кэш 5 минут) — нужна для оценки комиссий в SOL."""
        if self._sol_price and time.time() - self._sol_price_ts < 300:
            return self._sol_price
        data = await self._get(f"{self.jup_base}/price/v3",
                               params={"ids": SOL_MINT}, headers=self._jup_headers())
        price = num(dig(data, SOL_MINT, "usdPrice")) or num(dig(data, "data", SOL_MINT, "price"))
        if not price:
            pairs = await self._get(f"{DEX_API}/latest/dex/tokens/{SOL_MINT}")
            for pair in (dig(pairs, "pairs", default=[]) or [])[:5]:
                price = num(dig(pair, "priceUsd"))
                if price:
                    break
        if price:
            self._sol_price, self._sol_price_ts = price, time.time()
        return self._sol_price

    # ---------- pump.fun ----------

    async def pumpfun_recent(self, limit: int = 50) -> list[Launch]:
        data = await self._get(f"{PUMP_API}/coins", params={
            "offset": 0, "limit": limit, "sort": "created_timestamp",
            "order": "DESC", "includeNsfw": "false"})
        items = data if isinstance(data, list) else (data or {}).get("coins") or []
        out = []
        for it in items:
            if isinstance(it, dict):
                launch = parse_pumpfun(it)
                if launch:
                    out.append(launch)
        return out

    # ---------- DexScreener ----------

    async def dexscreener_new(self) -> list[Launch]:
        """Свежие профили токенов → пары (запасной источник, если Jupiter молчит)."""
        profiles = await self._get(f"{DEX_API}/token-profiles/latest/v1")
        mints = [str(p.get("tokenAddress")) for p in (profiles or [])
                 if isinstance(p, dict) and str(p.get("chainId", "")).lower() == "solana"
                 and p.get("tokenAddress")]
        return await self.dexscreener_pairs(mints[:60])

    async def dexscreener_pairs(self, mints: list[str]) -> list[Launch]:
        best: dict[str, Launch] = {}
        for batch in chunks([m for m in mints if m], 30):
            data = await self._get(f"{DEX_API}/latest/dex/tokens/" + ",".join(batch))
            for pair in (dig(data, "pairs", default=[]) or []):
                if not isinstance(pair, dict):
                    continue
                launch = parse_dexscreener_pair(pair)
                if not launch:
                    continue
                cur = best.get(launch.mint)
                if not cur or launch.liquidity > cur.liquidity:
                    best[launch.mint] = launch
        return list(best.values())

    # ---------- RugCheck ----------

    async def rugcheck(self, launch: Launch) -> None:
        url = f"https://api.rugcheck.xyz/v1/tokens/{launch.mint}/report/summary"
        data = await self._get(url, retries=1)
        if not isinstance(data, dict):
            return
        score = data.get("score_normalised")
        if isinstance(score, (int, float)):
            launch.rug_score = float(score)
        for item in (data.get("risks") or [])[:8]:
            if not isinstance(item, dict):
                continue
            level = str(item.get("level", "")).lower()
            name = str(item.get("name", ""))
            if not name:
                continue
            if level in ("danger", "high"):
                launch.rug_danger = True
                launch.rug_flags.append(f"🔴 {name}")
            elif level in ("warn", "warning", "medium"):
                launch.rug_flags.append(f"🟡 {name}")
            low = name.lower()
            if "lp" in low and ("unlock" in low or "not burned" in low or "unburn" in low):
                launch.locked_ratio = launch.locked_ratio or 0.0
            if "mint authority" in low:
                launch.mint_revoked = False
            if "freeze authority" in low:
                launch.freeze_revoked = False

    # ---------- сводный сбор ----------

    async def collect(self) -> list[Launch]:
        src = self.conf.get("sources") or {}
        tasks: list[Awaitable] = []
        if src.get("jupiter", True):
            tasks.append(self.jupiter_recent())
        if src.get("pumpfun", False):
            tasks.append(self.pumpfun_recent())

        merged: dict[str, Launch] = {}
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, BaseException):
                log.debug("источник упал: %s", res)
                continue
            for launch in res:
                cur = merged.get(launch.mint)
                if cur:
                    cur.merge(launch)
                else:
                    merged[launch.mint] = launch

        if not merged and src.get("dexscreener", True):
            log.info("Jupiter/pump.fun пусты — иду в DexScreener")
            for launch in await self.dexscreener_new():
                merged.setdefault(launch.mint, launch)
        return list(merged.values())

    async def enrich(self, launches: list[Launch]) -> None:
        """Догрузить пул/соцсети (DexScreener) и риск (RugCheck) для шорт-листа."""
        if not launches:
            return
        if self.conf.get("enrich_dexscreener", True):
            try:
                pairs = await self.dexscreener_pairs([l.mint for l in launches])
                by_mint = {p.mint: p for p in pairs}
                for launch in launches:
                    extra = by_mint.get(launch.mint)
                    if extra:
                        launch.merge(extra)
            except Exception as e:  # noqa: BLE001
                log.debug("обогащение DexScreener: %s", e)
        if self.conf.get("rugcheck", True):
            await asyncio.gather(*(self.rugcheck(l) for l in launches),
                                 return_exceptions=True)


# ════════════════════════════════════════════════════════════════════════════
#  ФИЛЬТРЫ
# ════════════════════════════════════════════════════════════════════════════

def fresh_passes(l: Launch, conf: dict[str, Any]) -> tuple[bool, str]:
    """Жёсткий отсев: не прошёл — монету даже не считаем."""
    age = l.age_minutes
    if l.created_ts and age < num(conf.get("min_age_minutes"), 2):
        return False, "слишком свежая, данных ещё нет"
    max_age = num(conf.get("max_age_minutes"), 240)
    if max_age and l.created_ts and age > max_age:
        return False, "уже не новая"
    if not l.created_ts:
        return False, "нет времени создания"

    if l.liquidity and l.liquidity < num(conf.get("min_liquidity_usd"), 8000):
        return False, "мало ликвидности"
    if l.mcap and l.mcap < num(conf.get("min_mcap_usd"), 15000):
        return False, "капитализация ниже порога"
    max_mcap = num(conf.get("max_mcap_usd"))
    if max_mcap and l.mcap > max_mcap:
        return False, "капитализация выше порога"

    if l.holders and l.holders < int(num(conf.get("min_holders"), 50)):
        return False, "мало холдеров"
    if l.buys_5m < int(num(conf.get("min_buys_5m"), 20)):
        return False, "мало покупок за 5м"
    if l.vol_5m and l.vol_5m < num(conf.get("min_volume_5m_usd"), 4000):
        return False, "мало объёма за 5м"
    min_vol = num(conf.get("min_volume_usd"))
    if min_vol and l.vol_total and l.vol_total < min_vol:
        return False, "мало общего объёма"
    min_fees = num(conf.get("min_fees_sol"))
    if min_fees and l.fees_sol and l.fees_sol < min_fees:
        return False, f"комиссий всего ~{l.fees_sol:.2f} SOL"

    if l.dev_pct > num(conf.get("max_dev_pct"), 6):
        return False, f"дев держит {l.dev_pct:.1f}%"
    if l.top10_pct > num(conf.get("max_top10_pct"), 30):
        return False, f"топ-10 держат {l.top10_pct:.0f}%"
    if l.dev_migrations > int(num(conf.get("max_dev_migrations"), 8)):
        return False, f"дев выпустил уже {l.dev_migrations} монет"

    if conf.get("require_mint_revoked", True) and l.mint_revoked is False:
        return False, "mint authority не отозван"
    if conf.get("require_freeze_revoked", True) and l.freeze_revoked is False:
        return False, "freeze authority не отозван"
    if l.honeypot or l.rugpull_flag:
        return False, "помечен как honeypot/rugpull"

    if not launchpad_matches(l.launchpad, conf.get("launchpads") or []):
        return False, f"протокол {l.launchpad} не выбран"

    quotes = [str(q).upper() for q in (conf.get("quote_tokens") or [])]
    if quotes and l.quote_symbol and l.quote_symbol.upper() not in quotes:
        return False, f"квота {l.quote_symbol} не выбрана"

    text = f"{l.symbol} {l.name}".lower()
    for word in (conf.get("blacklist_words") or []):
        if str(word).lower() in text:
            return False, f"стоп-слово «{word}»"
    return True, "ok"


# ════════════════════════════════════════════════════════════════════════════
#  СКОРИНГ — та же логика, что смотрят глазами в терминале
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    name: str
    points: float
    max_points: float
    note: str = ""


@dataclass
class FreshAnalysis:
    launch: Launch
    score: float
    signals: list[Signal] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    multiplier: float = 1.0
    verdict: str = ""
    decision: str = "watch"          # enter | watch | skip
    news_titles: list[tuple[str, str]] = field(default_factory=list)   # (заголовок, ссылка)
    narratives: list[str] = field(default_factory=list)
    news_bonus: float = 0.0
    llm: dict | None = None
    smart_hits: int = 0                    # сколько отслеживаемых кошельков зашло
    smart_note: str = ""

    @property
    def mint(self) -> str:
        return self.launch.mint

    @property
    def decision_label(self) -> str:
        return {"enter": "🟢 НОРМ", "watch": "🟡 НАБЛЮДАТЬ",
                "skip": "🔴 МИМО"}.get(self.decision, "🟡 НАБЛЮДАТЬ")


def _sig_holders(l: Launch) -> Signal:
    pts = piecewise(l.holders, [(0, 0), (30, 2), (60, 5), (150, 8.5), (400, 11), (1200, 12)])
    return Signal("Холдеры", pts, 12, f"{l.holders}")


def _sig_holder_flow(l: Launch) -> Signal:
    flow = l.holders_change_5m
    if not flow and l.traders_5m:
        flow = l.traders_5m * 0.4                    # грубая оценка, если поля нет
    pts = piecewise(flow, [(0, 0), (5, 1.5), (20, 4), (60, 6.5), (150, 8)])
    if flow < 0:
        pts = 0.0
    return Signal("Приток холдеров 5м", pts, 8, f"{flow:+.0f}")


def _sig_buy_pressure(l: Launch) -> Signal:
    total = l.buys_5m + l.sells_5m
    if not total:
        return Signal("Давление покупок 5м", 0, 12, "нет сделок")
    ratio = l.buy_ratio_5m
    pts = piecewise(ratio, [(0.35, 0), (0.5, 4), (0.6, 7.5), (0.7, 10), (0.85, 12)])
    if ratio > 0.95 and total > 40:
        pts *= 0.7                                   # 100% покупок — обычно боты
    depth = piecewise(total, [(0, 0.4), (50, 0.8), (150, 1.0)])
    return Signal("Давление покупок 5м", pts * depth, 12,
                  f"{l.buys_5m}/{l.sells_5m} · {ratio*100:.0f}% buy")


def _sig_volume(l: Launch) -> Signal:
    if l.liquidity <= 0:
        pts = piecewise(l.vol_5m, [(0, 0), (5000, 3), (25000, 6), (100000, 8)])
        return Signal("Объём 5м", pts, 10, fmt_usd(l.vol_5m))
    ratio = l.vol_5m / l.liquidity
    pts = piecewise(ratio, [(0, 0), (0.15, 3), (0.5, 6), (1.5, 9), (4, 10)])
    if ratio > 12:
        pts *= 0.6                                   # похоже на прокрутку объёма
    return Signal("Объём 5м к ликвидности", pts, 10,
                  f"{fmt_usd(l.vol_5m)} · {ratio:.2f}x")


def _sig_liquidity(l: Launch) -> Signal:
    pts = piecewise(l.liquidity, [(0, 0), (8000, 3), (20000, 6), (60000, 9), (200000, 10)])
    return Signal("Ликвидность", pts, 10, fmt_usd(l.liquidity))


def _sig_momentum(l: Launch) -> Signal:
    m5, h1 = l.price_change_5m, l.price_change_1h
    pts = piecewise(m5, [(-30, 0), (0, 2), (15, 4.5), (60, 6)]) * 0.6 \
        + piecewise(h1, [(-40, 0), (0, 2), (50, 4.5), (300, 6)]) * 0.4
    if m5 > 400:
        pts *= 0.65                                  # вертикальная свеча = поздний вход
    return Signal("Моментум цены", min(pts, 8), 8, f"{m5:+.0f}% 5м · {h1:+.0f}% 1ч")


def _sig_organic(l: Launch) -> Signal:
    label = (l.organic_label or "").lower()
    base = {"high": 8.0, "medium": 5.0, "low": 2.0}.get(label)
    if base is None:
        base = piecewise(l.organic_score, [(0, 0), (20, 2), (50, 5), (80, 8)])
    share = 0.0
    if l.buys_5m and l.organic_buyers_5m:
        share = min(1.0, l.organic_buyers_5m / max(l.buys_5m, 1))
    pts = min(10.0, base + share * 2)
    note = label or f"{l.organic_score:.0f}"
    if l.organic_buyers_5m:
        note += f" · органик-покупателей {l.organic_buyers_5m}"
    return Signal("Органика (Jupiter)", pts, 10, note)


def _sig_safety(l: Launch) -> Signal:
    pts, notes = 0.0, []
    if l.mint_revoked:
        pts += 3.5
        notes.append("mint ✅")
    elif l.mint_revoked is False:
        notes.append("mint ❌")
    if l.freeze_revoked:
        pts += 3.5
        notes.append("freeze ✅")
    elif l.freeze_revoked is False:
        notes.append("freeze ❌")
    if l.locked_ratio is not None:
        pts += piecewise(l.locked_ratio, [(0, 0), (0.5, 1.5), (0.9, 3)])
        notes.append(f"LP заперт {l.locked_ratio*100:.0f}%")
    elif l.graduated:
        pts += 1.5                                   # пул мигрировал — LP обычно сожжён
    if l.rug_score is not None:
        pts += piecewise(l.rug_score, [(0, 2), (20, 1.2), (40, 0.4), (60, 0)])
        notes.append(f"RugCheck {l.rug_score:.0f}/100")
    return Signal("Безопасность контракта", min(pts, 12), 12, " · ".join(notes) or "нет данных")


def _sig_distribution(l: Launch) -> Signal:
    pts = 0.0
    pts += piecewise(l.dev_pct, [(0, 5), (1, 4.5), (3, 3), (6, 1), (10, 0)])
    pts += piecewise(l.top10_pct, [(0, 5), (12, 4.5), (20, 3), (30, 1), (45, 0)])
    pts += piecewise(l.dev_migrations, [(0, 2), (2, 1.5), (5, 0.5), (10, 0)])
    note = f"дев {l.dev_pct:.1f}% · топ-10 {l.top10_pct:.0f}%"
    if l.dev_migrations:
        note += f" · монет у дева: {l.dev_migrations}"
    return Signal("Распределение supply", min(pts, 12), 12, note)


def _sig_stage(l: Launch) -> Signal:
    pts, notes = 0.0, []
    if l.graduated:
        pts += 3
        notes.append("мигрировал на DEX")
    elif l.bonding_curve is not None:
        pts += piecewise(l.bonding_curve, [(0, 0), (30, 1), (60, 2), (85, 3)])
        notes.append(f"кривая {l.bonding_curve:.0f}%")
    socials = sum(bool(x) for x in (l.twitter, l.telegram, l.website))
    pts += piecewise(socials, [(0, 0), (1, 1), (2, 2), (3, 2.5)])
    if socials:
        notes.append(f"соцсетей: {socials}")
    if l.launchpad.lower() in KNOWN_LAUNCHPADS:
        pts += 0.5
    return Signal("Этап и соцсети", min(pts, 6), 6, " · ".join(notes) or "—")


def red_flags(l: Launch) -> tuple[list[str], float]:
    """Красные флаги и множитель к скору."""
    flags: list[str] = []
    mult = 1.0

    if l.honeypot:
        flags.append("☠️ помечен как honeypot — продать не дадут")
        mult *= 0.15
    if l.rugpull_flag:
        flags.append("☠️ Blockaid: признаки rug pull")
        mult *= 0.25
    if l.mint_revoked is False:
        flags.append("🔴 mint authority активен — можно допечатать supply")
        mult *= 0.5
    if l.freeze_revoked is False:
        flags.append("🔴 freeze authority активен — кошелёк могут заморозить")
        mult *= 0.5
    if l.dev_pct >= 10:
        flags.append(f"🔴 дев держит {l.dev_pct:.1f}% supply")
        mult *= 0.55
    elif l.dev_pct >= 5:
        flags.append(f"🟡 дев держит {l.dev_pct:.1f}% supply")
        mult *= 0.85
    if l.top10_pct >= 40:
        flags.append(f"🔴 топ-10 кошельков держат {l.top10_pct:.0f}%")
        mult *= 0.6
    elif l.top10_pct >= 25:
        flags.append(f"🟡 топ-10 кошельков держат {l.top10_pct:.0f}%")
        mult *= 0.88
    if l.dev_migrations >= 5:
        flags.append(f"🟡 дев уже выпускал {l.dev_migrations} монет — серийный запускатор")
        mult *= 0.8
    if l.locked_ratio is not None and l.locked_ratio < 0.5 and l.graduated:
        flags.append(f"🔴 LP заперт лишь на {l.locked_ratio*100:.0f}%")
        mult *= 0.6
    if l.rug_danger:
        mult *= 0.55
    if l.rug_score is not None and l.rug_score >= 60:
        flags.append(f"🔴 RugCheck risk {l.rug_score:.0f}/100")
        mult *= 0.7
    for f in l.rug_flags[:4]:
        if f not in flags:
            flags.append(f)

    total_5m = l.buys_5m + l.sells_5m
    if total_5m >= 30 and l.buy_ratio_5m < 0.4 and l.price_change_5m < -10:
        flags.append("🔴 продавцов больше покупателей, цена валится")
        mult *= 0.7
    if l.holders and l.holders < 30:
        flags.append(f"🟡 всего {l.holders} холдеров")
        mult *= 0.85
    if l.holders and l.top10_pct and l.holders < 100 and l.top10_pct > 20:
        flags.append("🟡 мало холдеров при высокой концентрации — похоже на бандл")
        mult *= 0.85
    if l.liquidity and l.mcap and l.liquidity / max(l.mcap, 1) < 0.02:
        flags.append("🟡 ликвидность меньше 2% от капитализации — выйти будет дорого")
        mult *= 0.85
    if l.age_minutes < 5 and l.mcap > 500000:
        flags.append("🟡 капитализация взлетела за минуты — вход уже поздний")
        mult *= 0.9

    return flags, max(mult, 0.05)


def verdict_text(score: float, flags: list[str]) -> str:
    if any(f.startswith("☠️") for f in flags):
        return "☠️ Скам — руками не трогать"
    if score >= 82:
        return "🔥 Сильный старт: органика и распределение в порядке"
    if score >= 70:
        return "🚀 Интересно, но смотри стакан руками"
    if score >= 60:
        return "⚡ Средне: есть за что зацепиться, риск высокий"
    if score >= 45:
        return "👀 Слабовато — только наблюдать"
    return "🚫 Мусор — мимо"


def news_bonus(l: Launch, news: Any, conf: dict[str, Any]) -> tuple[float, list[str], list[tuple[str, str]]]:
    """Плюс к скору за нарратив: тикер/название мелькает в свежих новостях.

    `news` — движок новостей из memebot (NewsEngine) либо любой объект с
    методом .score(symbol, name). Нет движка — нет бонуса.
    """
    if not news or not (conf.get("news") or {}).get("enabled", True):
        return 0.0, [], []
    try:
        match = news.score(l.symbol, l.name)
    except Exception as e:  # noqa: BLE001
        log.debug("новости: %s", e)
        return 0.0, [], []
    cap = num((conf.get("news") or {}).get("bonus_max"), 8)
    pts = min(cap, num(getattr(match, "points", 0)))
    titles = [(str(getattr(i, "title", ""))[:120], str(getattr(i, "link", "")))
              for i in (getattr(match, "headlines", []) or [])[:3]]
    return pts, list(getattr(match, "narratives", []) or []), titles


# Флаги, при которых вход невозможен в принципе: у монеты сломан сам контракт.
# Остальные красные флаги (доля дева, концентрация, RugCheck) уже срезают скор
# множителем — блокировать вход ещё и ими значило бы штрафовать дважды,
# и тогда свежие лончи не проходят вообще никогда.
FATAL = ("mint authority активен", "freeze authority активен",
         "honeypot", "rug pull", "продать не дадут")


def is_fatal(flag: str) -> bool:
    low = flag.lower()
    return any(f in low for f in FATAL)


def decide(score: float, flags: list[str], llm: dict | None,
           conf: dict[str, Any], smart_hits: int = 0) -> str:
    """Автопилот: enter (норм) / watch (наблюдать) / skip (мимо)."""
    auto = conf.get("auto") or {}
    enter_at = num(auto.get("enter_score"), 70)
    watch_at = num(auto.get("watch_score"), 55)

    if any(f.startswith("☠️") for f in flags):
        return "skip"

    # Совпало несколько умных кошельков — заходим, даже если метрики не дотянули.
    # Сломанный контракт всё равно блокирует: там минус гарантирован.
    wconf = conf.get("wallets") or {}
    if (wconf.get("force_enter", True)
            and smart_hits >= int(num(wconf.get("min_hits"), 2))
            and not any(is_fatal(f) for f in flags)):
        return "enter"
    if llm:
        risk = num(llm.get("risk"))
        veto = num((conf.get("llm") or {}).get("veto_risk"), 9)
        if veto and risk >= veto:
            return "skip"
        if str(llm.get("decision", "")).lower().startswith(("мимо", "skip", "нет")):
            return "skip" if score < enter_at else "watch"
    if score < watch_at:
        return "skip"
    if score < enter_at:
        return "watch"
    if auto.get("block_on_red", True) and any(is_fatal(f) for f in flags):
        return "watch"
    return "enter"


def analyze_launch(l: Launch, news: Any = None, llm: dict | None = None,
                   conf: dict[str, Any] | None = None, smart: Any = None) -> FreshAnalysis:
    conf = conf or DEFAULTS
    signals = [
        _sig_holders(l), _sig_holder_flow(l), _sig_buy_pressure(l), _sig_volume(l),
        _sig_liquidity(l), _sig_momentum(l), _sig_organic(l), _sig_safety(l),
        _sig_distribution(l), _sig_stage(l),
    ]
    raw = sum(s.points for s in signals)
    flags, mult = red_flags(l)

    bonus, narratives, titles = news_bonus(l, news, conf)
    if bonus:
        signals.append(Signal("Нарратив из новостей", bonus,
                              num((conf.get("news") or {}).get("bonus_max"), 8),
                              ", ".join(narratives[:4])))

    # Кошельки, которые стабильно в плюсе, — сигнал сильнее любой метрики:
    # это не догадка о монете, а факт, что в неё зашли те, кто умеет.
    smart_hits, smart_note, smart_bonus = 0, "", 0.0
    if smart is not None and getattr(smart, "hits", 0):
        wconf = (conf.get("wallets") or {})
        smart_hits, smart_note = smart.hits, smart.note
        smart_bonus = min(num(wconf.get("max_bonus"), 22),
                          smart_hits * num(wconf.get("bonus_per_hit"), 9))
        signals.append(Signal("Умные кошельки", smart_bonus,
                              num(wconf.get("max_bonus"), 22), smart_note))

    score = max(0.0, min(100.0, raw * mult + bonus + smart_bonus))
    a = FreshAnalysis(launch=l, score=score, signals=signals, flags=flags,
                      multiplier=mult, verdict=verdict_text(score, flags),
                      news_titles=titles, narratives=narratives,
                      news_bonus=bonus, llm=llm,
                      smart_hits=smart_hits, smart_note=smart_note)
    a.decision = decide(score, flags, llm, conf, smart_hits)
    if a.decision == "skip" and not a.verdict.startswith("☠️"):
        a.verdict = verdict_text(score, flags)
    return a


# ════════════════════════════════════════════════════════════════════════════
#  ФИНАЛЬНОЕ СЛОВО НЕЙРОСЕТИ
# ════════════════════════════════════════════════════════════════════════════

LLM_SYSTEM = (
    "Ты — трейдер по свежим мем-коинам Solana, смотришь монету в терминале Axiom/FOMO. "
    "По ончейн-данным и заголовкам новостей реши, стоит ли она входа прямо сейчас. "
    "Отвечай ТОЛЬКО JSON без markdown, строго по схеме: "
    '{"decision": "норм|наблюдать|мимо", "hype": 0-10, "risk": 0-10, '
    '"reason": "одно предложение по-русски", "reasons": ["до трёх коротких пунктов"]}. '
    "hype — способность нарратива собрать толпу в ближайший час. "
    "risk — вероятность скама, бандла или слива дева. "
    "Не занижай оценку из общей осторожности: «мимо» ставь, когда видишь конкретный признак скама или слива, а не просто потому, что монета новая. "
    "Если данные обычные для свежего лонча — это «наблюдать» или «норм»."
)


async def llm_verdict(session: aiohttp.ClientSession, api_key: str, model: str,
                      a: FreshAnalysis) -> dict | None:
    """Прогоняет монету через Claude и возвращает вердикт словарём."""
    if not api_key:
        return None
    l = a.launch
    ctx = [
        f"Тикер: {l.symbol} | Название: {l.name} | Лончпад: {l.launchpad or '—'}",
        f"Возраст: {fmt_age(l.age_minutes)} | Этап: "
        + ("мигрировал на DEX" if l.graduated
           else (f"кривая {l.bonding_curve:.0f}%" if l.bonding_curve is not None else "на кривой")),
        f"MC {fmt_usd(l.mcap)} | Ликвидность {fmt_usd(l.liquidity)} | Цена {fmt_price(l.price_usd)}",
        f"Холдеров {l.holders} (за 5м {l.holders_change_5m:+.0f})",
        f"5м: покупок {l.buys_5m}, продаж {l.sells_5m}, объём {fmt_usd(l.vol_5m)}, "
        f"цена {l.price_change_5m:+.0f}%",
        f"1ч: покупок {l.buys_1h}, продаж {l.sells_1h}, объём {fmt_usd(l.vol_1h)}, "
        f"цена {l.price_change_1h:+.0f}%",
        f"Дев держит {l.dev_pct:.1f}%, топ-10 {l.top10_pct:.0f}%, "
        f"монет у дева ранее: {l.dev_migrations}",
        f"mint отозван: {l.mint_revoked}, freeze отозван: {l.freeze_revoked}, "
        f"RugCheck: {l.rug_score if l.rug_score is not None else '—'}",
        f"Соцсети: X {'есть' if l.twitter else 'нет'}, TG {'есть' if l.telegram else 'нет'}, "
        f"сайт {'есть' if l.website else 'нет'}",
        f"Скор бота: {a.score:.0f}/100",
    ]
    if a.narratives:
        ctx.append("Нарративы: " + ", ".join(a.narratives[:5]))
    if a.news_titles:
        ctx.append("Свежие заголовки:")
        ctx += [f"- {t}" for t, _ in a.news_titles[:5]]
    if a.flags:
        ctx.append("Красные флаги бота:")
        ctx += [f"- {f}" for f in a.flags[:6]]

    payload = {"model": model, "max_tokens": 400, "system": LLM_SYSTEM,
               "messages": [{"role": "user", "content": "\n".join(ctx)}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        async with session.post("https://api.anthropic.com/v1/messages", json=payload,
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=45)) as r:
            if r.status != 200:
                log.warning("LLM %s: %s", r.status, (await r.text())[:200])
                return None
            data = await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM: %s", e)
        return None

    text = "".join(blk.get("text", "") for blk in (data.get("content") or [])
                   if isinstance(blk, dict) and blk.get("type") == "text").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    for candidate in (text, (re.search(r"\{.*\}", text, re.S) or [None])[0] if "{" in text else None):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


# ════════════════════════════════════════════════════════════════════════════
#  СООБЩЕНИЯ
# ════════════════════════════════════════════════════════════════════════════

def score_bar(score: float) -> str:
    filled = int(round(max(0.0, min(100.0, score)) / 10))
    return "█" * filled + "░" * (10 - filled)


def head_emoji(score: float) -> str:
    return "🔥🔥" if score >= 85 else "🚀" if score >= 75 else "⚡" if score >= 62 else "👀"


def terminal_links(l: Launch, conf: dict[str, Any]) -> str:
    tpl = conf.get("terminals") or DEFAULTS["terminals"]
    shown = conf.get("terminals_shown") or list(tpl)
    out = []
    for name in shown:
        url = tpl.get(name)
        if not url:
            continue
        if "{pool}" in url and not l.pool:
            continue
        out.append(f"<a href=\"{esc(url.format(mint=l.mint, pool=l.pool))}\">{esc(name)}</a>")
    return " · ".join(out)


def info_links(l: Launch) -> str:
    out = [f"<a href=\"https://dexscreener.com/solana/{esc(l.pool or l.mint)}\">DexScreener</a>",
           f"<a href=\"https://rugcheck.xyz/tokens/{esc(l.mint)}\">RugCheck</a>",
           f"<a href=\"https://solscan.io/token/{esc(l.mint)}\">Solscan</a>"]
    if l.launchpad.lower() in ("pump.fun", "pumpfun"):
        out.append(f"<a href=\"https://pump.fun/coin/{esc(l.mint)}\">pump.fun</a>")
    if l.twitter:
        out.append(f"<a href=\"{esc(l.twitter)}\">X</a>")
    if l.telegram:
        out.append(f"<a href=\"{esc(l.telegram)}\">TG</a>")
    if l.website:
        out.append(f"<a href=\"{esc(l.website)}\">Сайт</a>")
    return " · ".join(out)


def _safety_line(l: Launch) -> str:
    def mark(v: bool | None) -> str:
        return "✅" if v else ("❌" if v is False else "❔")
    parts = [f"mint {mark(l.mint_revoked)}", f"freeze {mark(l.freeze_revoked)}"]
    if l.locked_ratio is not None:
        parts.append(f"LP заперт {l.locked_ratio*100:.0f}%")
    elif l.graduated:
        parts.append("LP: пул мигрировал")
    if l.rug_score is not None:
        parts.append(f"RugCheck {l.rug_score:.0f}/100")
    return "🛡 " + " · ".join(parts)


def fresh_message(a: FreshAnalysis, conf: dict[str, Any], full: bool = True) -> str:
    l = a.launch
    pad = KNOWN_LAUNCHPADS.get(l.launchpad.lower(), "🆕")
    stage = "мигрировал" if l.graduated else (
        f"кривая {l.bonding_curve:.0f}%" if l.bonding_curve is not None else "на кривой")

    lines = [
        f"{head_emoji(a.score)} <b>{esc(l.title)}</b> — <b>{a.score:.0f}/100</b>  {score_bar(a.score)}",
        f"<b>Вердикт: {a.decision_label}</b>",
        f"<i>{esc(l.name)}</i> · {pad} {esc(l.launchpad or 'solana')} · "
        f"⏱ {fmt_age(l.age_minutes)} от старта · {esc(stage)}",
        "",
        f"💰 MC {fmt_usd(l.mcap)} · 💧 Ликв {fmt_usd(l.liquidity)} · 💵 {fmt_price(l.price_usd)}",
        f"📈 {l.price_change_5m:+.0f}% 5м · {l.price_change_1h:+.0f}% 1ч",
        f"👥 Холдеров {l.holders}" + (f" ({l.holders_change_5m:+.0f} за 5м)"
                                      if l.holders_change_5m else ""),
        f"🔄 5м: {l.buys_5m} покупок / {l.sells_5m} продаж · "
        f"{l.buy_ratio_5m*100:.0f}% buy · объём {fmt_usd(l.vol_5m)}",
        f"💸 Оборот {fmt_usd(l.vol_total)}"
        + (f" · комиссий ~{l.fees_sol:.2f} SOL" if l.fees_sol else "")
        + (f" · пара к {esc(l.quote_symbol)}" if l.quote_symbol else ""),
        f"🧪 Дев {l.dev_pct:.1f}% · Топ-10 {l.top10_pct:.0f}%"
        + (f" · монет у дева: {l.dev_migrations}" if l.dev_migrations else ""),
        _safety_line(l),
        "",
        f"🎯 {esc(a.verdict)}",
    ]

    if a.narratives or a.news_titles:
        lines.append("")
        if a.narratives:
            lines.append("🏷 Нарратив: " + esc(", ".join(a.narratives[:4])))
        for title, link in a.news_titles[:3]:
            lines.append(f"📰 <a href=\"{esc(link)}\">{esc(title)}</a>" if link
                         else f"📰 {esc(title)}")

    if a.llm:
        reason = str(a.llm.get("reason") or a.llm.get("verdict") or "")[:220]
        lines += ["", f"🧠 <b>Нейросеть:</b> {esc(str(a.llm.get('decision','—')))} · "
                      f"хайп {num(a.llm.get('hype')):.0f}/10 · риск {num(a.llm.get('risk')):.0f}/10"]
        if reason:
            lines.append(f"   {esc(reason)}")
        for r in (a.llm.get("reasons") or [])[:3]:
            lines.append(f"   • {esc(str(r)[:120])}")

    if full:
        lines += ["", "<b>Сигналы:</b>"]
        for s in sorted(a.signals, key=lambda x: -x.points):
            lines.append(f"  • {esc(s.name)}: {s.points:.1f}/{s.max_points:.0f}"
                         + (f" — {esc(s.note)}" if s.note else ""))
        if a.flags:
            lines += ["", "<b>⚠️ Красные флаги:</b>"]
            lines += [f"  • {esc(f)}" for f in a.flags[:8]]
            if a.multiplier < 1:
                lines.append(f"  <i>(скор снижен ×{a.multiplier:.2f})</i>")

    lines += ["", "🖥 Терминалы: " + terminal_links(l, conf),
              "🔗 " + info_links(l),
              f"<code>{esc(l.mint)}</code>"]
    if full:
        lines += ["", "<i>Не финансовый совет. Свежие мем-коины — это лотерея, "
                      "заходи только тем, что не жалко потерять.</i>"]
    return "\n".join(lines)


def fresh_list_message(items: list[FreshAnalysis], conf: dict[str, Any],
                       title: str = "Свежие лончи") -> str:
    if not items:
        return "Сейчас ничего живого среди новых монет нет."
    out = [f"🆕 <b>{esc(title)}</b>"]
    for i, a in enumerate(items, 1):
        l = a.launch
        out.append(
            f"\n{i}. {head_emoji(a.score)} <b>{esc(l.title)}</b> — {a.score:.0f}/100 · "
            f"{a.decision_label} · {fmt_age(l.age_minutes)}\n"
            f"   MC {fmt_usd(l.mcap)} · Ликв {fmt_usd(l.liquidity)} · 👥 {l.holders} · "
            f"{l.buy_ratio_5m*100:.0f}% buy\n"
            f"   {terminal_links(l, conf)}\n"
            f"   <code>{esc(l.mint)}</code>")
    return "\n".join(out)


# ════════════════════════════════════════════════════════════════════════════
#  ХРАНИЛИЩЕ (дедуп алертов)
# ════════════════════════════════════════════════════════════════════════════

FRESH_SCHEMA = """
CREATE TABLE IF NOT EXISTS fresh_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts REAL NOT NULL,
    symbol TEXT, launchpad TEXT, score REAL, price_usd REAL, mcap REAL,
    liquidity REAL, holders INTEGER, age_minutes REAL);
CREATE INDEX IF NOT EXISTS idx_fresh ON fresh_alerts(mint, ts);
"""


class FreshStore:
    """Работает поверх Storage из memebot.py (общая база) либо самостоятельно."""

    def __init__(self, storage: Any = None, path: str | Path = "data/memebot.db"):
        if storage is not None and hasattr(storage, "conn"):
            self.conn = storage.conn
            self.lock = getattr(storage, "lock", threading.Lock())
        else:
            p = Path(path)
            if not p.is_absolute():
                p = ROOT / p
            p.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(p), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(FRESH_SCHEMA)
            self.conn.commit()

    def last(self, mint: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM fresh_alerts WHERE mint=? ORDER BY ts DESC LIMIT 1",
                (mint,)).fetchone()
        return dict(row) if row else None

    def should_alert(self, mint: str, score: float, cooldown_min: float,
                     delta: float) -> bool:
        last = self.last(mint)
        if not last:
            return True
        if time.time() - num(last.get("ts")) < cooldown_min * 60:
            return False
        return score >= num(last.get("score")) + delta

    def record(self, a: FreshAnalysis) -> None:
        l = a.launch
        with self.lock:
            self.conn.execute(
                "INSERT INTO fresh_alerts (mint, ts, symbol, launchpad, score, price_usd,"
                " mcap, liquidity, holders, age_minutes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (l.mint, time.time(), l.symbol, l.launchpad, a.score, l.price_usd,
                 l.mcap, l.liquidity, l.holders, l.age_minutes))
            self.conn.commit()

    def recent(self, hours: float = 24) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM fresh_alerts WHERE ts>=? ORDER BY score DESC",
                (time.time() - hours * 3600,)).fetchall()
        return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
#  СКАНЕР
# ════════════════════════════════════════════════════════════════════════════

SendFn = Callable[[str], Awaitable[Any]]


class FreshScanner:
    """Опрашивает ленту новых монет, считает скор и шлёт алерты."""

    def __init__(self, session: aiohttp.ClientSession, storage: Any = None,
                 send: SendFn | None = None, conf: dict[str, Any] | None = None,
                 news: Any = None, preset: str = "",
                 on_alert: Callable[["FreshAnalysis"], Awaitable[Any]] | None = None,
                 wallets: Any = None, scout: Any = None):
        base = preset_conf(preset) if preset else dict(DEFAULTS)
        self.conf = merge_conf(base, conf or {})
        self.session = session
        self.news = news
        self.wallets = wallets          # слежка за кошельками, если подключена
        self.scout = scout              # разведчик: сам ищет такие кошельки
        self.feed = LaunchFeed(session, self.conf)
        self.store = FreshStore(storage, self.conf.get("storage_path", "data/memebot.db"))
        self.send = send
        self.on_alert = on_alert          # сюда уходят монеты с вердиктом «норм»
        self.threshold = num(self.conf.get("min_score"), 62)
        self.scans = 0
        self.alerts = 0
        self.last_seen = 0
        self.last_passed = 0
        self.last_error = ""
        self.llm_calls = 0
        self.last_batch: list[FreshAnalysis] = []   # что видели в прошлый раз
        self.last_drops: dict[str, int] = {}        # где отсеялись
        self.last_scan_ts = 0.0
        self.stop_event = asyncio.Event()

    def _smart(self, mint: str):
        """Сигнал по кошелькам для монеты, если слежка подключена."""
        if self.wallets is None:
            return None
        try:
            return self.wallets.signal(mint)
        except Exception as e:  # noqa: BLE001
            log.debug("сигнал кошельков: %s", e)
            return None

    @property
    def preset(self) -> str:
        return str(self.conf.get("preset", "default"))

    # Насколько охотно бот заходит. Одна ручка вместо пяти настроек.
    AGGRESSION = {
        "low":  {"min_score": 68, "enter": 72, "watch": 58, "veto": 7, "red": True},
        "mid":  {"min_score": 58, "enter": 58, "watch": 45, "veto": 9, "red": True},
        # фатальные флаги блокируют на любом уровне: mint не отозван или honeypot —
        # это не «рискованно», это гарантированный минус
        "high": {"min_score": 48, "enter": 48, "watch": 38, "veto": 10, "red": True},
    }

    def set_aggression(self, level: str) -> bool:
        """low — редко и придирчиво, mid — по умолчанию, high — заходит почти во всё."""
        p = self.AGGRESSION.get(str(level).strip().lower())
        if not p:
            return False
        self.threshold = float(p["min_score"])
        self.conf["min_score"] = p["min_score"]
        self.conf["auto"] = {**(self.conf.get("auto") or {}),
                             "enter_score": p["enter"], "watch_score": p["watch"],
                             "block_on_red": p["red"]}
        self.conf["llm"] = {**(self.conf.get("llm") or {}), "veto_risk": p["veto"]}
        log.info("Агрессивность: %s (вход от %s, вето нейросети от %s)",
                 level, p["enter"], p["veto"])
        return True

    def aggression_line(self) -> str:
        auto = self.conf.get("auto") or {}
        return (f"Вход от <b>{num(auto.get('enter_score'), 58):.0f}</b>/100 · "
                f"«наблюдать» от {num(auto.get('watch_score'), 45):.0f} · "
                f"вето нейросети при риске "
                f"{num((self.conf.get('llm') or {}).get('veto_risk'), 9):.0f}/10 · "
                f"фатальные флаги "
                f"{'блокируют' if auto.get('block_on_red', True) else 'не блокируют'}")

    def apply_preset(self, name: str) -> bool:
        """Переключить профиль на лету: /preset axiom."""
        key = str(name or "").strip().lower()
        if key not in PRESETS:
            return False
        self.conf = preset_conf(key)
        self.feed.conf = self.conf
        self.threshold = num(self.conf.get("min_score"), 62)
        log.info("Профиль переключён на %s", key.upper())
        return True

    # ---------- нейросеть ----------

    async def _llm_pass(self, analyses: list[FreshAnalysis]) -> None:
        """Прогнать лучших кандидатов через Claude и пересчитать вердикт."""
        conf_llm = self.conf.get("llm") or {}
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not conf_llm.get("enabled", True) or not api_key:
            return
        floor = num(conf_llm.get("min_score"), 58)
        limit = int(num(conf_llm.get("max_per_scan"), 3))
        picked = [a for a in analyses if a.score >= floor][:limit]
        if not picked:
            return
        model = str(conf_llm.get("model", "claude-sonnet-5"))
        results = await asyncio.gather(
            *(llm_verdict(self.session, api_key, model, a) for a in picked),
            return_exceptions=True)
        for a, res in zip(picked, results):
            if isinstance(res, dict):
                a.llm = res
                self.llm_calls += 1
                a.decision = decide(a.score, a.flags, res, self.conf)

    # ---------- один проход ----------

    async def analyze_all(self, limit: int | None = None,
                          llm: bool = False) -> list[FreshAnalysis]:
        """Собрать ленту, отфильтровать и посчитать скор. Без рассылки."""
        launches = await self.feed.collect()
        self.last_seen = len(launches)

        # оценка Global Fees Paid в SOL — нужна цена SOL
        if num(self.conf.get("min_fees_sol")) or launches:
            fee_rate = num(self.conf.get("fee_rate"), 0.01)
            sol = await self.feed.sol_price()
            for l in launches:
                l.estimate_fees_sol(fee_rate, sol)

        candidates: list[Launch] = []
        for l in launches:
            ok, _ = fresh_passes(l, self.conf)
            if ok:
                candidates.append(l)
        self.last_passed = len(candidates)

        # Монеты, которые купили наши кошельки, тянем отдельно: в общую ленту
        # они могут не попасть (слишком свежие или отсеялись), а это как раз
        # самый ценный сигнал — упустить его нельзя.
        if self.wallets is not None:
            known = {l.mint for l in candidates}
            extra = []
            for mint in {b.mint for b in getattr(self.wallets, "buys", [])}:
                if mint in known:
                    continue
                sig = self._smart(mint)
                if not sig or sig.hits < int(num(
                        (self.conf.get("wallets") or {}).get("min_hits"), 2)):
                    continue
                launch = await self.feed.jupiter_token(mint)
                if launch:
                    extra.append(launch)
                    log.info("Монета от кошельков вне ленты: %s (%s)",
                             launch.symbol or mint[:8], sig.note)
            candidates.extend(extra)
            self.last_passed = len(candidates)

        # предварительный скор → тяжёлые проверки только для лучших
        candidates.sort(key=lambda l: analyze_launch(
            l, conf=self.conf, smart=self._smart(l.mint)).score, reverse=True)
        shortlist = candidates[:int(num(self.conf.get("shortlist_limit"), 12))]
        await self.feed.enrich(shortlist)

        if self.scout is not None:
            for l in candidates:
                self.scout.observe(l.mint, l.symbol, l.price_usd)

        out = [analyze_launch(l, news=self.news, conf=self.conf,
                              smart=self._smart(l.mint)) for l in shortlist]
        out.sort(key=lambda a: -a.score)
        if llm:
            await self._llm_pass(out)
            out.sort(key=lambda a: -a.score)
        return out[:limit] if limit else out

    async def poll(self) -> list[FreshAnalysis]:
        """Полный автопроход: собрал → отфильтровал → новости → нейросеть → алерт."""
        analyses = await self.analyze_all(llm=True)
        self.scans += 1

        auto = self.conf.get("auto") or {}
        only_enter = bool(auto.get("enabled", True) and auto.get("only_enter", True))
        cooldown = num(self.conf.get("cooldown_minutes"), 90)
        delta = num(self.conf.get("rescore_delta"), 8)
        max_per_scan = int(num(self.conf.get("max_per_scan"), 4))
        sent: list[FreshAnalysis] = []

        # считаем, на каком шаге отсеялась каждая монета — иначе на вопрос
        # «почему за ночь ноль сделок» можно только гадать
        drops = {"скор ниже порога": 0, "вердикт «мимо»": 0, "только «наблюдать»": 0,
                 "уже брали недавно": 0, "лимит за проход": 0}
        self.last_batch = analyses[:8]

        for a in analyses:
            if a.score < self.threshold:
                drops["скор ниже порога"] += 1
                continue
            if a.decision == "skip":
                drops["вердикт «мимо»"] += 1
                continue
            if only_enter and a.decision != "enter":
                drops["только «наблюдать»"] += 1
                continue
            if not self.store.should_alert(a.mint, a.score, cooldown, delta):
                drops["уже брали недавно"] += 1
                continue
            if len(sent) >= max_per_scan:
                drops["лимит за проход"] += 1
                break
            ok = True
            # notify=False: монета всё равно уходит в работу (в трейдер),
            # просто её разбор не сыплется в чат — там только сделки
            if self.conf.get("notify", False):
                if self.send:
                    try:
                        ok = bool(await self.send(fresh_message(a, self.conf)))
                    except Exception as e:  # noqa: BLE001
                        log.exception("отправка алерта: %s", e)
                        ok = False
                else:
                    print(fresh_message(a, self.conf))
            if ok:
                self.store.record(a)
                self.alerts += 1
                sent.append(a)
                if self.on_alert:
                    try:
                        await self.on_alert(a)
                    except Exception as e:  # noqa: BLE001
                        log.exception("обработчик алерта: %s", e)
                await asyncio.sleep(0.8)

        self.last_drops = {k: v for k, v in drops.items() if v}
        self.last_scan_ts = time.time()
        log.info("[%s] свежие лончи: %d собрано, %d после фильтров, %d в работу%s",
                 self.preset, self.last_seen, self.last_passed, len(sent),
                 (" · отсев: " + ", ".join(f"{k} {v}" for k, v in self.last_drops.items()))
                 if self.last_drops else "")
        return sent

    def why_message(self) -> str:
        """Что бот увидел в последнем проходе и почему не зашёл."""
        if not self.last_scan_ts:
            return "Скана ещё не было — бот только запустился."
        ago = (time.time() - self.last_scan_ts) / 60
        out = [f"🔎 <b>Последний проход</b> ({ago:.0f} мин назад)",
               f"Монет собрано: <b>{self.last_seen}</b>",
               f"Прошло фильтры {self.preset.upper()}: <b>{self.last_passed}</b>",
               f"Порог входа: <b>{self.threshold:.0f}</b>, "
               f"вердикт «норм» от: <b>{num((self.conf.get('auto') or {}).get('enter_score'), 70):.0f}</b>"]

        if self.last_seen == 0:
            out += ["", "⚠️ Ноль монет из источников — значит данные не приходят.",
                    "Проверь интернет; если он есть, источник мог временно закрыться."]
        elif self.last_drops:
            out += ["", "<b>Где отсеялись:</b>"]
            out += [f"  • {k}: {v}" for k, v in self.last_drops.items()]

        if self.last_batch:
            out += ["", "<b>Лучшие кандидаты:</b>"]
            for a in self.last_batch[:5]:
                line = (f"  • ${esc(a.launch.symbol or a.mint[:6])} — {a.score:.0f}/100 · "
                        f"{a.decision_label}")
                if a.llm:
                    line += (f"\n     🧠 {esc(str(a.llm.get('decision', '')))} · "
                             f"риск {num(a.llm.get('risk')):.0f}/10")
                    reason = str(a.llm.get("reason") or "")[:110]
                    if reason:
                        line += f"\n     {esc(reason)}"
                if a.flags:
                    line += f"\n     ⚠️ {esc(a.flags[0])}"
                out.append(line)

        out += ["", "<i>Планку можно опустить: /freshscore 55, или /auto off — "
                    "тогда берём и «наблюдать».</i>"]
        return "\n".join(out)

    async def inspect(self, mint: str) -> FreshAnalysis | None:
        """Разбор конкретной монеты по адресу минта (команда /check)."""
        mint = mint.strip()
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
            return None
        launch = await self.feed.jupiter_token(mint)
        if launch is None:
            pairs = await self.feed.dexscreener_pairs([mint])
            launch = pairs[0] if pairs else None
        if launch is None:
            return None
        await self.feed.enrich([launch])
        a = analyze_launch(launch, news=self.news, conf=self.conf,
                           smart=self._smart(launch.mint))
        await self._llm_pass([a])
        return a

    # ---------- цикл ----------

    async def loop(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or self.stop_event
        interval = num(self.conf.get("interval_seconds"), 45)
        while not stop.is_set():
            try:
                await self.poll()
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:200]
                log.exception("сбой скана свежих лончей: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def status_line(self) -> str:
        llm_on = bool((self.conf.get("llm") or {}).get("enabled", True)
                      and os.environ.get("ANTHROPIC_API_KEY"))
        return (f"Свежие лончи [{self.preset.upper()}]: сканов {self.scans}, "
                f"алертов {self.alerts}, порог {self.threshold:.0f}, "
                f"нейросеть {'вкл' if llm_on else 'выкл'} ({self.llm_calls} разборов), "
                f"в последнем проходе {self.last_seen} монет / {self.last_passed} после фильтров"
                + (f", ошибка: {esc(self.last_error)}" if self.last_error else ""))


FRESH_HELP = (
    "/fresh [N] — топ свежих лончей прямо сейчас\n"
    "/check &lt;mint&gt; — разбор конкретной монеты\n"
    "/preset [axiom|fomo|safe|degen] — профиль автопилота\n"
    "/freshscore [0-100] — порог алерта по свежим\n"
    "/auto [on|off] — слать только вердикт «норм» или всё подряд"
)


# ════════════════════════════════════════════════════════════════════════════
#  АВТОНОМНЫЙ ЗАПУСК
# ════════════════════════════════════════════════════════════════════════════

async def amain(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Telegram = NewsEngine = None                    # type: ignore[assignment]
    try:
        from memebot import Telegram, NewsEngine, load_env   # общий TG и лента новостей
        load_env()
    except Exception as e:  # noqa: BLE001
        log.warning("memebot.py рядом не найден (%s) — работаю автономно", e)

    preset = args.preset or os.environ.get("FRESH_PRESET", "") or "axiom"
    conf = preset_conf(preset)
    if args.min_score is not None:
        conf["min_score"] = args.min_score
    if args.interval:
        conf["interval_seconds"] = args.interval
    if args.max_age:
        conf["max_age_minutes"] = args.max_age
    if args.no_llm:
        conf["llm"] = {**conf["llm"], "enabled": False}
    if args.all:
        conf["auto"] = {**conf["auto"], "only_enter": False}
    log.info("Профиль: %s | порог %.0f | окно до %.0f мин",
             conf.get("preset"), num(conf.get("min_score")), num(conf.get("max_age_minutes")))

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    dry = args.dry or not token

    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=UA, connector=connector) as session:
        send: SendFn | None = None
        if Telegram is not None:
            tg = Telegram(session, token, os.environ.get("TELEGRAM_CHANNEL_ID", ""),
                          os.environ.get("TELEGRAM_CHAT_ID", ""), dry=dry)
            send = tg.broadcast

        news = None
        if NewsEngine is not None and (conf.get("news") or {}).get("enabled", True):
            news = NewsEngine(session)
            try:
                await news.refresh()
            except Exception as e:  # noqa: BLE001
                log.warning("новости не загрузились: %s", e)

        scanner = FreshScanner(session, send=send, conf=conf, news=news)

        if args.once:
            found = await scanner.poll()
            log.info("Готово. Алертов: %d", len(found))
            return

        async def news_loop() -> None:
            if news is None:
                return
            while not scanner.stop_event.is_set():
                try:
                    await asyncio.wait_for(scanner.stop_event.wait(), timeout=600)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    await news.refresh()
                except Exception as e:  # noqa: BLE001
                    log.warning("новости: %s", e)

        await asyncio.gather(scanner.loop(), news_loop())


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Сканер свежих мем-коинов Solana (Axiom / FOMO)")
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    ap.add_argument("--dry", action="store_true", help="печатать в консоль, не слать в TG")
    ap.add_argument("--min-score", type=float, help="порог алерта 0-100")
    ap.add_argument("--interval", type=float, help="период опроса, секунды")
    ap.add_argument("--max-age", type=float, help="максимальный возраст монеты, минуты")
    ap.add_argument("--preset", type=str,
                    help="профиль: axiom | fomo | safe | degen (по умолчанию axiom)")
    ap.add_argument("--no-llm", action="store_true", help="без разбора нейросетью")
    ap.add_argument("--all", action="store_true",
                    help="слать и «наблюдать», не только «норм»")
    ap.add_argument("-v", "--verbose", action="store_true")
    try:
        asyncio.run(amain(ap.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
