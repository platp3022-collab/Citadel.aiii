#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мем-коин сканер: DexScreener + новостной анализ + алерты в Telegram-канал.
Всё в одном файле.

Установка:
    pip install aiohttp
    (опционально: pip install feedparser  — иначе используется встроенный RSS-парсер)

Настройка (переменные окружения или файл .env рядом со скриптом):
    TELEGRAM_BOT_TOKEN=123456:AA...        # от @BotFather
    TELEGRAM_CHANNEL_ID=@my_channel        # КАНАЛ для алертов (бота сделать админом)
    TELEGRAM_CHAT_ID=123456789             # твой личный чат: команды + дубль алертов
    ANTHROPIC_API_KEY=                     # опционально, для LLM-анализа

Запуск:
    python memebot.py            # боевой режим
    python memebot.py --once     # один скан и выход
    python memebot.py --dry      # без отправки в Telegram, всё в консоль

Отказ от ответственности: инструмент фильтрации информации, не финансовый совет.
Подавляющее большинство мем-коинов уходит в ноль.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import aiohttp
except ImportError:
    sys.exit("Нужен aiohttp:  pip install aiohttp")

try:
    import feedparser  # опционально
except ImportError:
    feedparser = None

try:
    import axiom_scout                     # сканер свежих лончей (Axiom / FOMO)
except ImportError:                        # модуль удалили — основной бот всё равно работает
    axiom_scout = None                     # type: ignore[assignment]

try:
    import trader                          # бумажная (позже — реальная) торговля
except ImportError:
    trader = None                          # type: ignore[assignment]

try:
    import dashboard                       # мини-апп со статистикой в браузере
except ImportError:
    dashboard = None                       # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("memebot")

# ════════════════════════════════════════════════════════════════════════════
#  КОНФИГ — правь прямо здесь
# ════════════════════════════════════════════════════════════════════════════

CONFIG: dict[str, Any] = {
    "scan": {
        # Старый сканер по DexScreener с подробным разбором монеты в чат.
        # Выключен: в Telegram нужны только сделки. Разово — командой /scan.
        "enabled": False,
        "interval_seconds": 60,
        "chains": ["solana", "base", "bsc", "ethereum"],
        "max_tokens_per_scan": 120,
        "check_paid_orders": True,
        "drain_lookback_seconds": 900,      # окно для детекта слива ликвидности
        "sources": {
            "token_profiles": True,          # новые токены с оформленным профилем
            "boosts_latest": True,           # свежие бусты
            "boosts_top": True,              # топ по бустам
        },
        "search_queries": [],                # напр. ["WIF", "PEPE"]
    },
    "filters": {
        "min_liquidity_usd": 15000,
        "max_liquidity_usd": 5000000,        # 0 = без верхней границы
        "min_volume_h1_usd": 15000,
        "min_volume_h24_usd": 50000,
        "min_txns_h1": 40,
        "min_age_minutes": 15,
        "max_age_hours": 96,
        "min_fdv_usd": 50000,
        "max_fdv_usd": 30000000,
        "min_liq_to_fdv": 0.02,
        "allowed_quotes": ["SOL", "WETH", "ETH", "USDC", "USDT", "WBNB", "BNB"],
        "blacklist_words": ["test", "rug", "scam"],
    },
    "alerts": {
        "min_score": 65,                     # порог отправки
        "deep_check_margin": 12,             # за сколько до порога включать тяжёлые проверки
        "cooldown_minutes": 180,             # не спамить одной монетой
        "rescore_delta": 10,                 # повтор, только если скор вырос на N
        "channel_short": False,              # True = в канал короткая версия, в личку полная
        "max_per_scan": 5,                   # анти-флуд: не больше N алертов за проход
    },
    "risk": {"enabled": True},               # RugCheck (Solana) + honeypot.is (EVM)
    "news": {
        "enabled": True,
        "refresh_minutes": 10,
        "item_ttl_hours": 24,
        "feeds": [
            "https://cointelegraph.com/rss",
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://decrypt.co/feed",
            "https://cryptoslate.com/feed/",
            "https://bitcoinist.com/feed/",
            "https://news.bitcoin.com/feed/",
            "https://www.theblock.co/rss.xml",
        ],
        # Нарративы: совпало с названием монеты → плюс к скору.
        # Полный вес, если слово реально мелькает в свежих заголовках, иначе 45%.
        "narratives": {
            "ai": 5, "agent": 5, "trump": 6, "elon": 5, "musk": 5, "doge": 4,
            "cat": 3, "dog": 3, "pepe": 3, "wojak": 3, "quantum": 4, "robot": 4,
            "etf": 4, "tariff": 4, "fed": 3, "moon": 2,
        },
    },
    "llm": {"enabled": False, "model": "claude-sonnet-5"},
    # Автопилот по свежим лончам Solana (Axiom / FOMO) — см. axiom_scout.py
    "fresh": {
        "enabled": True,
        "preset": "axiom",                  # axiom | fomo | safe | degen
        "overrides": {},                    # точечно переопределить любой ключ пресета
    },
    # Торговля по находкам сканера — см. trader.py.
    # mode: paper = только считаем сделки, реальных транзакций нет.
    "trade": {
        "enabled": True,
        "mode": "paper",
        "size_sol": 0.1,
        "daily_limit_sol": 1.0,
        "max_positions": 3,
        "take_profit_pct": 60.0,
        "stop_loss_pct": -35.0,
        "timeout_minutes": 30.0,
    },
    # Мини-апп со статистикой: http://localhost:8420 (см. dashboard.py)
    "dashboard": {"enabled": True, "host": "127.0.0.1", "port": 8420},
    "tracking": {"interval_minutes": 15, "window_hours": 48},
    "storage": {"path": "data/memebot.db", "keep_days": 7},
}

BASE_API = "https://api.dexscreener.com"
UA = {"User-Agent": "memecoin-scanner/1.0 (+research bot)"}


def cfg(path: str, default: Any = None) -> Any:
    node: Any = CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    # utf-8-sig: .env, сохранённый блокнотом или PowerShell, приходит с BOM
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ════════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ════════════════════════════════════════════════════════════════════════════

def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    node = obj
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
        if node is None:
            return default
    return node


def age_minutes(pair: dict) -> float:
    created = num(pair.get("pairCreatedAt"))
    if created <= 0:
        return -1.0
    return (time.time() * 1000 - created) / 60000.0


def fmt_usd(v: float) -> str:
    v = num(v)
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:.0f}"


def fmt_price(v: float) -> str:
    v = num(v)
    if v == 0:
        return "-"
    if v >= 1:
        return f"${v:,.4f}"
    if v >= 0.0001:
        return f"${v:.6f}".rstrip("0")
    return f"${v:.10f}".rstrip("0")


def fmt_age(minutes: float) -> str:
    if minutes < 0:
        return "?"
    if minutes < 60:
        return f"{minutes:.0f}м"
    if minutes < 1440:
        return f"{minutes/60:.1f}ч"
    return f"{minutes/1440:.1f}д"


def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


def piecewise(x: float, points: list[tuple[float, float]]) -> float:
    """Кусочно-линейная интерполяция по контрольным точкам."""
    if not points:
        return 0.0
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            if x1 == x0:
                return y1
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return points[-1][1]


def txns(pair: dict, window: str) -> tuple[int, int]:
    node = dig(pair, "txns", window, default={}) or {}
    return int(num(node.get("buys"))), int(num(node.get("sells")))


def socials(pair: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    info = pair.get("info") or {}
    for site in info.get("websites") or []:
        if isinstance(site, dict) and site.get("url"):
            out.setdefault("website", site["url"])
    for soc in info.get("socials") or []:
        if isinstance(soc, dict) and soc.get("url"):
            key = str(soc.get("type") or soc.get("platform") or "link").lower()
            out.setdefault(key, soc["url"])
    if info.get("imageUrl"):
        out["image"] = info["imageUrl"]
    return out


def chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ════════════════════════════════════════════════════════════════════════════
#  DEXSCREENER API
# ════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """Token-bucket: не больше N запросов в минуту."""

    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.tokens = float(per_minute)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self.updated) * self.capacity / 60.0)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) * 60.0 / self.capacity
            await asyncio.sleep(wait)


class DexScreener:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=20)
        self.rl_slow = RateLimiter(55)    # profiles / boosts / orders  (лимит 60/мин)
        self.rl_fast = RateLimiter(280)   # tokens / pairs / search     (лимит 300/мин)

    async def _get(self, path: str, limiter: RateLimiter,
                   params: dict | None = None, retries: int = 3) -> Any:
        url = f"{BASE_API}{path}"
        for attempt in range(retries):
            await limiter.acquire()
            try:
                async with self.session.get(url, params=params, timeout=self.timeout) as r:
                    if r.status == 429:
                        wait = num(r.headers.get("Retry-After"), 5)
                        log.warning("429 DexScreener, пауза %.1fs", wait)
                        await asyncio.sleep(wait)
                        continue
                    if r.status >= 500:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    if r.status != 200:
                        log.warning("HTTP %s %s", r.status, url)
                        return None
                    return await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Сеть %s: %s", url, e)
                await asyncio.sleep(1.5 * (attempt + 1))
        return None

    async def latest_token_profiles(self) -> list[dict]:
        d = await self._get("/token-profiles/latest/v1", self.rl_slow)
        return d if isinstance(d, list) else []

    async def latest_boosts(self) -> list[dict]:
        d = await self._get("/token-boosts/latest/v1", self.rl_slow)
        return d if isinstance(d, list) else []

    async def top_boosts(self) -> list[dict]:
        d = await self._get("/token-boosts/top/v1", self.rl_slow)
        return d if isinstance(d, list) else []

    async def pairs_for_tokens(self, addresses: list[str]) -> list[dict]:
        out: list[dict] = []
        for batch in chunks(addresses, 30):
            d = await self._get("/latest/dex/tokens/" + ",".join(batch), self.rl_fast)
            if isinstance(d, dict) and isinstance(d.get("pairs"), list):
                out.extend(d["pairs"])
        return out

    async def search(self, query: str) -> list[dict]:
        d = await self._get("/latest/dex/search", self.rl_fast, {"q": query})
        if isinstance(d, dict) and isinstance(d.get("pairs"), list):
            return d["pairs"]
        return []

    async def paid_orders(self, chain: str, token: str) -> list[dict]:
        d = await self._get(f"/orders/v1/{chain}/{token}", self.rl_slow)
        return d if isinstance(d, list) else []


