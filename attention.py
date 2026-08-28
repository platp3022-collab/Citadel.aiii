#!/usr/bin/env python3
"""
Внимание: говорят ли о монете за пределами графика.

Мем-коин живёт вниманием, а не метриками. Пока о тикере пишут — цена растёт;
разговор кончился — не спасут ни холдеры, ни объём. Ончейн показывает
последствие, разговор показывает причину, поэтому её и надо смотреть.

Что смотрим, всё без ключей и без платных API:

* Reddit — ищем тикер и название за сутки: сколько постов, какой отклик.
* Google News — попал ли инфоповод в ленты за двое суток.
* DexScreener boosts — за монету платят за показы, значит внимание покупают.
* Ссылки на соцсети у самого токена — их отсутствие говорит само за себя.

Отдельно ведём историю: у каждой монеты запоминаем пик внимания. Разговор
пошёл на спад — это сигнал выходить, а не ждать разворота.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

import aiohttp

log = logging.getLogger("attention")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "check_top": 6,             # для скольких лучших кандидатов идём в сеть
    "cache_minutes": 20,        # столько держим ответ, чтобы не долбить источники
    "timeout": 8,

    "reddit": True,
    "news": True,
    "boosts": True,

    "reddit_bonus": 9,          # максимум за разговор на Reddit
    "news_bonus": 6,            # максимум за попадание в новости
    "boost_bonus": 4,           # за платное продвижение
    "socials_bonus": 3,         # за живые ссылки у токена
    "silence_penalty": -4,      # тишина вокруг монеты — это тоже сигнал
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; CitadelBot/1.0)"}
REDDIT = "https://www.reddit.com/search.json"
GOOGLE_NEWS = "https://news.google.com/rss/search"
DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"

# слишком короткие и слишком общие тикеры ищутся бессмысленно: по запросу
# «AI» или «SOL» найдётся весь интернет, и это не про нашу монету
STOP_TICKERS = {"ai", "sol", "usd", "eth", "btc", "the", "new", "buy", "dog", "cat"}


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 пост», «2 поста», «8 постов» — иначе строка режет глаз."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Attention:
    """Сколько вокруг монеты разговора и куда он движется."""
    points: float = 0.0
    reddit_posts: int = 0
    reddit_score: int = 0
    news_items: int = 0
    boosted: bool = False
    socials: int = 0
    checked: bool = False        # ходили ли в сеть вообще
    peak: float = 0.0            # лучший показатель за жизнь монеты
    notes: list[str] = field(default_factory=list)
    ts: float = 0.0

    @property
    def note(self) -> str:
        return " · ".join(self.notes) if self.notes else (
            "о монете нигде не говорят" if self.checked else "внимание не проверяли")

    @property
    def decayed(self) -> bool:
        """Разговор был и сошёл на нет — самый честный сигнал на выход."""
        return bool(self.peak > 6 and self.points <= self.peak * 0.5)


class AttentionReader:
    """Ходит по бесплатным источникам и считает, сколько вокруг монеты шума."""

    def __init__(self, session: aiohttp.ClientSession | None = None,
                 conf: dict[str, Any] | None = None):
        self.conf = {**DEFAULTS, **(conf or {})}
        self.session = session
        self.cache: dict[str, Attention] = {}
        self.boosted: set[str] = set()
        self.boosts_ts = 0.0
        self.last_error = ""

    # ---------- источники ----------

    async def _get(self, url: str, params: dict | None = None, as_text: bool = False):
        if self.session is None:
            return None
        try:
            async with self.session.get(
                    url, params=params, headers=UA,
                    timeout=aiohttp.ClientTimeout(
                        total=num(self.conf.get("timeout"), 8))) as r:
                if r.status != 200:
                    self.last_error = f"{url.split('/')[2]}: HTTP {r.status}"
                    return None
                return await (r.text() if as_text else r.json(content_type=None))
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{url.split('/')[2]}: {type(e).__name__}"
            return None

    async def reddit(self, query: str) -> tuple[int, int]:
        """Сколько постов за сутки и какой у них отклик."""
        data = await self._get(REDDIT, {"q": query, "sort": "new", "t": "day",
                                        "limit": "25"})
        posts = (((data or {}).get("data") or {}).get("children") or [])
        cutoff = time.time() - 86400
        fresh = [p for p in posts
                 if isinstance(p, dict) and num(((p.get("data") or {})
                                                 .get("created_utc"))) >= cutoff]
        score = sum(int(num((p.get("data") or {}).get("score")))
                    + int(num((p.get("data") or {}).get("num_comments")))
                    for p in fresh)
        return len(fresh), score

    async def news(self, query: str) -> int:
        """Сколько заголовков про монету за двое суток."""
        text = await self._get(f"{GOOGLE_NEWS}?q={quote_plus(query)}"
                               "&hl=en-US&gl=US&ceid=US:en", as_text=True)
        if not text:
            return 0
        # разбирать RSS целиком незачем: считаем свежие записи
        return min(len(re.findall(r"<item>", text)), 20)

    async def boosts(self) -> set[str]:
        """Монеты, за показы которых кто-то заплатил."""
        if time.time() - self.boosts_ts < 300:
            return self.boosted
        data = await self._get(DEX_BOOSTS)
        out = set()
        for item in (data or []):
            if isinstance(item, dict) and item.get("tokenAddress"):
                out.add(str(item["tokenAddress"]))
        self.boosted, self.boosts_ts = out, time.time()
        return out

    # ---------- разбор одной монеты ----------

    def _query(self, symbol: str, name: str) -> str:
        sym = (symbol or "").strip()
        if sym and sym.lower() not in STOP_TICKERS and len(sym) >= 3:
            return f"${sym}"
        # тикер бесполезен для поиска — идём по названию
        return (name or sym).strip()[:40]

    async def read(self, mint: str, symbol: str = "", name: str = "",
                   socials: int = 0) -> Attention:
        """Считает внимание вокруг монеты. Кэш держим, источники щадим."""
        a = Attention()
        if not self.conf.get("enabled", True):
            return a

        old = self.cache.get(mint)
        if old and time.time() - old.ts < num(self.conf.get("cache_minutes"), 20) * 60:
            return old

        query = self._query(symbol, name)
        if not query:
            return a

        jobs = []
        if self.conf.get("reddit", True):
            jobs.append(self.reddit(query))
        if self.conf.get("news", True):
            jobs.append(self.news(query))
        if self.conf.get("boosts", True):
            jobs.append(self.boosts())
        results = await asyncio.gather(*jobs, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                continue
            if isinstance(res, tuple):
                a.reddit_posts, a.reddit_score = res
            elif isinstance(res, set):
                a.boosted = mint in res
            elif isinstance(res, int):
                a.news_items = res
        a.checked = True
        a.socials = int(socials)

        # ---- в баллы ----
        if a.reddit_posts:
            pts = min(num(self.conf.get("reddit_bonus"), 9),
                      a.reddit_posts * 2 + min(a.reddit_score / 20.0, 5))
            a.points += pts
            a.notes.append(f"Reddit: {a.reddit_posts} "
                           + plural(a.reddit_posts, "пост", "поста", "постов")
                           + " за сутки"
                           + (f", отклик {a.reddit_score}" if a.reddit_score else ""))
        if a.news_items:
            a.points += min(num(self.conf.get("news_bonus"), 6), a.news_items * 1.5)
            a.notes.append(f"в новостях: {a.news_items} "
                           + plural(a.news_items, "заголовок", "заголовка",
                                    "заголовков"))
        if a.boosted:
            a.points += num(self.conf.get("boost_bonus"), 4)
            a.notes.append("за показы платят")
        if a.socials:
            a.points += min(num(self.conf.get("socials_bonus"), 3), a.socials * 1.5)
            a.notes.append(f"соцсети у токена: {a.socials} "
                           + plural(a.socials, "ссылка", "ссылки", "ссылок"))
        if not (a.reddit_posts or a.news_items or a.boosted):
            # монета есть, а разговора нет — расти ей не на чем
            a.points += num(self.conf.get("silence_penalty"), -4)
            a.notes.append("тишина: нигде не обсуждают")

        a.peak = max(num(getattr(old, "peak", 0.0)), a.points)
        a.ts = time.time()
        self.cache[mint] = a
        if len(self.cache) > 2000:
            oldest = sorted(self.cache.items(), key=lambda kv: kv[1].ts)[:500]
            for key, _ in oldest:
                self.cache.pop(key, None)
        return a

    async def enrich(self, launches: list[Any]) -> None:
        """Проверяет внимание для лучших кандидатов прохода."""
        if not self.conf.get("enabled", True) or self.session is None:
            return
        top = launches[:int(num(self.conf.get("check_top"), 6))]
        for l in top:
            socials = sum(1 for s in (getattr(l, "twitter", ""),
                                      getattr(l, "telegram", ""),
                                      getattr(l, "website", "")) if s)
            try:
                l.attention = await self.read(l.mint, l.symbol, l.name, socials)
            except Exception as e:  # noqa: BLE001
                log.debug("внимание %s: %s", getattr(l, "symbol", "")[:8], e)
            await asyncio.sleep(0.3)      # источники бесплатные, не наглеем

    def status_line(self) -> str:
        if not self.conf.get("enabled", True):
            return "Внимание: проверка выключена"
        return (f"Внимание: проверено монет {len(self.cache)}, "
                f"платных продвижений в ленте {len(self.boosted)}"
                + (f", ошибка: {self.last_error}" if self.last_error else ""))