# ════════════════════════════════════════════════════════════════════════════
#  НОВОСТИ
# ════════════════════════════════════════════════════════════════════════════

STOP_TICKERS = {
    "THE", "AND", "FOR", "USD", "USDT", "USDC", "ETH", "SOL", "BTC", "NEW", "NOW",
    "ALL", "ONE", "TWO", "TOP", "BUY", "WIN", "GET", "OUT", "CEO", "SEC", "ETF",
    "NFT", "DEX", "CEX", "AI", "GM", "PUMP", "MOON", "COIN", "TOKEN", "APP", "WEB",
}


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    ts: float


@dataclass
class NewsMatch:
    points: float = 0.0
    headlines: list[NewsItem] = field(default_factory=list)
    narratives: list[str] = field(default_factory=list)
    note: str = ""


def parse_feed_fallback(raw: str, source: str) -> list[NewsItem]:
    """Встроенный RSS/Atom парсер — на случай отсутствия feedparser."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw.encode("utf-8", "ignore"))
    except ET.ParseError:
        return []

    def tag(el) -> str:
        return el.tag.split("}")[-1]

    items: list[NewsItem] = []
    for el in root.iter():
        if tag(el) not in ("item", "entry"):
            continue
        title, link, ts = "", "", time.time()
        for child in el:
            name = tag(child)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "link":
                link = (child.get("href") or child.text or "").strip()
            elif name in ("pubDate", "published", "updated"):
                text = (child.text or "").strip()
                try:
                    ts = parsedate_to_datetime(text).timestamp()
                except Exception:  # noqa: BLE001
                    try:
                        from datetime import datetime
                        ts = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
                    except Exception:  # noqa: BLE001
                        pass
        if title:
            items.append(NewsItem(title, link, source, ts))
    return items[:60]


class NewsEngine:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.enabled = bool(cfg("news.enabled", True))
        self.feeds: list[str] = cfg("news.feeds") or []
        self.narratives = {str(k).lower(): float(v)
                           for k, v in (cfg("news.narratives") or {}).items()}
        self.ttl_hours = num(cfg("news.item_ttl_hours"), 24)
        self.refresh_minutes = num(cfg("news.refresh_minutes"), 10)
        self.items: list[NewsItem] = []
        self.hot: dict[str, int] = {}
        self.last_refresh = 0.0

    async def refresh(self) -> None:
        if not self.enabled or not self.feeds:
            return
        results = await asyncio.gather(*(self._fetch(u) for u in self.feeds),
                                       return_exceptions=True)
        fresh: list[NewsItem] = []
        for res in results:
            if isinstance(res, list):
                fresh.extend(res)

        cutoff = time.time() - self.ttl_hours * 3600
        seen, merged = set(), []
        for it in fresh + self.items:
            key = it.title.lower()[:120]
            if key in seen or it.ts < cutoff:
                continue
            seen.add(key)
            merged.append(it)
        merged.sort(key=lambda i: i.ts, reverse=True)
        self.items = merged[:600]
        self.last_refresh = time.time()
        blob = " ".join(i.title.lower() for i in self.items)
        self.hot = {w: blob.count(w) for w in self.narratives if w in blob}
        log.info("Новостей: %d, горячих тем: %d", len(self.items), len(self.hot))

    async def _fetch(self, url: str) -> list[NewsItem]:
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    return []
                raw = await r.text()
        except Exception as e:  # noqa: BLE001
            log.warning("RSS %s: %s", url, e)
            return []

        source = url.split("/")[2].replace("www.", "")
        if feedparser is not None:
            parsed = feedparser.parse(raw)
            if parsed.feed:
                source = parsed.feed.get("title") or source
            out = []
            for e in parsed.entries[:60]:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                ts = time.time()
                for key in ("published_parsed", "updated_parsed"):
                    if e.get(key):
                        try:
                            ts = time.mktime(e[key])
                        except Exception:  # noqa: BLE001
                            pass
                        break
                out.append(NewsItem(title, e.get("link", ""), source, ts))
            return out
        return parse_feed_fallback(raw, source)

    def score(self, symbol: str, name: str) -> NewsMatch:
        m = NewsMatch()
        if not self.enabled:
            return m
        sym = (symbol or "").strip().upper()
        nm = (name or "").strip().lower()

        hits: list[NewsItem] = []
        if len(sym) >= 3 and sym not in STOP_TICKERS:
            pat = re.compile(rf"(?<![A-Za-z0-9$]){re.escape(sym)}(?![A-Za-z0-9])", re.I)
            hits += [i for i in self.items if pat.search(i.title)]
        if len(nm) >= 5:
            hits += [i for i in self.items if nm in i.title.lower() and i not in hits]
        hits = hits[:5]

        if hits:
            fresh = any(time.time() - i.ts < 6 * 3600 for i in hits)
            m.points += min(11.0, 5.0 + 2.0 * len(hits) + (2.0 if fresh else 0.0))
            m.headlines = hits
            m.note = f"{len(hits)} упоминаний в новостях"

        blob = f"{nm} {sym.lower()}"
        for word, weight in self.narratives.items():
            if word in blob:
                m.points += weight * (1.0 if word in self.hot else 0.45)
                m.narratives.append(word)

        m.points = max(0.0, min(15.0, m.points))
        if m.narratives and not m.note:
            m.note = "нарратив: " + ", ".join(m.narratives[:3])
        return m


# ════════════════════════════════════════════════════════════════════════════
#  РИСК-ПРОВЕРКИ КОНТРАКТА
# ════════════════════════════════════════════════════════════════════════════

EVM_CHAIN_IDS = {"ethereum": 1, "bsc": 56, "base": 8453, "arbitrum": 42161,
                 "polygon": 137, "avalanche": 43114, "optimism": 10}


@dataclass
class RiskReport:
    ok: bool = False
    danger: bool = False
    multiplier: float = 1.0
    flags: list[str] = field(default_factory=list)
    source: str = ""


def _merge_reports(reports: list[RiskReport]) -> RiskReport:
    reports = [r for r in reports if r.ok]
    if not reports:
        return RiskReport()
    merged = RiskReport(ok=True, danger=any(r.danger for r in reports),
                        source="+".join(dict.fromkeys(r.source for r in reports if r.source)))
    mult = 1.0
    for r in reports:
        mult *= r.multiplier
        merged.flags.extend(r.flags)
    merged.multiplier = mult
    return merged


async def risk_check(session: aiohttp.ClientSession, chain: str, token: str) -> RiskReport:
    chain = (chain or "").lower()
    try:
        if chain == "solana":
            return await _rugcheck(session, token)
        if chain in EVM_CHAIN_IDS:
            results = await asyncio.gather(_honeypot(session, chain, token),
                                           _goplus(session, chain, token),
                                           return_exceptions=True)
            return _merge_reports([r for r in results if isinstance(r, RiskReport)])
    except Exception as e:  # noqa: BLE001
        log.debug("risk %s/%s: %s", chain, token, e)
    return RiskReport()


async def _rugcheck(session: aiohttp.ClientSession, mint: str) -> RiskReport:
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            return RiskReport()
        data = await r.json(content_type=None)

    rep = RiskReport(ok=True, source="rugcheck")
    for item in (data.get("risks") or [])[:8]:
        level = str(item.get("level", "")).lower()
        name = str(item.get("name", ""))
        if level in ("danger", "high"):
            rep.flags.append(f"🔴 {name}")
            rep.danger = True
        elif level in ("warn", "warning", "medium"):
            rep.flags.append(f"🟡 {name}")

    norm = data.get("score_normalised")
    if isinstance(norm, (int, float)):
        if norm >= 60:
            rep.multiplier, rep.danger = 0.45, True
        elif norm >= 35:
            rep.multiplier = 0.7
        elif norm >= 20:
            rep.multiplier = 0.88
        rep.flags.append(f"RugCheck risk: {norm:.0f}/100")
    elif rep.danger:
        rep.multiplier = 0.55
    return rep


async def _honeypot(session: aiohttp.ClientSession, chain: str, token: str) -> RiskReport:
    params = {"address": token, "chainID": str(EVM_CHAIN_IDS[chain])}
    async with session.get("https://api.honeypot.is/v2/IsHoneypot", params=params,
                           timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            return RiskReport()
        data = await r.json(content_type=None)

    rep = RiskReport(ok=True, source="honeypot.is")
    if (data.get("honeypotResult") or {}).get("isHoneypot"):
        rep.danger, rep.multiplier = True, 0.1
        rep.flags.append("🔴 HONEYPOT — продать нельзя")

    sim = data.get("simulationResult") or {}
    buy_tax, sell_tax = num(sim.get("buyTax")), num(sim.get("sellTax"))
    if sell_tax >= 15 or buy_tax >= 15:
        rep.multiplier = min(rep.multiplier, 0.5)
        rep.flags.append(f"🔴 налоги {buy_tax:.0f}%/{sell_tax:.0f}%")
    elif sell_tax >= 6 or buy_tax >= 6:
        rep.multiplier = min(rep.multiplier, 0.85)
        rep.flags.append(f"🟡 налоги {buy_tax:.0f}%/{sell_tax:.0f}%")

    for flag in ((data.get("summary") or {}).get("flags") or [])[:4]:
        desc = flag.get("description") if isinstance(flag, dict) else str(flag)
        if desc:
            rep.flags.append(f"🟡 {desc}")
    return rep


async def _goplus(session: aiohttp.ClientSession, chain: str, token: str) -> RiskReport:
    """GoPlus Security: контракт (mint/owner/pause), концентрация холдеров, блокировка LP."""
    url = f"https://api.gopluslabs.io/api/v1/token_security/{EVM_CHAIN_IDS[chain]}"
    async with session.get(url, params={"contract_addresses": token},
                           timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            return RiskReport()
        data = await r.json(content_type=None)

    result = (data.get("result") or {}).get(token.lower()) or {}
    if not result:
        return RiskReport()

    def on(key: str) -> bool:
        return str(result.get(key, "0")) == "1"

    rep = RiskReport(ok=True, source="goplus")

    if on("is_honeypot") or on("cannot_sell_all"):
        rep.danger = True
        rep.multiplier = min(rep.multiplier, 0.1)
        rep.flags.append("🔴 GoPlus: продать нельзя (honeypot)")
    if on("hidden_owner") or on("can_take_back_ownership"):
        rep.multiplier = min(rep.multiplier, 0.5)
        rep.flags.append("🔴 скрытый/восстанавливаемый овнер контракта")
    if on("is_mintable"):
        rep.multiplier = min(rep.multiplier, 0.65)
        rep.flags.append("🟡 контракт может доминтить токены")
    if on("transfer_pausable"):
        rep.multiplier = min(rep.multiplier, 0.5)
        rep.flags.append("🔴 переводы токена можно поставить на паузу")
    if on("is_blacklisted"):
        rep.multiplier = min(rep.multiplier, 0.6)
        rep.flags.append("🟡 в контракте есть чёрный список адресов")
    if on("slippage_modifiable"):
        rep.multiplier = min(rep.multiplier, 0.7)
        rep.flags.append("🟡 налоги/проскальзывание можно менять на лету")
    if not on("is_open_source"):
        rep.multiplier = min(rep.multiplier, 0.85)
        rep.flags.append("🟡 исходный код контракта не верифицирован")

    owner_pct = num(result.get("owner_percent"))
    creator_pct = num(result.get("creator_percent"))
    if owner_pct >= 0.2:
        rep.multiplier = min(rep.multiplier, 0.6)
        rep.flags.append(f"🔴 овнер контракта держит {owner_pct*100:.0f}% supply")
    elif creator_pct >= 0.2:
        rep.multiplier = min(rep.multiplier, 0.7)
        rep.flags.append(f"🟡 создатель держит {creator_pct*100:.0f}% supply")

    holders = result.get("holders") or []
    top10 = sum(num(h.get("percent")) for h in holders[:10]
               if not h.get("is_contract") and not h.get("is_locked"))
    if top10 >= 0.5:
        rep.multiplier = min(rep.multiplier, 0.8)
        rep.flags.append(f"🟡 топ-10 холдеров держат {top10*100:.0f}% supply")

    lp_holders = result.get("lp_holders") or []
    if lp_holders:
        lp_locked = sum(num(h.get("percent")) for h in lp_holders if h.get("is_locked"))
        if lp_locked < 0.5:
            rep.multiplier = min(rep.multiplier, 0.55)
            rep.flags.append(f"🔴 LP заблокирован лишь на {lp_locked*100:.0f}%")

    return rep


# ════════════════════════════════════════════════════════════════════════════
#  ФИЛЬТРЫ
# ════════════════════════════════════════════════════════════════════════════

SCAM_WORDS = ("test", "airdrop claim", "reward claim", "visit ", ".com claim")


def passes(pair: dict) -> tuple[bool, str]:
    f = cfg("filters", {}) or {}
    chains = [c.lower() for c in (cfg("scan.chains") or [])]
    if chains and str(pair.get("chainId", "")).lower() not in chains:
        return False, "chain"

    liq = num(dig(pair, "liquidity", "usd"))
    if liq < num(f.get("min_liquidity_usd"), 10000):
        return False, "liq_low"
    max_liq = num(f.get("max_liquidity_usd"))
    if max_liq and liq > max_liq:
        return False, "liq_high"

    if num(dig(pair, "volume", "h1")) < num(f.get("min_volume_h1_usd")):
        return False, "vol_h1"
    if num(dig(pair, "volume", "h24")) < num(f.get("min_volume_h24_usd")):
        return False, "vol_h24"

    b, s = txns(pair, "h1")
    if b + s < num(f.get("min_txns_h1")):
        return False, "txns"

    age = age_minutes(pair)
    if age >= 0:
        if age < num(f.get("min_age_minutes")):
            return False, "too_new"
        if age > num(f.get("max_age_hours"), 168) * 60:
            return False, "too_old"

    fdv = num(pair.get("fdv")) or num(pair.get("marketCap"))
    max_fdv, min_fdv = num(f.get("max_fdv_usd")), num(f.get("min_fdv_usd"))
    if max_fdv and fdv > max_fdv:
        return False, "fdv_high"
    if min_fdv and fdv and fdv < min_fdv:
        return False, "fdv_low"
    if fdv > 0:
        ratio = num(f.get("min_liq_to_fdv"))
        if ratio and liq / fdv < ratio:
            return False, "liq_fdv"

    quotes = [q.upper() for q in (f.get("allowed_quotes") or [])]
    if quotes and str(dig(pair, "quoteToken", "symbol", default="")).upper() not in quotes:
        return False, "quote"

    name = (f"{dig(pair,'baseToken','name',default='')} "
            f"{dig(pair,'baseToken','symbol',default='')}").lower()
    if any(w in name for w in SCAM_WORDS):
        return False, "scam_name"
    if any(w.lower() in name for w in (f.get("blacklist_words") or [])):
        return False, "blacklist"
    return True, "ok"


# ════════════════════════════════════════════════════════════════════════════
#  СКОРИНГ
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    name: str
    points: float
    max_points: float
    note: str = ""


@dataclass
class Analysis:
    pair: dict
    base_score: float = 0.0
    score: float = 0.0
    multiplier: float = 1.0
    signals: list[Signal] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    news: NewsMatch | None = None
    llm: dict | None = None

    @property
    def symbol(self) -> str:
        return str(dig(self.pair, "baseToken", "symbol", default="?"))

    @property
    def token_address(self) -> str:
        return str(dig(self.pair, "baseToken", "address", default=""))

    @property
    def chain(self) -> str:
        return str(self.pair.get("chainId", ""))

    @property
    def pair_address(self) -> str:
        return str(self.pair.get("pairAddress", ""))


def _sig_liquidity(pair: dict) -> Signal:
    liq = num(dig(pair, "liquidity", "usd"))
    pts = 0.0 if liq <= 0 else max(0.0, min(15.0, (math.log10(liq) - 3.7) * 8))
    return Signal("Ликвидность", pts, 15, f"${liq:,.0f}")


def _sig_volume_ratio(pair: dict) -> Signal:
    liq = num(dig(pair, "liquidity", "usd"))
    vol = num(dig(pair, "volume", "h1"))
    if liq <= 0:
        return Signal("Оборот/ликв.", 0, 15, "нет данных")
    r = vol / liq
    pts = piecewise(r, [(0, 0), (0.05, 2), (0.3, 8), (1, 13), (3, 15),
                        (8, 11), (20, 5), (40, 0)])
    return Signal("Оборот/ликв.", pts, 15, f"{r:.2f}x за 1ч")


def _sig_buy_pressure(pair: dict) -> Signal:
    b, s = txns(pair, "h1")
    total = b + s
    if total < 10:
        return Signal("Давление покупок", 2, 12, "мало сделок")
    ratio = b / total
    pts = piecewise(ratio, [(0.30, 0), (0.45, 4), (0.55, 10), (0.65, 12),
                            (0.75, 10), (0.85, 6), (0.95, 2)])
    return Signal("Давление покупок", pts, 12, f"{ratio*100:.0f}% buy, {total} сделок")


def _sig_momentum(pair: dict) -> Signal:
    pc = pair.get("priceChange") or {}
    m5, h1, h6, h24 = (num(pc.get("m5")), num(pc.get("h1")),
                       num(pc.get("h6")), num(pc.get("h24")))
    pts = min(6.0, h1 / 4.0) if h1 > 0 else max(-3.0, h1 / 6.0)
    if m5 > 0:
        pts += min(3.0, m5 / 2.0)
    if h1 > 0 and h6 > 0:
        pts += 2.0                        # тренд подтверждён на двух окнах
        if h1 > h6 / 6.0:
            pts += 2.0                    # ускорение
    if h24 > 0 and h6 > 0 and h1 > 0:
        pts += 2.0
    if h24 > 300 and h1 < 0:
        pts -= 5.0                        # заход после пика
    note = f"5м {m5:+.1f}% · 1ч {h1:+.1f}% · 6ч {h6:+.1f}% · 24ч {h24:+.1f}%"
    return Signal("Моментум", max(0.0, min(15.0, pts)), 15, note)


def _sig_age(pair: dict) -> Signal:
    a = age_minutes(pair)
    if a < 0:
        return Signal("Возраст пула", 4, 8, "неизвестен")
    pts = piecewise(a, [(0, 3), (30, 6), (120, 8), (720, 7), (2880, 5), (5760, 3)])
    return Signal("Возраст пула", pts, 8, f"{a/60:.1f}ч")


def _sig_socials(pair: dict) -> Signal:
    s = socials(pair)
    pts, have = 0.0, []
    if s.get("website"):
        pts += 3; have.append("сайт")
    if s.get("twitter") or s.get("x"):
        pts += 3; have.append("X")
    if s.get("telegram"):
        pts += 2; have.append("TG")
    if s.get("image"):
        pts += 1
    if (pair.get("info") or {}).get("header"):
        pts += 1
    return Signal("Соцсети/инфо", min(10.0, pts), 10, ", ".join(have) or "пусто")


def _sig_boosts(pair: dict, paid_orders: list[dict] | None) -> Signal:
    active = num(dig(pair, "boosts", "active"))
    pts, notes = 0.0, []
    if active > 0:
        pts += min(7.0, 2.0 + math.log10(active + 1) * 3.5)
        notes.append(f"boosts x{active:.0f}")
    if paid_orders and any(str(o.get("status", "")).lower() == "approved"
                           for o in paid_orders):
        pts += 3.0
        notes.append("оплачен профиль DEX")
    return Signal("Маркетинг", min(10.0, pts), 10, ", ".join(notes) or "нет")


def _sig_news(match: NewsMatch | None) -> Signal:
    if not match:
        return Signal("Новости/нарратив", 0, 15, "выключено")
    return Signal("Новости/нарратив", match.points, 15, match.note or "нет упоминаний")


def _risk_flags(pair: dict, prev: dict | None) -> tuple[list[str], float]:
    flags: list[str] = []
    mult = 1.0
    liq = num(dig(pair, "liquidity", "usd"))
    fdv = num(pair.get("fdv")) or num(pair.get("marketCap"))
    vol_h1 = num(dig(pair, "volume", "h1"))
    pc = pair.get("priceChange") or {}
    b, s = txns(pair, "h1")
    total = b + s

    if fdv > 0 and liq / fdv < 0.03:
        flags.append(f"низкая ликв./FDV ({liq/fdv*100:.1f}%)"); mult *= 0.85
    if liq > 0 and vol_h1 / liq > 20:
        flags.append("возможен wash-trading (оборот ≫ ликвидности)"); mult *= 0.8
    if total > 100 and b / max(total, 1) > 0.92:
        flags.append("почти нет продаж — похоже на ботов"); mult *= 0.85
    if num(pc.get("h24")) > 300 and num(pc.get("h1")) < -3:
        flags.append("цена уже после пика, идёт распродажа"); mult *= 0.7
    a = age_minutes(pair)
    if 0 <= a < 30:
        flags.append("очень свежий пул (<30 мин)"); mult *= 0.9
    sc = socials(pair)
    if not (sc.get("twitter") or sc.get("x") or sc.get("telegram")):
        flags.append("нет соцсетей"); mult *= 0.92
    if prev:
        prev_liq = num(prev.get("liquidity_usd"))
        if prev_liq > 0 and liq < prev_liq * 0.7:
            flags.append(f"⚠️ ликвидность упала на {(1-liq/prev_liq)*100:.0f}% — возможен слив LP")
            mult *= 0.4
    return flags, mult


def analyze(pair: dict, news: NewsMatch | None = None, prev: dict | None = None,
            paid_orders: list[dict] | None = None,
            risk_report: RiskReport | None = None,
            llm: dict | None = None) -> Analysis:
    signals = [
        _sig_liquidity(pair), _sig_volume_ratio(pair), _sig_buy_pressure(pair),
        _sig_momentum(pair), _sig_age(pair), _sig_socials(pair),
        _sig_boosts(pair, paid_orders), _sig_news(news),
    ]
    base = sum(s.points for s in signals)
    flags, mult = _risk_flags(pair, prev)

    if risk_report and risk_report.ok:
        mult *= risk_report.multiplier
        flags.extend(risk_report.flags)

    if llm:
        hype, risk = num(llm.get("hype")), num(llm.get("risk"))
        base += max(-6.0, min(6.0, (hype - 5.0) * 1.2))
        if risk >= 8:
            mult *= 0.7
            flags.append(f"LLM оценил риск {risk:.0f}/10")

    a = Analysis(pair=pair, signals=signals, risks=flags, news=news, llm=llm)
    a.base_score = round(max(0.0, min(100.0, base)), 1)
    a.multiplier = round(mult, 3)
    a.score = round(max(0.0, min(100.0, a.base_score * mult)), 1)
    return a


# ════════════════════════════════════════════════════════════════════════════
#  LLM (опционально)
# ════════════════════════════════════════════════════════════════════════════

LLM_SYSTEM = (
    "Ты аналитик мем-коинов. По ончейн-данным и заголовкам новостей оцени монету. "
    "Отвечай ТОЛЬКО JSON без markdown, строго по схеме: "
    '{"hype": 0-10, "risk": 0-10, "verdict": "одно предложение по-русски", '
    '"reasons": ["до трёх коротких пунктов"]}. '
    "hype — способность нарратива привлечь толпу прямо сейчас. risk — вероятность скама/слива. "
    "Будь скептичен: большинство мем-коинов — мусор."
)


async def llm_analyze(session: aiohttp.ClientSession, api_key: str, model: str,
                      pair: dict, news_titles: list[str]) -> dict | None:
    if not api_key:
        return None
    base = pair.get("baseToken") or {}
    b, s = txns(pair, "h1")
    pc = pair.get("priceChange") or {}
    ctx = [
        f"Тикер: {base.get('symbol')} | Название: {base.get('name')}",
        f"Сеть: {pair.get('chainId')} / {pair.get('dexId')}",
        f"Ликвидность: {fmt_usd(num(dig(pair,'liquidity','usd')))} | FDV: {fmt_usd(num(pair.get('fdv')))}",
        f"Объём 1ч: {fmt_usd(num(dig(pair,'volume','h1')))}, 24ч: {fmt_usd(num(dig(pair,'volume','h24')))}",
        f"Цена: 5м {num(pc.get('m5')):+.1f}%, 1ч {num(pc.get('h1')):+.1f}%, "
        f"6ч {num(pc.get('h6')):+.1f}%, 24ч {num(pc.get('h24')):+.1f}%",
        f"Сделки 1ч: покупок {b}, продаж {s}",
    ]
    if news_titles:
        ctx.append("Заголовки:")
        ctx += [f"- {t}" for t in news_titles[:6]]

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

    text = "".join(blk.get("text", "") for blk in data.get("content", [])
                   if isinstance(blk, dict) and blk.get("type") == "text").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ════════════════════════════════════════════════════════════════════════════
#  ХРАНИЛИЩЕ
# ════════════════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    pair_address TEXT NOT NULL, ts REAL NOT NULL, chain TEXT, symbol TEXT,
    price_usd REAL, liquidity_usd REAL, volume_h1 REAL, fdv REAL, score REAL);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(pair_address, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pair_address TEXT NOT NULL,
    token_address TEXT, chain TEXT, symbol TEXT, ts REAL NOT NULL, score REAL,
    price_usd REAL, max_price REAL, last_price REAL, checked_ts REAL);
CREATE INDEX IF NOT EXISTS idx_alerts ON alerts(pair_address, ts);

CREATE TABLE IF NOT EXISTS mutes (key TEXT PRIMARY KEY, until REAL);
"""


class Storage:
    def __init__(self, path: str | Path):
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def last_snapshot(self, pair_address: str, min_age_sec: float = 600) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM snapshots WHERE pair_address=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1",
                (pair_address, time.time() - min_age_sec)).fetchone()
        return dict(row) if row else None

    def add_snapshot(self, pair: dict, score: float) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                (str(pair.get("pairAddress")), time.time(), str(pair.get("chainId")),
                 str(dig(pair, "baseToken", "symbol", default="")),
                 num(pair.get("priceUsd")), num(dig(pair, "liquidity", "usd")),
                 num(dig(pair, "volume", "h1")), num(pair.get("fdv")), float(score)))
            self.conn.commit()

    def prune(self, days: float = 7) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM snapshots WHERE ts < ?",
                              (time.time() - days * 86400,))
            self.conn.commit()

    def last_alert(self, pair_address: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM alerts WHERE pair_address=? ORDER BY ts DESC LIMIT 1",
                (pair_address,)).fetchone()
        return dict(row) if row else None

    def should_alert(self, pair_address: str, score: float,
                     cooldown_min: float, rescore_delta: float) -> bool:
        last = self.last_alert(pair_address)
        if not last:
            return True
        if time.time() - last["ts"] < cooldown_min * 60:
            return False
        return score >= (last["score"] or 0) + rescore_delta

    def record_alert(self, a: Analysis) -> None:
        price = num(a.pair.get("priceUsd"))
        with self.lock:
            self.conn.execute(
                "INSERT INTO alerts (pair_address, token_address, chain, symbol, ts,"
                " score, price_usd, max_price, last_price, checked_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (a.pair_address, a.token_address, a.chain, a.symbol, time.time(),
                 a.score, price, price, price, time.time()))
            self.conn.commit()

    def open_alerts(self, hours: float = 24) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM alerts WHERE ts >= ? ORDER BY ts DESC",
                                     (time.time() - hours * 3600,)).fetchall()
        return [dict(r) for r in rows]

    def update_alert_price(self, alert_id: int, price: float) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE alerts SET last_price=?, max_price=MAX(COALESCE(max_price,0),?),"
                " checked_ts=? WHERE id=?", (price, price, time.time(), alert_id))
            self.conn.commit()

    def stats(self, hours: float = 24) -> dict:
        rows = self.open_alerts(hours)
        if not rows:
            return {"total": 0}
        gains = [((r["max_price"] or r["price_usd"]) / r["price_usd"] - 1) * 100
                 for r in rows if (r["price_usd"] or 0) > 0]
        return {"total": len(rows), "tracked": len(gains),
                "wins_30": len([g for g in gains if g >= 30]),
                "avg_max": sum(gains) / len(gains) if gains else 0,
                "best": max(gains) if gains else 0}

    def top(self, hours: float = 24, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT symbol, chain, pair_address, MAX(score) AS score, MAX(ts) AS ts,"
                " MAX(price_usd) AS price_usd, MAX(max_price) AS max_price FROM alerts"
                " WHERE ts>=? GROUP BY pair_address ORDER BY score DESC LIMIT ?",
                (time.time() - hours * 3600, limit)).fetchall()
        return [dict(r) for r in rows]

    def mute(self, key: str, minutes: float) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO mutes VALUES (?,?)",
                              (key, time.time() + minutes * 60))
            self.conn.commit()

    def unmute(self, key: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM mutes WHERE key=?", (key,))
            self.conn.commit()

    def is_muted(self, key: str) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT until FROM mutes WHERE key=?", (key,)).fetchone()
        return bool(row and row["until"] > time.time())


# ════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ════════════════════════════════════════════════════════════════════════════

class Telegram:
    """Отправка в канал и/или личный чат + приём команд."""

    def __init__(self, session: aiohttp.ClientSession, token: str,
                 channel_id: str = "", admin_chat_id: str = "", dry: bool = False):
        self.session = session
        self.token = token
        self.channel_id = str(channel_id or "").strip()
        self.admin_chat_id = str(admin_chat_id or "").strip()
        self.api = f"https://api.telegram.org/bot{token}"
        self.offset = 0
        self.dry = dry

    @property
    def alert_targets(self) -> list[str]:
        """Куда шлём алерты: канал (основное) + личка, если она задана."""
        targets = [t for t in (self.channel_id, self.admin_chat_id) if t]
        # если канал и личка совпали — не дублируем
        return list(dict.fromkeys(targets))

    async def send(self, text: str, chat_id: str | None = None,
                   preview: bool = False, markup: dict | None = None) -> bool:
        target = chat_id or self.channel_id or self.admin_chat_id
        if self.dry or not self.token or not target:
            print(f"\n--- [TG → {target or 'нет адресата'}] ---\n{text}\n")
            return True
        payload = {"chat_id": target, "text": text[:4090], "parse_mode": "HTML",
                   "disable_web_page_preview": not preview}
        if markup:
            payload["reply_markup"] = markup
        for attempt in range(3):
            try:
                async with self.session.post(f"{self.api}/sendMessage", json=payload,
                                             timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 429:
                        data = await r.json(content_type=None)
                        await asyncio.sleep(num((data.get("parameters") or {}).get("retry_after"), 3))
                        continue
                    if r.status != 200:
                        body = (await r.text())[:250]
                        log.warning("Telegram %s (%s): %s", r.status, target, body)
                        if markup and "BUTTON" in body.upper():
                            log.error("Telegram не принял кнопку мини-аппа: %s", body)
                        if "chat not found" in body or "bot is not a member" in body:
                            log.error("Добавь бота в канал %s и сделай администратором "
                                      "с правом публикации.", target)
                        return False
                    return True
            except Exception as e:  # noqa: BLE001
                log.warning("Отправка в TG: %s", e)
                await asyncio.sleep(2 * (attempt + 1))
        return False

    async def broadcast(self, text: str, short_text: str | None = None) -> bool:
        """Алерт во все адресаты: в канал — короткая версия (если включена),
        в личку — полная."""
        if not self.alert_targets:
            # ни канал, ни личка не заданы (например, прогон с --dry):
            # печатаем в консоль и считаем доставленным, иначе цепочка
            # «алерт → запись в базу → сделка» молча обрывается
            return await self.send(text)
        ok = False
        for target in self.alert_targets:
            body = text
            if short_text and target == self.channel_id and self.channel_id:
                body = short_text
            ok = await self.send(body, chat_id=target) or ok
            await asyncio.sleep(0.4)
        return ok

    async def send_document(self, path: Path | str, caption: str = "",
                            chat_id: str | None = None) -> bool:
        """Отправить файл (например, выгрузку сделок) в чат."""
        target = chat_id or self.admin_chat_id or self.channel_id
        path = Path(path)
        if not path.exists():
            return False
        if self.dry or not self.token or not target:
            print(f"\n--- [TG файл → {target or 'нет адресата'}] {path} ---\n{caption}\n")
            return True
        try:
            data = aiohttp.FormData()
            data.add_field("chat_id", str(target))
            if caption:
                data.add_field("caption", caption[:1000])
                data.add_field("parse_mode", "HTML")
            data.add_field("document", path.read_bytes(), filename=path.name,
                           content_type="text/csv")
            async with self.session.post(f"{self.api}/sendDocument", data=data,
                                         timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    log.warning("sendDocument %s: %s", r.status, (await r.text())[:200])
                    return False
                return True
        except Exception as e:  # noqa: BLE001
            log.warning("Отправка файла: %s", e)
            return False

    async def set_menu_button(self, url: str = "", text: str = "Статистика") -> bool:
        """Кнопка мини-аппа слева от поля ввода в чате с ботом.

        Пустой url убирает кнопку. Это важно: кнопка живёт на стороне Telegram
        и остаётся от прошлого запуска, поэтому старый адрес туннеля продолжал
        бы открываться даже после перезапуска бота с новым.
        """
        if self.dry or not self.token or not self.admin_chat_id:
            return False
        payload = {"chat_id": self.admin_chat_id,
                   "menu_button": {"type": "web_app", "text": text,
                                   "web_app": {"url": url}} if url
                                  else {"type": "commands"}}
        try:
            async with self.session.post(f"{self.api}/setChatMenuButton", json=payload,
                                         timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    log.warning("setChatMenuButton: %s", (await r.text())[:200])
                    return False
                return True
        except Exception as e:  # noqa: BLE001
            log.warning("setChatMenuButton: %s", e)
            return False

    async def me(self) -> dict | None:
        """Проверка токена: кто мы такие. None — токен битый или сети нет."""
        if not self.token:
            return None
        try:
            async with self.session.get(f"{self.api}/getMe",
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json(content_type=None)
                return data.get("result") if data.get("ok") else None
        except Exception as e:  # noqa: BLE001
            log.warning("getMe: %s", e)
            return None

    async def get_updates(self, timeout: int = 25) -> list[dict]:
        if not self.token or self.dry:
            await asyncio.sleep(timeout)
            return []
        params = {"timeout": timeout, "offset": self.offset,
                  "allowed_updates": '["message","channel_post"]'}
        try:
            async with self.session.get(f"{self.api}/getUpdates", params=params,
                                        timeout=aiohttp.ClientTimeout(total=timeout + 15)) as r:
                if r.status != 200:
                    await asyncio.sleep(3)
                    return []
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(3)
            return []
        updates = data.get("result") or []
        for u in updates:
            self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
        return updates


# ════════════════════════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ СООБЩЕНИЙ
# ════════════════════════════════════════════════════════════════════════════

CHAIN_EMOJI = {"solana": "◎", "ethereum": "Ξ", "base": "🔵", "bsc": "🟡",
               "arbitrum": "🔷", "polygon": "🟣", "avalanche": "🔺"}


def score_bar(score: float) -> str:
    filled = int(round(score / 10))
    return "█" * filled + "░" * (10 - filled)


def head_emoji(score: float) -> str:
    return "🔥🔥" if score >= 85 else "🚀" if score >= 75 else "⚡" if score >= 65 else "👀"


def _links(pair: dict) -> str:
    sc = socials(pair)
    chain = str(pair.get("chainId", ""))
    addr = str(dig(pair, "baseToken", "address", default=""))
    out = [f"<a href=\"{esc(pair.get('url'))}\">DexScreener</a>"]
    if sc.get("twitter") or sc.get("x"):
        out.append(f"<a href=\"{esc(sc.get('twitter') or sc.get('x'))}\">X</a>")
    if sc.get("telegram"):
        out.append(f"<a href=\"{esc(sc['telegram'])}\">TG</a>")
    if sc.get("website"):
        out.append(f"<a href=\"{esc(sc['website'])}\">Сайт</a>")
    if chain == "solana":
        out.append(f"<a href=\"https://solscan.io/token/{esc(addr)}\">Solscan</a>")
    return " · ".join(out)


def alert_message(a: Analysis) -> str:
    p = a.pair
    base = p.get("baseToken") or {}
    liq = num(dig(p, "liquidity", "usd"))
    vol_h1, vol_h24 = num(dig(p, "volume", "h1")), num(dig(p, "volume", "h24"))
    fdv = num(p.get("fdv")) or num(p.get("marketCap"))
    pc = p.get("priceChange") or {}
    b, s = txns(p, "h1")
    total = max(b + s, 1)
    chain = str(p.get("chainId", ""))

    lines = [
        f"{head_emoji(a.score)} <b>${esc(base.get('symbol'))}</b> — "
        f"<b>{a.score:.0f}/100</b>  {score_bar(a.score)}",
        f"<i>{esc(base.get('name'))}</i> · {CHAIN_EMOJI.get(chain,'')} "
        f"{esc(chain)}/{esc(p.get('dexId'))}",
        "",
        f"💵 Цена: <b>{fmt_price(num(p.get('priceUsd')))}</b>",
        f"📈 {num(pc.get('m5')):+.1f}% 5м · {num(pc.get('h1')):+.1f}% 1ч · "
        f"{num(pc.get('h6')):+.1f}% 6ч · {num(pc.get('h24')):+.1f}% 24ч",
        f"💧 Ликвидность: <b>{fmt_usd(liq)}</b>" + (f" · FDV: {fmt_usd(fdv)}" if fdv else ""),
        f"📊 Объём: {fmt_usd(vol_h1)} за 1ч · {fmt_usd(vol_h24)} за 24ч"
        + (f" ({vol_h1/liq:.1f}x ликв.)" if liq > 0 else ""),
        f"🔄 Сделки 1ч: {b+s} (покупки {b/total*100:.0f}%)",
        f"⏱ Возраст пула: {fmt_age(age_minutes(p))}",
    ]

    if a.news and (a.news.headlines or a.news.narratives):
        lines.append("")
        if a.news.narratives:
            lines.append("🏷 Нарратив: " + esc(", ".join(a.news.narratives[:4])))
        for item in a.news.headlines[:3]:
            title = esc(item.title[:110])
            lines.append(f"📰 <a href=\"{esc(item.link)}\">{title}</a> — <i>{esc(item.source)}</i>"
                         if item.link else f"📰 {title}")

    if a.llm:
        lines += ["", f"🧠 LLM: хайп {num(a.llm.get('hype')):.0f}/10, "
                      f"риск {num(a.llm.get('risk')):.0f}/10 — "
                      f"{esc(str(a.llm.get('verdict',''))[:200])}"]

    lines += ["", "<b>Сигналы:</b>"]
    for sig in sorted(a.signals, key=lambda x: -x.points):
        lines.append(f"  • {esc(sig.name)}: {sig.points:.1f}/{sig.max_points:.0f}"
                     + (f" — {esc(sig.note)}" if sig.note else ""))

    if a.risks:
        lines += ["", "<b>⚠️ Риски:</b>"]
        lines += [f"  • {esc(r)}" for r in a.risks[:8]]
        if a.multiplier < 1:
            lines.append(f"  <i>(итог снижен ×{a.multiplier:.2f})</i>")

    lines += ["", "🔗 " + _links(p),
              f"<code>{esc(dig(p, 'baseToken', 'address', default=''))}</code>",
              "", "<i>Не финансовый совет. Мем-коины = высокий риск потерять всё.</i>"]
    return "\n".join(lines)


def alert_message_short(a: Analysis) -> str:
    """Компактная версия для канала."""
    p = a.pair
    base = p.get("baseToken") or {}
    pc = p.get("priceChange") or {}
    chain = str(p.get("chainId", ""))
    lines = [
        f"{head_emoji(a.score)} <b>${esc(base.get('symbol'))}</b> — <b>{a.score:.0f}/100</b> "
        f"{CHAIN_EMOJI.get(chain,'')}",
        f"💧 {fmt_usd(num(dig(p,'liquidity','usd')))} · 📊 {fmt_usd(num(dig(p,'volume','h1')))}/1ч "
        f"· ⏱ {fmt_age(age_minutes(p))}",
        f"📈 {num(pc.get('h1')):+.1f}% 1ч · {num(pc.get('h24')):+.1f}% 24ч",
    ]
    if a.news and a.news.narratives:
        lines.append("🏷 " + esc(", ".join(a.news.narratives[:3])))
    if a.risks:
        lines.append("⚠️ " + esc(a.risks[0]))
    lines += ["🔗 " + _links(p),
              f"<code>{esc(dig(p,'baseToken','address',default=''))}</code>"]
    return "\n".join(lines)


def stats_message(st: dict, hours: float) -> str:
    if not st.get("total"):
        return f"За последние {hours:.0f}ч алертов не было."
    return (f"📊 <b>Статистика за {hours:.0f}ч</b>\nАлертов: {st['total']}\n"
            f"Отслежено: {st['tracked']}\nДали +30% и выше: {st['wins_30']}\n"
            f"Средний максимум: {st['avg_max']:+.1f}%\nЛучший: {st['best']:+.1f}%")


def top_message(rows: list[dict], hours: float) -> str:
    if not rows:
        return f"За {hours:.0f}ч ничего не проходило порог."
    out = [f"🏆 <b>Топ за {hours:.0f}ч</b>"]
    for i, r in enumerate(rows, 1):
        p0, pmax = num(r.get("price_usd")), num(r.get("max_price"))
        gain = ((pmax / p0 - 1) * 100) if p0 > 0 else 0
        out.append(f"{i}. <b>${esc(r.get('symbol'))}</b> — {num(r.get('score')):.0f}/100 "
                   f"· макс {gain:+.0f}% · {esc(r.get('chain'))}")
    return "\n".join(out)


HELP = (
    "🤖 <b>Мем-коин сканер</b> (DexScreener + свежие лончи + новости)\n\n"
    "/scan — прогнать скан сейчас\n"
    "/fresh [N] — свежие лончи Solana (Axiom/FOMO) прямо сейчас\n"
    "/check &lt;mint&gt; — полный разбор монеты по адресу\n"
    "/preset [axiom|fomo|safe|degen] — профиль автопилота\n"
    "/freshscore [0-100] — порог по свежим · /auto [on|off]\n"
    "/details [on|off] — показывать разбор монет в чате\n"
    "/app — мини-апп со статистикой в браузере\n"
    "\n<b>Торговля</b>:\n"
    "/wallet — кошелёк бота и баланс\n"
    "/mode — бумага или реальные деньги\n"
    "/buy &lt;минт&gt; [SOL] — купить вручную · /sell &lt;минт&gt; — продать\n"
    "/positions — открытые позиции\n"
    "/pnl [часы] — итог: сколько заработал или потерял\n"
    "/history [N] — последние сделки · /history all — все\n"
    "/export — все сделки таблицей (Excel)\n"
    "/trade [on|off] — включить или остановить входы\n"
    "/close &lt;mint&gt; — закрыть позицию вручную\n"
    "/size [SOL] — размер одной сделки\n\n"
    "/top [часы] — лучшие сигналы\n"
    "/stats [часы] — как отработали алерты\n"
    "/status — состояние бота\n"
    "/threshold [0-100] — порог алерта\n"
    "/mute [минуты] — тишина · /unmute\n"
    "/news — свежие заголовки\n"
    "/id — показать chat_id этого чата\n"
    "/help — справка"
)


# ════════════════════════════════════════════════════════════════════════════
#  БОТ
# ════════════════════════════════════════════════════════════════════════════

class Bot:
    def __init__(self, session: aiohttp.ClientSession, dry: bool = False):
        self.session = session
        self.dex = DexScreener(session)
        self.news = NewsEngine(session)
        self.store = Storage(cfg("storage.path", "data/memebot.db"))
        self.tg = Telegram(session, os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                           os.environ.get("TELEGRAM_CHANNEL_ID", ""),
                           os.environ.get("TELEGRAM_CHAT_ID", ""), dry=dry)
        self.threshold = num(cfg("alerts.min_score"), 65)
        self.trader = None
        if trader is not None and cfg("trade.enabled", True):
            self.trader = trader.Trader(
                session, storage=self.store, send=self.tg.broadcast,
                conf={**(cfg("trade") or {}),
                      "storage_path": cfg("storage.path", "data/memebot.db")})

        self.dash = None
        if dashboard is not None and cfg("dashboard.enabled", True):
            self.dash = dashboard.Dashboard(
                {**(cfg("dashboard") or {}),
                 "storage_path": cfg("storage.path", "data/memebot.db")})

        self.fresh = None
        if axiom_scout is not None and cfg("fresh.enabled", True):
            self.fresh = axiom_scout.FreshScanner(
                session, storage=self.store, send=self.tg.broadcast,
                conf={**(cfg("fresh.overrides") or {}),
                      "storage_path": cfg("storage.path", "data/memebot.db")},
                news=self.news, preset=str(cfg("fresh.preset", "axiom")),
                on_alert=self.on_fresh_alert)
        self.started = time.time()
        self.scans = 0
        self.alerts_sent = 0
        self.last_seen = 0
        self.stop_event = asyncio.Event()

    # ---------- сбор кандидатов ----------

    async def collect(self) -> list[dict]:
        chains = {c.lower() for c in (cfg("scan.chains") or [])}
        src = cfg("scan.sources", {}) or {}
        tasks = []
        if src.get("token_profiles", True):
            tasks.append(self.dex.latest_token_profiles())
        if src.get("boosts_latest", True):
            tasks.append(self.dex.latest_boosts())
        if src.get("boosts_top", True):
            tasks.append(self.dex.top_boosts())

        addresses, seen = [], set()
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if not isinstance(res, list):
                continue
            for item in res:
                if not isinstance(item, dict):
                    continue
                chain = str(item.get("chainId", "")).lower()
                addr = item.get("tokenAddress")
                if not addr or (chains and chain not in chains):
                    continue
                key = f"{chain}:{addr}"
                if key not in seen:
                    seen.add(key)
                    addresses.append(str(addr))

        pairs: list[dict] = []
        if addresses:
            pairs += await self.dex.pairs_for_tokens(
                addresses[:int(num(cfg("scan.max_tokens_per_scan"), 120))])
        for q in (cfg("scan.search_queries") or []):
            pairs += await self.dex.search(str(q))

        best: dict[str, dict] = {}
        for p in pairs:
            if not isinstance(p, dict):
                continue
            chain = str(p.get("chainId", "")).lower()
            if chains and chain not in chains:
                continue
            key = f"{chain}:{dig(p, 'baseToken', 'address', default='')}"
            cur = best.get(key)
            if not cur or num(dig(p, "liquidity", "usd")) > num(dig(cur, "liquidity", "usd")):
                best[key] = p
        return list(best.values())

    # ---------- один проход ----------

    async def scan_once(self, notify_empty: bool = False) -> list[Analysis]:
        pairs = await self.collect()
        self.last_seen = len(pairs)
        candidates = [p for p in pairs if passes(p)[0]]
        log.info("Скан: %d пар, после фильтров %d", len(pairs), len(candidates))

        margin = num(cfg("alerts.deep_check_margin"), 12)
        cooldown = num(cfg("alerts.cooldown_minutes"), 180)
        rescore = num(cfg("alerts.rescore_delta"), 10)
        max_per_scan = int(num(cfg("alerts.max_per_scan"), 5))
        short_mode = bool(cfg("alerts.channel_short", False))
        alerted: list[Analysis] = []

        for pair in candidates:
            try:
                nm = self.news.score(str(dig(pair, "baseToken", "symbol", default="")),
                                     str(dig(pair, "baseToken", "name", default="")))
                prev = self.store.last_snapshot(
                    str(pair.get("pairAddress")),
                    min_age_sec=num(cfg("scan.drain_lookback_seconds"), 900))
                a = analyze(pair, news=nm, prev=prev)
                if a.score >= self.threshold - margin:
                    a = await self.deep_analyze(pair, nm, prev)

                self.store.add_snapshot(pair, a.score)

                if a.score < self.threshold or self.store.is_muted("global"):
                    continue
                if not self.store.should_alert(a.pair_address, a.score, cooldown, rescore):
                    continue
                if len(alerted) >= max_per_scan:
                    log.info("Лимит алертов за проход достигнут")
                    break

                sent = await self.tg.broadcast(
                    alert_message(a),
                    short_text=alert_message_short(a) if short_mode else None)
                if sent:
                    self.store.record_alert(a)
                    self.alerts_sent += 1
                    alerted.append(a)
                    await asyncio.sleep(1.0)
            except Exception as e:  # noqa: BLE001
                log.exception("Ошибка анализа: %s", e)

        self.scans += 1
        if notify_empty and not alerted:
            await self.tg.send(
                f"Скан завершён: {len(pairs)} пар, {len(candidates)} после фильтров, "
                f"выше порога {self.threshold:.0f} — ничего.",
                chat_id=self.tg.admin_chat_id or None)
        return alerted

    async def deep_analyze(self, pair: dict, nm: NewsMatch, prev: dict | None) -> Analysis:
        chain = str(pair.get("chainId", ""))
        token = str(dig(pair, "baseToken", "address", default=""))
        orders_co = (self.dex.paid_orders(chain, token)
                     if cfg("scan.check_paid_orders", True) else asyncio.sleep(0, result=[]))
        risk_co = (risk_check(self.session, chain, token)
                   if cfg("risk.enabled", True) else asyncio.sleep(0, result=RiskReport()))

        orders, report = await asyncio.gather(orders_co, risk_co, return_exceptions=True)
        if isinstance(orders, BaseException):
            orders = []
        if isinstance(report, BaseException):
            report = RiskReport()

        llm_res = None
        if cfg("llm.enabled", False) and os.environ.get("ANTHROPIC_API_KEY"):
            llm_res = await llm_analyze(
                self.session, os.environ["ANTHROPIC_API_KEY"],
                str(cfg("llm.model", "claude-sonnet-5")), pair,
                [h.title for h in nm.headlines])
        return analyze(pair, news=nm, prev=prev, paid_orders=orders,
                       risk_report=report, llm=llm_res)

    # ---------- циклы ----------

    async def scanner_loop(self) -> None:
        if not cfg("scan.enabled", True):
            log.info("Подробный сканер DexScreener выключен — в чат идут только сделки")
            return
        interval = num(cfg("scan.interval_seconds"), 60)
        while not self.stop_event.is_set():
            try:
                await self.scan_once()
            except Exception as e:  # noqa: BLE001
                log.exception("Сбой скана: %s", e)
            await self._sleep(interval)

    async def news_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.news.refresh()
            except Exception as e:  # noqa: BLE001
                log.exception("Сбой новостей: %s", e)
            await self._sleep(self.news.refresh_minutes * 60)

    async def tracker_loop(self) -> None:
        interval = num(cfg("tracking.interval_minutes"), 15) * 60
        window = num(cfg("tracking.window_hours"), 48)
        while not self.stop_event.is_set():
            await self._sleep(interval)
            try:
                rows = self.store.open_alerts(window)
                if not rows:
                    continue
                by_token: dict[str, list[dict]] = {}
                for r in rows:
                    if r.get("token_address"):
                        by_token.setdefault(r["token_address"], []).append(r)
                pairs = await self.dex.pairs_for_tokens(list(by_token)[:150])
                prices: dict[str, float] = {}
                for p in pairs:
                    addr = str(dig(p, "baseToken", "address", default=""))
                    price = num(p.get("priceUsd"))
                    if addr and price > prices.get(addr, 0):
                        prices[addr] = price
                for addr, alerts in by_token.items():
                    price = prices.get(addr)
                    if price:
                        for r in alerts:
                            self.store.update_alert_price(int(r["id"]), price)
                self.store.prune(num(cfg("storage.keep_days"), 7))
            except Exception as e:  # noqa: BLE001
                log.exception("Сбой трекера: %s", e)

    async def on_fresh_alert(self, a: Any) -> None:
        """Сканер нашёл монету с вердиктом «норм» — отдаём её трейдеру."""
        if self.trader is None:
            return
        await self.trader.consider(
            mint=a.launch.mint, symbol=a.launch.symbol, score=a.score,
            launchpad=a.launch.launchpad, price_hint=a.launch.price_usd)

    async def tunnel_loop(self) -> None:
        """Туннель падает — поднимаем заново и обновляем кнопку с новым адресом."""
        if self.dash is None:
            return

        async def on_change(link: str) -> None:
            await self.tg.set_menu_button(link)
            if link:
                await self.tg.send(
                    f"📊 Адрес статистики обновился:\n{esc(link)}",
                    chat_id=self.tg.admin_chat_id or None)

        await self.dash.watch_tunnel(on_change, self.stop_event)

    async def trader_loop(self) -> None:
        """Ведение открытых позиций: тейк, стоп, трейлинг, таймаут."""
        if self.trader is None:
            return
        await self.trader.loop(self.stop_event)

    async def fresh_loop(self) -> None:
        """Автопилот по свежим лончам: сам ищет, сам смотрит новости, сам решает."""
        if self.fresh is None:
            return
        await self.fresh.loop(self.stop_event)

    async def telegram_loop(self) -> None:
        if not self.tg.token or self.tg.dry:
            return
        while not self.stop_event.is_set():
            for u in await self.tg.get_updates():
                msg = u.get("message") or u.get("channel_post") or {}
                text = (msg.get("text") or "").strip()
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not text.startswith("/"):
                    continue
                # команды принимаем только из админ-чата (или из канала, если он же админ)
                allowed = {self.tg.admin_chat_id, self.tg.channel_id} - {""}
                if allowed and chat_id not in allowed and text.split()[0] != "/id":
                    continue
                try:
                    await self.handle_command(text, chat_id)
                except Exception as e:  # noqa: BLE001
                    # молчание в ответ на команду — худший вариант: человек
                    # не понимает, дошло ли вообще
                    log.exception("Команда %s: %s", text, e)
                    await self.tg.send(f"Команда {esc(text.split()[0])} сломалась: "
                                       f"{esc(str(e)[:200])}", chat_id)

    async def handle_command(self, text: str, chat_id: str) -> None:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1] if len(parts) > 1 else None
        def hours() -> float:
            return num(arg, 24) if arg else 24

        if cmd in ("/start", "/help"):
            await self.tg.send(HELP, chat_id)
        elif cmd == "/id":
            await self.tg.send(f"chat_id этого чата: <code>{esc(chat_id)}</code>", chat_id)
        elif cmd == "/status":
            await self.tg.send(
                f"🤖 <b>Статус</b>\nАптайм: {(time.time()-self.started)/3600:.1f}ч\n"
                f"Сканов: {self.scans}\nАлертов: {self.alerts_sent}\n"
                f"Порог: {self.threshold:.0f}\nПар в последнем скане: {self.last_seen}\n"
                f"Новостей в кэше: {len(self.news.items)}\n"
                f"Канал: {esc(self.tg.channel_id or 'не задан')}\n"
                f"Тишина: {'да' if self.store.is_muted('global') else 'нет'}\n"
                + (self.fresh.status_line() if self.fresh else "Свежие лончи: выкл")
                + ("\n" + self.trader.status_line() if self.trader else "")
                + ("\n" + self.dash.status_line() if self.dash else ""), chat_id)
        elif cmd == "/scan":
            await self.tg.send("🔍 Запускаю скан...", chat_id)
            await self.scan_once(notify_empty=True)
        elif cmd == "/top":
            await self.tg.send(top_message(self.store.top(hours()), hours()), chat_id)
        elif cmd == "/stats":
            await self.tg.send(stats_message(self.store.stats(hours()), hours()), chat_id)
        elif cmd == "/threshold":
            if arg:
                self.threshold = max(0.0, min(100.0, num(arg, self.threshold)))
                await self.tg.send(f"Порог алерта: {self.threshold:.0f}/100", chat_id)
            else:
                await self.tg.send(f"Текущий порог: {self.threshold:.0f}. "
                                   f"Пример: /threshold 70", chat_id)
        elif cmd == "/mute":
            minutes = num(arg, 60) if arg else 60
            self.store.mute("global", minutes)
            await self.tg.send(f"🔕 Тишина на {minutes:.0f} мин.", chat_id)
        elif cmd == "/unmute":
            self.store.unmute("global")
            await self.tg.send("🔔 Алерты включены.", chat_id)
        elif cmd == "/positions":
            if self.trader is None:
                await self.tg.send("Торговый модуль не подключён.", chat_id)
            else:
                await self.tg.send(
                    trader.positions_message(self.trader.positions, self.trader.conf),
                    chat_id)
        elif cmd == "/pnl":
            if self.trader is None:
                await self.tg.send("Торговый модуль не подключён.", chat_id)
            else:
                await self.tg.send(self.trader.stats(hours()), chat_id)
        elif cmd in ("/history", "/trades"):
            if self.trader is None:
                await self.tg.send("Торговый модуль не подключён.", chat_id)
            elif arg and arg.lower() in ("all", "все", "всё", "0"):
                pages = self.trader.history_all()
                for i, page in enumerate(pages):
                    await self.tg.send(page, chat_id)
                    if i < len(pages) - 1:
                        await asyncio.sleep(0.6)
                if len(pages) > 1:
                    await self.tg.send("Таблицей в Excel: /export", chat_id)
            else:
                await self.tg.send(self.trader.history(int(num(arg, 10)) if arg else 10),
                                   chat_id)
        elif cmd in ("/export", "/csv"):
            if self.trader is None:
                await self.tg.send("Торговый модуль не подключён.", chat_id)
            else:
                path = self.trader.export_csv()
                if not path:
                    await self.tg.send("Сделок пока нет — выгружать нечего.", chat_id)
                else:
                    ok = await self.tg.send_document(
                        path, "📊 Все сделки: вход, выход, причина, итог. "
                              "Открывается в Excel.", chat_id)
                    if not ok:
                        await self.tg.send(f"Не смог отправить файл. Он лежит здесь:\n"
                                           f"<code>{esc(path)}</code>", chat_id)
        elif cmd == "/trade":
            if self.trader is None:
                await self.tg.send("Торговый модуль не подключён.", chat_id)
            else:
                if arg and arg.lower() in ("on", "вкл", "1"):
                    self.trader.conf["enabled"] = True
                elif arg and arg.lower() in ("off", "выкл", "0"):
                    self.trader.conf["enabled"] = False
                await self.tg.send(self.trader.status_line(), chat_id)
        elif cmd in ("/app", "/dash"):
            if self.dash is None:
                await self.tg.send("Мини-апп выключен в настройках.", chat_id)
            elif self.dash.public_url:
                # ссылку шлём отдельным сообщением и первой: если Telegram
                # отобьёт кнопку мини-аппа, ответ всё равно придёт
                link = self.dash.link()
                await self.tg.send(f"📊 <b>Статистика бота</b>\n{esc(link)}",
                                   chat_id, preview=False)
                ok = await self.tg.send(
                    "Открыть прямо здесь:", chat_id,
                    markup={"inline_keyboard": [[{
                        "text": "📊 Открыть", "web_app": {"url": link}}]]})
                if not ok:
                    await self.tg.send(
                        "Кнопку мини-аппа Telegram не принял — открой ссылку выше "
                        "в браузере. Причина в логе бота.", chat_id)
            else:
                await self.tg.send(
                    "📊 <b>Статистика</b>\n"
                    f"{esc(self.dash.status_line())}\n\n"
                    + ("Поставь cloudflared и перезапусти бота:\n"
                       "<code>winget install --id Cloudflare.cloudflared</code>"
                       if "cloudflared" in self.dash.reason else
                       "Закрой лишние окна бота и перезапусти — порт занят."
                       if "порт" in self.dash.reason else
                       "Подробности в логе бота."),
                    chat_id)
        elif cmd == "/details":
            if self.fresh is None:
                await self.tg.send("Модуль свежих лончей не подключён.", chat_id)
            else:
                if arg and arg.lower() in ("on", "вкл", "1"):
                    self.fresh.conf["notify"] = True
                elif arg and arg.lower() in ("off", "выкл", "0"):
                    self.fresh.conf["notify"] = False
                await self.tg.send(
                    "Разбор каждой монеты в чат: "
                    + ("<b>включён</b> — будут длинные сообщения с сигналами"
                       if self.fresh.conf.get("notify") else
                       "<b>выключен</b> — приходят только сделки"), chat_id)
        elif cmd == "/wallet":
            if self.trader is None:
                await self.tg.send("Торговый модуль не подключён.", chat_id)
            else:
                await self.tg.send(await self.trader.wallet_info(), chat_id)
        elif cmd == "/buy":
            if self.trader is None or not arg:
                await self.tg.send("Пример: <code>/buy &lt;минт&gt; [сколько SOL]</code>",
                                   chat_id)
            else:
                size = num(parts[2], 0) if len(parts) > 2 else 0
                if size:
                    saved, self.trader.conf["size_sol"] = self.trader.conf["size_sol"], size
                await self.tg.send(f"Покупаю на "
                                   f"{num(self.trader.conf.get('size_sol')):.3f} SOL...",
                                   chat_id)
                pos = await self.trader.consider(arg, arg[:6], 100, "вручную")
                if size:
                    self.trader.conf["size_sol"] = saved
                if not pos:
                    await self.tg.send("Не вышло — причина в логе бота.", chat_id)
        elif cmd == "/sell":
            if self.trader is None or not arg:
                await self.tg.send("Пример: <code>/sell &lt;минт или тикер&gt;</code>",
                                   chat_id)
            else:
                pos = next((p for p in self.trader.positions
                            if p.mint == arg or p.symbol.upper() == arg.upper()), None)
                if not pos:
                    await self.tg.send("Такой открытой позиции нет. /positions", chat_id)
                else:
                    sol = await self.trader.prices.sol_price()
                    price = (await self.trader.prices.prices([pos.mint])).get(
                        pos.mint, pos.last_price)
                    await self.trader.close(pos, price, sol, "manual")
        elif cmd == "/mode":
            if self.trader is None:
                await self.tg.send("Торговый модуль не подключён.", chat_id)
            elif arg and arg.lower() in ("live", "реал"):
                await self.tg.send(
                    "Живой режим включается не командой, а в настройках — "
                    "так нельзя случайно начать тратить деньги.\n\n"
                    "1. Впиши <code>SOLANA_PRIVATE_KEY</code> в .env\n"
                    "2. В memebot.py поставь <code>\"mode\": \"live\"</code> "
                    "в блоке <code>CONFIG[\"trade\"]</code>\n"
                    "3. Перезапусти бота и проверь /wallet", chat_id)
            elif arg and arg.lower() in ("paper", "бумага"):
                live_open = [p for p in self.trader.positions if p.mode == "live"]
                if live_open:
                    # иначе бот «закроет» их на бумаге, а купленные токены
                    # так и останутся висеть в кошельке непроданными
                    await self.tg.send(
                        f"Сначала закрой реальные позиции ({len(live_open)} шт.) — "
                        f"командой /sell по каждой. Иначе токены останутся в кошельке, "
                        f"а бот будет считать их проданными.", chat_id)
                else:
                    self.trader.executor = trader.PaperExecutor(self.trader.conf)
                    self.trader.conf["mode"] = "paper"
                    await self.tg.send("Перешёл на бумагу. Реальные деньги "
                                       "больше не тратятся.", chat_id)
            else:
                await self.tg.send(self.trader.status_line(), chat_id)
        elif cmd == "/size":
            if self.trader is None:
                await self.tg.send("Торговый модуль не подключён.", chat_id)
            elif arg:
                self.trader.conf["size_sol"] = max(0.001, num(arg, 0.1))
                await self.tg.send(f"Размер сделки: "
                                   f"{self.trader.conf['size_sol']:.3f} SOL", chat_id)
            else:
                await self.tg.send(f"Размер сделки: "
                                   f"{num(self.trader.conf.get('size_sol')):.3f} SOL. "
                                   f"Пример: /size 0.25", chat_id)
        elif cmd == "/close":
            if self.trader is None or not arg:
                await self.tg.send("Пример: <code>/close &lt;адрес минта&gt;</code>", chat_id)
            else:
                pos = next((p for p in self.trader.positions
                            if p.mint == arg or p.symbol.upper() == arg.upper()), None)
                if not pos:
                    await self.tg.send("Такой открытой позиции нет. /positions", chat_id)
                else:
                    sol = await self.trader.prices.sol_price()
                    price = (await self.trader.prices.prices([pos.mint])).get(
                        pos.mint, pos.last_price)
                    await self.trader.close(pos, price, sol, "manual")
        elif cmd in ("/fresh", "/new"):
            if self.fresh is None:
                await self.tg.send("Модуль свежих лончей не подключён.", chat_id)
            else:
                limit = int(num(arg, 5)) if arg else 5
                await self.tg.send(f"🔍 Смотрю свежие лончи "
                                   f"[{self.fresh.preset.upper()}]...", chat_id)
                items = await self.fresh.analyze_all(limit=max(1, min(limit, 10)), llm=True)
                await self.tg.send(
                    axiom_scout.fresh_list_message(items, self.fresh.conf), chat_id)
        elif cmd == "/check":
            if self.fresh is None or not arg:
                await self.tg.send("Пример: <code>/check &lt;адрес минта&gt;</code>", chat_id)
            else:
                await self.tg.send("🔬 Разбираю монету...", chat_id)
                a = await self.fresh.inspect(arg)
                await self.tg.send(
                    axiom_scout.fresh_message(a, self.fresh.conf) if a
                    else "Не нашёл такую монету (или адрес неверный).", chat_id)
        elif cmd == "/preset":
            if self.fresh is None:
                await self.tg.send("Модуль свежих лончей не подключён.", chat_id)
            elif not arg:
                await self.tg.send(
                    f"Текущий профиль: <b>{esc(self.fresh.preset.upper())}</b>\n"
                    f"Доступны: {', '.join(axiom_scout.PRESETS)}\n"
                    f"Пример: <code>/preset fomo</code>", chat_id)
            elif self.fresh.apply_preset(arg):
                CONFIG["fresh"]["preset"] = arg.lower()
                await self.tg.send(f"✅ Профиль: <b>{esc(arg.upper())}</b>, "
                                   f"порог {self.fresh.threshold:.0f}/100", chat_id)
            else:
                await self.tg.send(f"Не знаю такой профиль. Есть: "
                                   f"{', '.join(axiom_scout.PRESETS)}", chat_id)
        elif cmd == "/freshscore":
            if self.fresh is None:
                await self.tg.send("Модуль свежих лончей не подключён.", chat_id)
            elif arg:
                self.fresh.threshold = max(0.0, min(100.0, num(arg, self.fresh.threshold)))
                await self.tg.send(f"Порог по свежим: {self.fresh.threshold:.0f}/100", chat_id)
            else:
                await self.tg.send(f"Порог по свежим: {self.fresh.threshold:.0f}. "
                                   f"Пример: /freshscore 70", chat_id)
        elif cmd == "/auto":
            if self.fresh is None:
                await self.tg.send("Модуль свежих лончей не подключён.", chat_id)
            else:
                auto = self.fresh.conf.setdefault("auto", {})
                if arg and arg.lower() in ("on", "вкл", "1"):
                    auto["only_enter"] = True
                elif arg and arg.lower() in ("off", "выкл", "0"):
                    auto["only_enter"] = False
                await self.tg.send(
                    "Режим автопилота: " + ("только вердикт «норм» 🟢"
                                            if auto.get("only_enter", True)
                                            else "«норм» + «наблюдать» 🟡"), chat_id)
        elif cmd == "/news":
            items = self.news.items[:8]
            if not items:
                await self.tg.send("Лента пуста.", chat_id)
            else:
                await self.tg.send("📰 <b>Свежие заголовки</b>\n" + "\n".join(
                    f"• <a href=\"{esc(i.link)}\">{esc(i.title[:100])}</a>" for i in items),
                    chat_id)
        else:
            await self.tg.send("Не знаю такой команды. /help", chat_id)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def preflight(self) -> None:
        """Самопроверка при старте: токен, адресаты, доступность данных."""
        if not self.tg.dry:
            info = await self.tg.me()
            if info:
                log.info("Telegram: подключён как @%s", info.get("username"))
            else:
                log.error("Telegram: токен не принят или нет сети. Проверь "
                          "TELEGRAM_BOT_TOKEN в .env")
        if not self.tg.alert_targets and not self.tg.dry:
            log.error("Не задан ни TELEGRAM_CHAT_ID, ни TELEGRAM_CHANNEL_ID — "
                      "алерты слать некуда. Напиши боту /id и впиши число в .env")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.warning("ANTHROPIC_API_KEY не задан — вердикта нейросети не будет, "
                        "работает только скоринг")

    async def run(self) -> None:
        await self.preflight()
        if self.dash is not None:
            await self.dash.start()
            # Публичный адрес нужен, чтобы страница открывалась внутри Telegram.
            # Кнопку переписываем в любом случае: адрес быстрого туннеля меняется
            # при каждом запуске, а старая кнопка на стороне Telegram живёт вечно
            # и вела бы на уже несуществующий домен.
            link = self.dash.link() if await self.dash.start_tunnel() else ""
            await self.tg.set_menu_button(link)
            if not link:
                log.warning("Мини-апп в Telegram недоступен: %s",
                            self.dash.reason or "причина неизвестна")
        await self.news.refresh()
        await self.tg.send(
            f"🤖 Сканер запущен.\nСети: {', '.join(cfg('scan.chains') or [])}\n"
            f"Порог: {self.threshold:.0f}/100\n"
            + (f"Автопилот по свежим лончам: <b>{esc(self.fresh.preset.upper())}</b>, "
               f"порог {self.fresh.threshold:.0f}\n" if self.fresh else "")
            + (((f"💰 <b>РЕАЛЬНЫЕ ДЕНЬГИ</b> — кошелёк "
                 f"<code>{esc(self.trader.wallet_address[:8])}…</code>\n"
                 if self.trader.mode == "live" else "📄 Торговля бумажная\n")
                + f"{num(self.trader.conf.get('size_sol')):.3f} SOL на сделку, "
                  f"лимит {num(self.trader.conf.get('daily_limit_sol')):.2f} SOL в сутки\n")
               if self.trader else "")
            + (f"📊 {esc(self.dash.status_line())}\n" if self.dash else "")
            + f"Канал: {esc(self.tg.channel_id or 'не задан')}\n/help — команды",
            chat_id=self.tg.admin_chat_id or None)
        await asyncio.gather(self.scanner_loop(), self.news_loop(),
                             self.tracker_loop(), self.telegram_loop(),
                             self.fresh_loop(), self.trader_loop(),
                             self.tunnel_loop())


# ════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════════════════════════════════════

async def amain(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env()

    if args.threshold is not None:
        CONFIG["alerts"]["min_score"] = args.threshold
    if args.chains:
        CONFIG["scan"]["chains"] = [c.strip().lower() for c in args.chains.split(",")]
    preset = args.preset or os.environ.get("FRESH_PRESET", "")
    if preset:
        CONFIG["fresh"]["preset"] = preset.strip().lower()

    if not args.dry and not os.environ.get("TELEGRAM_BOT_TOKEN"):
        log.warning("TELEGRAM_BOT_TOKEN не задан — работаю в режиме вывода в консоль")
        args.dry = True
    if not args.dry and not (os.environ.get("TELEGRAM_CHANNEL_ID")
                             or os.environ.get("TELEGRAM_CHAT_ID")):
        log.warning("Не задан ни TELEGRAM_CHANNEL_ID, ни TELEGRAM_CHAT_ID")

    connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=UA, connector=connector) as session:
        bot = Bot(session, dry=args.dry)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, bot.stop_event.set)
            except NotImplementedError:  # Windows
                pass
        if args.once:
            await bot.news.refresh()
            found = await bot.scan_once(notify_empty=True)
            fresh_found = []
            if bot.fresh is not None:
                fresh_found = await bot.fresh.poll()
            log.info("Готово. Алертов: %d (из них по свежим лончам: %d)",
                     len(found) + len(fresh_found), len(fresh_found))
            return
        await bot.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="Мем-коин сканер DexScreener → Telegram")
    ap.add_argument("--once", action="store_true", help="один скан и выход")
    ap.add_argument("--dry", action="store_true", help="не слать в Telegram, печатать в консоль")
    ap.add_argument("--threshold", type=float, help="порог алерта 0-100")
    ap.add_argument("--chains", type=str, help="сети через запятую, напр. solana,base")
    ap.add_argument("--preset", type=str,
                    help="профиль автопилота по свежим: axiom|fomo|safe|degen")
    ap.add_argument("-v", "--verbose", action="store_true")
    try:
        asyncio.run(amain(ap.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
