#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polybot — автоматический торговый бот для Polymarket с терминальным дашбордом.

Что делает:
    1. тянет активные рынки Polymarket (Gamma API), фильтрует по ликвидности/спреду/сроку;
    2. качает стакан (CLOB API), историю цен и ленту сделок (Data API);
    3. гоняет по ним три стратегии: RSI+VWAP, CVD-дивергенция, возврат к среднему;
    4. считает edge (перевес над ценой), режет размер позиции риск-менеджером;
    5. исполняет — в бумажном режиме по реальному стакану, в боевом через CLOB;
    6. рисует всё это в терминале: сигналы, исполнения, кривую эквити, P&L.

Установка:
    pip install -r requirements.txt

Настройка (переменные окружения или файл .env рядом со скриптом):
    BANKROLL=1000                # стартовый банк в USDC (бумажный режим)
    MAX_POSITIONS=6              # сколько позиций держим одновременно
    RISK_PER_TRADE=0.02          # доля банка под один вход
    DAILY_LOSS_LIMIT=0.06        # дневной стоп по просадке — бот выключает торговлю
    POLYMARKET_PRIVATE_KEY=      # только для --live
    POLYMARKET_API_KEY=          # ключи CLOB (--live), создаются через py-clob-client
    POLYMARKET_API_SECRET=
    POLYMARKET_API_PASSPHRASE=

Запуск:
    python polybot.py                  # бумажная торговля по реальным котировкам + дашборд
    python polybot.py --once           # один цикл сканирования и выход
    python polybot.py --no-ui          # без дашборда, обычные логи (для сервера)
    python polybot.py --backtest 7d    # прогон стратегий по истории цен
    python polybot.py --live           # реальные ордера (нужны ключи, см. README)

Отказ от ответственности: это инструмент исполнения твоей же стратегии, а не машина по печати
денег. Прогнозные рынки — торговля с отрицательной суммой после комиссий и спреда; любой бэктест
переоценивает результат. Никаких «+2643$ за ночь» здесь нет и быть не может: по умолчанию бот
торгует бумагой, боевой режим включается руками и на свой риск.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import shutil
import signal
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import aiohttp
except ImportError:
    sys.exit("Нужен aiohttp:  pip install -r requirements.txt")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "polybot.sqlite3"

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

log = logging.getLogger("polybot")


# --------------------------------------------------------------------------------------
# Конфиг
# --------------------------------------------------------------------------------------
def load_dotenv(path: Path) -> None:
    """Простой .env-парсер: KEY=VALUE, без экспортов и подстановок."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(float(os.environ.get(key, "") or default))
    except ValueError:
        return default


@dataclass
class Config:
    bankroll: float = 1000.0
    risk_per_trade: float = 0.02          # доля банка под один вход
    max_positions: int = 6
    max_position_usd: float = 150.0
    daily_loss_limit: float = 0.06        # -6% от банка за сутки → торговля на паузу
    max_drawdown: float = 0.20            # -20% от пика → полный стоп

    # фильтры рынков
    universe_size: int = 40               # сколько рынков держим в работе
    min_liquidity: float = 5_000.0        # $ в стакане по данным Gamma
    min_volume_24h: float = 20_000.0
    max_spread: float = 0.03              # 3 цента — шире не торгуем
    min_price: float = 0.08               # хвосты не берём: там комиссия съедает edge
    max_price: float = 0.92
    min_hours_to_close: float = 6.0
    max_days_to_close: float = 120.0

    # исполнение
    min_edge: float = 0.02                # минимальный перевес после комиссий, в центах цены
    take_profit: float = 0.06
    stop_loss: float = 0.04
    time_stop_min: float = 240.0          # держим не дольше 4 часов
    fee_bps: float = 0.0                  # тейкерская комиссия Polymarket, б.п. (сейчас 0)
    slippage_ticks: int = 1               # запас на проскальзывание, тиков по 0.01

    # циклы
    scan_interval: float = 45.0           # полный пересчёт вселенной
    tick_interval: float = 3.0            # переоценка позиций и перерисовка
    max_concurrency: int = 8

    live: bool = False
    dry_ui: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        c.bankroll = env_float("BANKROLL", c.bankroll)
        c.risk_per_trade = env_float("RISK_PER_TRADE", c.risk_per_trade)
        c.max_positions = env_int("MAX_POSITIONS", c.max_positions)
        c.max_position_usd = env_float("MAX_POSITION_USD", c.bankroll * 0.15)
        c.daily_loss_limit = env_float("DAILY_LOSS_LIMIT", c.daily_loss_limit)
        c.max_drawdown = env_float("MAX_DRAWDOWN", c.max_drawdown)
        c.universe_size = env_int("UNIVERSE_SIZE", c.universe_size)
        c.min_liquidity = env_float("MIN_LIQUIDITY", c.min_liquidity)
        c.min_volume_24h = env_float("MIN_VOLUME_24H", c.min_volume_24h)
        c.max_spread = env_float("MAX_SPREAD", c.max_spread)
        c.min_edge = env_float("MIN_EDGE", c.min_edge)
        c.take_profit = env_float("TAKE_PROFIT", c.take_profit)
        c.stop_loss = env_float("STOP_LOSS", c.stop_loss)
        c.time_stop_min = env_float("TIME_STOP_MIN", c.time_stop_min)
        c.fee_bps = env_float("FEE_BPS", c.fee_bps)
        c.scan_interval = env_float("SCAN_INTERVAL", c.scan_interval)
        c.tick_interval = env_float("TICK_INTERVAL", c.tick_interval)
        return c


# --------------------------------------------------------------------------------------
# Мелкие утилиты
# --------------------------------------------------------------------------------------
class C:
    """ANSI-цвета дашборда."""
    RESET = "\x1b[0m"
    DIM = "\x1b[2m"
    BOLD = "\x1b[1m"
    RED = "\x1b[38;5;203m"
    GREEN = "\x1b[38;5;114m"
    YELLOW = "\x1b[38;5;179m"
    BLUE = "\x1b[38;5;110m"
    GREY = "\x1b[38;5;244m"
    WHITE = "\x1b[38;5;253m"


def now() -> float:
    return time.time()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def money(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def short(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def strip_ansi(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\x1b":
            j = text.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def pad(text: str, width: int) -> str:
    """Дополнить строку пробелами с учётом невидимых ANSI-последовательностей."""
    visible = len(strip_ansi(text))
    if visible >= width:
        return text
    return text + " " * (width - visible)


def parse_json_field(value: Any) -> Any:
    """Gamma отдаёт списки строками JSON — разворачиваем."""
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return []
    return []


def parse_iso(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
class Http:
    """aiohttp-обёртка с ретраями и мягкой деградацией: сеть упала — бот не падает."""

    def __init__(self, concurrency: int = 8) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(concurrency)
        self.errors = 0
        self.requests = 0
        self.last_error: str = ""

    async def __aenter__(self) -> "Http":
        timeout = aiohttp.ClientTimeout(total=20, connect=8)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "polybot/1.0", "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session:
            await self._session.close()

    async def get_json(self, url: str, params: dict[str, Any] | None = None,
                       attempts: int = 3) -> Any:
        assert self._session is not None, "Http используется вне контекста"
        delay = 0.6
        for attempt in range(attempts):
            try:
                async with self._sem:
                    self.requests += 1
                    async with self._session.get(url, params=params) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(delay + random.random())
                            delay *= 2
                            continue
                        if resp.status >= 500:
                            raise aiohttp.ClientError(f"HTTP {resp.status}")
                        if resp.status >= 400:
                            self.last_error = f"{resp.status} {url.rsplit('/', 1)[-1]}"
                            return None
                        return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if attempt == attempts - 1:
                    self.errors += 1
                    return None
                await asyncio.sleep(delay + random.random() * 0.3)
                delay *= 2
        return None


# --------------------------------------------------------------------------------------
# Модель данных
# --------------------------------------------------------------------------------------
@dataclass
class Market:
    market_id: str
    condition_id: str
    question: str
    slug: str
    token_id: str            # clob-токен исхода, которым торгуем (YES)
    outcome: str             # название исхода
    no_token_id: str         # парный токен (NO) — им играем сигнал на продажу
    no_outcome: str
    price: float             # последняя цена исхода
    liquidity: float
    volume_24h: float
    end_ts: float | None

    @property
    def hours_to_close(self) -> float:
        if not self.end_ts:
            return 9_999.0
        return (self.end_ts - now()) / 3600.0


@dataclass
class Book:
    bids: list[tuple[float, float]] = field(default_factory=list)   # (цена, размер), лучшая первой
    asks: list[tuple[float, float]] = field(default_factory=list)

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 1.0

    @property
    def mid(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        if not self.bids or not self.asks:
            return 1.0
        return self.best_ask - self.best_bid

    def depth(self, side: str, levels: int = 5) -> float:
        book = self.asks if side == "BUY" else self.bids
        return sum(size for _, size in book[:levels])


@dataclass
class Trade:
    ts: float
    price: float
    size: float
    side: str  # BUY / SELL глазами тейкера


@dataclass
class Snapshot:
    """Всё, что стратегии знают о рынке в момент решения."""
    market: Market
    book: Book
    prices: list[tuple[float, float]] = field(default_factory=list)   # (ts, цена)
    trades: list[Trade] = field(default_factory=list)
    fetched_at: float = field(default_factory=now)

    @property
    def closes(self) -> list[float]:
        return [p for _, p in self.prices]

    @property
    def mid(self) -> float:
        return self.book.mid or self.market.price


@dataclass
class Signal:
    strategy: str
    market: Market
    side: str            # BUY / SELL
    fair: float          # оценка справедливой цены стратегией
    price: float         # цена, по которой готовы влезть
    edge: float          # fair - price для BUY (после комиссий)
    confidence: float    # 0..1
    reason: str
    ts: float = field(default_factory=now)


@dataclass
class Fill:
    ts: float
    token_id: str
    question: str
    side: str
    size: float          # штук (shares)
    price: float         # средняя цена исполнения
    fee: float
    strategy: str
    pnl: float = 0.0     # заполняется на выходе из позиции


@dataclass
class Position:
    token_id: str
    question: str
    outcome: str
    strategy: str
    size: float
    entry: float
    opened_at: float
    fair: float
    mark: float = 0.0
    condition_id: str = ""
    end_ts: float | None = None

    @property
    def cost(self) -> float:
        return self.size * self.entry

    def upnl(self, mark: float | None = None) -> float:
        price = self.mark if mark is None else mark
        return (price - self.entry) * self.size

    @property
    def age_min(self) -> float:
        return (now() - self.opened_at) / 60.0


# --------------------------------------------------------------------------------------
# Загрузка рыночных данных
# --------------------------------------------------------------------------------------
class MarketData:
    def __init__(self, http: Http, cfg: Config) -> None:
        self.http = http
        self.cfg = cfg

    async def universe(self) -> list[Market]:
        """Активные рынки с ордербуком, отсортированные по обороту за сутки."""
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": 200,
            "order": "volume24hr",
            "ascending": "false",
        }
        raw = await self.http.get_json(f"{GAMMA_API}/markets", params)
        if not isinstance(raw, list):
            return []

        markets: list[Market] = []
        for item in raw:
            market = self._to_market(item)
            if market and self._passes_filters(market):
                markets.append(market)
        markets.sort(key=lambda m: m.volume_24h, reverse=True)
        return markets[: self.cfg.universe_size]

    def _to_market(self, item: dict[str, Any]) -> Market | None:
        if not item.get("enableOrderBook", True):
            return None
        token_ids = parse_json_field(item.get("clobTokenIds"))
        outcomes = parse_json_field(item.get("outcomes"))
        prices = parse_json_field(item.get("outcomePrices"))
        if not token_ids or not outcomes:
            return None
        try:
            price = float(prices[0]) if prices else float(item.get("lastTradePrice") or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return Market(
            market_id=str(item.get("id") or ""),
            condition_id=str(item.get("conditionId") or ""),
            question=str(item.get("question") or item.get("slug") or "—"),
            slug=str(item.get("slug") or ""),
            token_id=str(token_ids[0]),
            outcome=str(outcomes[0]),
            no_token_id=str(token_ids[1]) if len(token_ids) > 1 else "",
            no_outcome=str(outcomes[1]) if len(outcomes) > 1 else "No",
            price=price,
            liquidity=float(item.get("liquidityNum") or item.get("liquidity") or 0.0),
            volume_24h=float(item.get("volume24hr") or item.get("volume24hrClob") or 0.0),
            end_ts=parse_iso(item.get("endDate")),
        )

    def _passes_filters(self, m: Market) -> bool:
        cfg = self.cfg
        if m.liquidity < cfg.min_liquidity or m.volume_24h < cfg.min_volume_24h:
            return False
        if not (cfg.min_price <= m.price <= cfg.max_price):
            return False
        hours = m.hours_to_close
        if hours < cfg.min_hours_to_close or hours > cfg.max_days_to_close * 24:
            return False
        return True

    async def book(self, token_id: str) -> Book:
        raw = await self.http.get_json(f"{CLOB_API}/book", {"token_id": token_id})
        if not isinstance(raw, dict):
            return Book()
        return self._to_book(raw)

    @staticmethod
    def _to_book(raw: dict[str, Any]) -> Book:
        def levels(key: str, reverse: bool) -> list[tuple[float, float]]:
            out: list[tuple[float, float]] = []
            for lvl in raw.get(key) or []:
                try:
                    out.append((float(lvl["price"]), float(lvl["size"])))
                except (KeyError, TypeError, ValueError):
                    continue
            out.sort(key=lambda x: x[0], reverse=reverse)
            return out

        # CLOB отдаёт биды по возрастанию, аски по убыванию — приводим «лучшая первой».
        return Book(bids=levels("bids", reverse=True), asks=levels("asks", reverse=False))

    async def prices(self, token_id: str, interval: str = "1d",
                     fidelity: int = 5) -> list[tuple[float, float]]:
        raw = await self.http.get_json(
            f"{CLOB_API}/prices-history",
            {"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        history = (raw or {}).get("history") if isinstance(raw, dict) else None
        if not history:
            return []
        out: list[tuple[float, float]] = []
        for point in history:
            try:
                out.append((float(point["t"]), float(point["p"])))
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: x[0])
        return out

    async def trades(self, condition_id: str, limit: int = 200) -> list[Trade]:
        raw = await self.http.get_json(
            f"{DATA_API}/trades", {"market": condition_id, "limit": limit, "takerOnly": "true"}
        )
        if not isinstance(raw, list):
            return []
        out: list[Trade] = []
        for item in raw:
            try:
                out.append(
                    Trade(
                        ts=float(item.get("timestamp") or 0),
                        price=float(item.get("price") or 0),
                        size=float(item.get("size") or 0),
                        side=str(item.get("side") or "BUY").upper(),
                    )
                )
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda t: t.ts)
        return out

    async def snapshot(self, market: Market) -> Snapshot | None:
        book, prices, trades = await asyncio.gather(
            self.book(market.token_id),
            self.prices(market.token_id),
            self.trades(market.condition_id),
        )
        if not book.bids or not book.asks:
            return None
        return Snapshot(market=market, book=book, prices=prices, trades=trades)


# --------------------------------------------------------------------------------------
# Индикаторы
# --------------------------------------------------------------------------------------
def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """RSI по Уайлдеру. None, если данных не хватает."""
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def vwap(trades: Sequence[Trade], window_sec: float = 6 * 3600) -> float | None:
    """VWAP по ленте сделок за окно. Без сделок — None."""
    cutoff = now() - window_sec
    notional = volume = 0.0
    for t in trades:
        if t.ts < cutoff or t.size <= 0:
            continue
        notional += t.price * t.size
        volume += t.size
    if volume <= 0:
        return None
    return notional / volume


def cvd_series(trades: Sequence[Trade], buckets: int = 12) -> list[float]:
    """Кумулятивная дельта объёма, разбитая на равные корзины по времени."""
    if len(trades) < 4:
        return []
    start, end = trades[0].ts, trades[-1].ts
    span = max(end - start, 1.0)
    acc = [0.0] * buckets
    for t in trades:
        idx = min(int((t.ts - start) / span * buckets), buckets - 1)
        acc[idx] += t.size if t.side == "BUY" else -t.size
    out, running = [], 0.0
    for delta in acc:
        running += delta
        out.append(running)
    return out


def price_buckets(trades: Sequence[Trade], buckets: int = 12) -> list[float]:
    """Средняя цена по тем же корзинам, что и CVD, — чтобы искать дивергенцию."""
    if len(trades) < 4:
        return []
    start, end = trades[0].ts, trades[-1].ts
    span = max(end - start, 1.0)
    sums = [0.0] * buckets
    counts = [0] * buckets
    for t in trades:
        idx = min(int((t.ts - start) / span * buckets), buckets - 1)
        sums[idx] += t.price
        counts[idx] += 1
    out, last = [], trades[0].price
    for total, count in zip(sums, counts):
        last = total / count if count else last
        out.append(last)
    return out


def zscore(values: Sequence[float], window: int = 48) -> float | None:
    tail = list(values[-window:])
    if len(tail) < 12:
        return None
    mean = statistics.fmean(tail)
    try:
        sd = statistics.stdev(tail)
    except statistics.StatisticsError:
        return None
    if sd < 1e-6:
        return None
    return (tail[-1] - mean) / sd


def realized_vol(values: Sequence[float], window: int = 48) -> float:
    tail = list(values[-window:])
    if len(tail) < 4:
        return 0.0
    diffs = [b - a for a, b in zip(tail, tail[1:])]
    try:
        return statistics.stdev(diffs)
    except statistics.StatisticsError:
        return 0.0


# --------------------------------------------------------------------------------------
# Стратегии
# --------------------------------------------------------------------------------------
class Strategy:
    name = "base"
    title = "BASE"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.signals = 0
        self.notes: list[str] = []       # последние строки для дашборда

    def note(self, text: str) -> None:
        self.notes.append(text)
        del self.notes[:-40]

    def evaluate(self, snap: Snapshot) -> Signal | None:
        raise NotImplementedError

    # общий помощник: собрать сигнал, если перевес пережил комиссии
    def _make(self, snap: Snapshot, side: str, fair: float, confidence: float,
              reason: str) -> Signal | None:
        book = snap.book
        price = book.best_ask if side == "BUY" else book.best_bid
        if price <= 0 or price >= 1:
            return None
        fee = price * self.cfg.fee_bps / 10_000.0
        edge = (fair - price - fee) if side == "BUY" else (price - fair - fee)
        if edge < self.cfg.min_edge:
            return None
        if book.spread > self.cfg.max_spread:
            return None
        self.signals += 1
        self.note(f"{side} {short(snap.market.question, 28)} @ {price:.2f} edge {edge*100:+.1f}c")
        return Signal(
            strategy=self.name,
            market=snap.market,
            side=side,
            fair=fair,
            price=price,
            edge=edge,
            confidence=clamp(confidence, 0.0, 1.0),
            reason=reason,
        )


class RsiVwap(Strategy):
    """Перепроданность/перекупленность по RSI + отклонение от VWAP ленты сделок.

    Идея простая: цена ушла от средневзвешенной, RSI подтверждает истощение движения —
    ставим на возврат к VWAP. Справедливая цена = VWAP, поджатый к текущей.
    """
    name = "rsi_vwap"
    title = "RSI + VWAP"

    def evaluate(self, snap: Snapshot) -> Signal | None:
        closes = snap.closes
        value = rsi(closes, period=14)
        anchor = vwap(snap.trades)
        if value is None or anchor is None:
            return None
        mid = snap.mid
        gap = anchor - mid
        if abs(gap) < 0.015:
            return None

        # тянемся только на половину расстояния до VWAP — так честнее оценивать edge
        fair = mid + gap * 0.5
        if value <= 32 and gap > 0:
            conf = clamp((32 - value) / 22 + abs(gap) * 4, 0.2, 0.95)
            return self._make(snap, "BUY", fair, conf,
                              f"RSI {value:.0f}, цена на {abs(gap)*100:.1f}c ниже VWAP")
        if value >= 68 and gap < 0:
            conf = clamp((value - 68) / 22 + abs(gap) * 4, 0.2, 0.95)
            return self._make(snap, "SELL", fair, conf,
                              f"RSI {value:.0f}, цена на {abs(gap)*100:.1f}c выше VWAP")
        return None


class CvdDivergence(Strategy):
    """Дивергенция цены и кумулятивной дельты объёма.

    Цена переписывает минимум, а CVD — нет: продавцы выдыхаются, крупный покупатель
    набирает тихо. Зеркально для максимумов.
    """
    name = "cvd_divergence"
    title = "CVD DIVERGENCE"

    def evaluate(self, snap: Snapshot) -> Signal | None:
        cvd = cvd_series(snap.trades)
        prices = price_buckets(snap.trades)
        if len(cvd) < 8 or len(prices) < 8:
            return None

        half = len(cvd) // 2
        p_first, p_last = prices[:half], prices[half:]
        c_first, c_last = cvd[:half], cvd[half:]
        mid = snap.mid
        vol = realized_vol(snap.closes) or 0.01

        # бычья дивергенция: новый минимум цены без нового минимума CVD
        if min(p_last) < min(p_first) and min(c_last) > min(c_first):
            strength = (min(c_last) - min(c_first)) / (abs(min(c_first)) + 1.0)
            fair = mid + clamp(vol * 3 + strength * 0.02, 0.02, 0.08)
            conf = clamp(0.35 + strength, 0.2, 0.9)
            return self._make(snap, "BUY", fair, conf,
                              "цена ниже, CVD выше — продавец выдохся")

        # медвежья дивергенция
        if max(p_last) > max(p_first) and max(c_last) < max(c_first):
            strength = (max(c_first) - max(c_last)) / (abs(max(c_first)) + 1.0)
            fair = mid - clamp(vol * 3 + strength * 0.02, 0.02, 0.08)
            conf = clamp(0.35 + strength, 0.2, 0.9)
            return self._make(snap, "SELL", fair, conf,
                              "цена выше, CVD ниже — покупатель выдохся")
        return None


class MeanReversion(Strategy):
    """Возврат к среднему по z-score истории цен + поправка на срок до расчёта.

    Чем ближе разрешение рынка, тем меньше веры в возврат: там уже работает информация,
    а не шум, поэтому требуемый z-score растёт.
    """
    name = "mean_reversion"
    title = "MEAN REVERSION"

    def evaluate(self, snap: Snapshot) -> Signal | None:
        closes = snap.closes
        z = zscore(closes, window=48)
        if z is None:
            return None
        hours = snap.market.hours_to_close
        threshold = 1.8 if hours > 72 else (2.3 if hours > 24 else 3.0)
        if abs(z) < threshold:
            return None

        window = closes[-48:]
        mean = statistics.fmean(window)
        mid = snap.mid
        fair = mid + (mean - mid) * 0.5    # ждём половину возврата, не весь
        conf = clamp((abs(z) - threshold) / 2 + 0.3, 0.2, 0.9)
        if z < 0 and fair > mid:
            return self._make(snap, "BUY", fair, conf,
                              f"z={z:.1f}, среднее {mean:.2f} против цены {mid:.2f}")
        if z > 0 and fair < mid:
            return self._make(snap, "SELL", fair, conf,
                              f"z={z:.1f}, среднее {mean:.2f} против цены {mid:.2f}")
        return None


def build_strategies(cfg: Config) -> list[Strategy]:
    return [RsiVwap(cfg), CvdDivergence(cfg), MeanReversion(cfg)]


# --------------------------------------------------------------------------------------
# Риск-менеджмент
# --------------------------------------------------------------------------------------
class RiskManager:
    """Единственное место, где решается «сколько» и «можно ли вообще»."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.blocked_reason: str = ""

    def size_for(self, signal: Signal, equity: float, open_count: int) -> float:
        """Размер входа в USDC. 0 — вход запрещён."""
        cfg = self.cfg
        if open_count >= cfg.max_positions:
            return 0.0
        price = signal.price
        if not (cfg.min_price <= price <= cfg.max_price):
            return 0.0

        # доля Келли на бинарный исход, зажатая до четверти и до риска на сделку
        p_win = clamp(signal.fair if signal.side == "BUY" else 1 - signal.fair, 0.01, 0.99)
        odds = (1 - price) / price if signal.side == "BUY" else price / (1 - price)
        kelly = (p_win * (odds + 1) - 1) / odds if odds > 0 else 0.0
        kelly = clamp(kelly, 0.0, 0.25) * 0.25 * signal.confidence

        budget = min(equity * cfg.risk_per_trade, equity * kelly + 1e-9, cfg.max_position_usd)
        return budget if budget >= 1.0 else 0.0

    def check_portfolio(self, equity: float, peak: float, day_start: float) -> str:
        """Возвращает причину блокировки торговли или пустую строку."""
        cfg = self.cfg
        if peak > 0 and (peak - equity) / peak >= cfg.max_drawdown:
            self.blocked_reason = f"макс. просадка {cfg.max_drawdown*100:.0f}% — торговля стоп"
            return self.blocked_reason
        if day_start > 0 and (day_start - equity) / day_start >= cfg.daily_loss_limit:
            self.blocked_reason = f"дневной лимит {cfg.daily_loss_limit*100:.0f}% — пауза до утра"
            return self.blocked_reason
        self.blocked_reason = ""
        return ""


# --------------------------------------------------------------------------------------
# Исполнение
# --------------------------------------------------------------------------------------
class PaperBroker:
    """Бумажное исполнение по реальному стакану: проходим уровни, платим спред и комиссию."""

    live = False

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def execute(self, side: str, book: Book, usd: float) -> tuple[float, float, float] | None:
        """→ (штук, средняя цена, комиссия). None, если стакан не потянул."""
        levels = book.asks if side == "BUY" else book.bids
        if not levels:
            return None
        tick = self.cfg.slippage_ticks * 0.01
        remaining = usd
        shares = notional = 0.0
        for price, size in levels:
            fill_price = clamp(price + tick if side == "BUY" else price - tick, 0.01, 0.99)
            level_usd = fill_price * size
            take = min(remaining, level_usd)
            if take <= 0:
                break
            qty = take / fill_price
            shares += qty
            notional += qty * fill_price
            remaining -= take
            if remaining <= 0.01:
                break
        if shares <= 0 or remaining > usd * 0.5:   # стакан тонкий — вход отменяем
            return None
        avg = notional / shares
        fee = notional * self.cfg.fee_bps / 10_000.0
        return shares, avg, fee


class ClobBroker(PaperBroker):
    """Боевое исполнение через CLOB. Требует py-clob-client и ключей в .env."""

    live = True

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        try:
            from py_clob_client.client import ClobClient           # type: ignore
            from py_clob_client.clob_types import ApiCreds          # type: ignore
        except ImportError as exc:  # noqa: PERF203
            raise SystemExit(
                "Боевой режим требует py-clob-client:  pip install py-clob-client"
            ) from exc

        key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        if not key:
            raise SystemExit("Нет POLYMARKET_PRIVATE_KEY в .env — боевой режим невозможен")
        creds = None
        api_key = os.environ.get("POLYMARKET_API_KEY", "")
        if api_key:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=os.environ.get("POLYMARKET_API_SECRET", ""),
                api_passphrase=os.environ.get("POLYMARKET_API_PASSPHRASE", ""),
            )
        self.client = ClobClient(
            CLOB_API,
            key=key,
            chain_id=137,
            creds=creds,
            signature_type=env_int("POLYMARKET_SIGNATURE_TYPE", 0) or None,
            funder=os.environ.get("POLYMARKET_FUNDER") or None,
        )
        if creds is None:
            self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def place(self, token_id: str, side: str, price: float, shares: float) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs, OrderType   # type: ignore
        from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore

        args = OrderArgs(
            token_id=token_id,
            price=round(clamp(price, 0.01, 0.99), 3),
            size=round(shares, 2),
            side=BUY if side == "BUY" else SELL,
        )
        signed = self.client.create_order(args)
        return self.client.post_order(signed, OrderType.GTC)


# --------------------------------------------------------------------------------------
# Портфель и хранилище
# --------------------------------------------------------------------------------------
class Store:
    """SQLite: сделки, кривая эквити, состояние. Перезапуск не теряет историю."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fills (
                ts REAL, token_id TEXT, question TEXT, side TEXT,
                size REAL, price REAL, fee REAL, strategy TEXT, pnl REAL
            );
            CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, value REAL);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self.conn.commit()

    def add_fill(self, fill: Fill) -> None:
        self.conn.execute(
            "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?)",
            (fill.ts, fill.token_id, fill.question, fill.side, fill.size,
             fill.price, fill.fee, fill.strategy, fill.pnl),
        )
        self.conn.commit()

    def add_equity(self, value: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity VALUES (?,?)", (round(now(), 1), value)
        )
        self.conn.commit()

    def get_state(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, str(value)))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class Portfolio:
    def __init__(self, cfg: Config, store: Store | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.cash = cfg.bankroll
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.equity_curve: list[float] = [cfg.bankroll]
        self.peak = cfg.bankroll
        self.day_start_equity = cfg.bankroll
        self.day_key = utc_now().strftime("%Y-%m-%d")
        self.per_strategy: dict[str, dict[str, float]] = {}

    # --- учёт -------------------------------------------------------------------------
    @property
    def exposure(self) -> float:
        return sum(p.size * (p.mark or p.entry) for p in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + self.exposure

    @property
    def realized(self) -> float:
        return sum(f.pnl for f in self.fills)

    @property
    def unrealized(self) -> float:
        return sum(p.upnl() for p in self.positions.values())

    def stats_for(self, strategy: str) -> dict[str, float]:
        return self.per_strategy.setdefault(
            strategy, {"pnl": 0.0, "trades": 0.0, "wins": 0.0}
        )

    def open(self, signal: Signal, shares: float, price: float, fee: float,
             token_id: str, outcome: str, fair: float) -> Fill:
        cost = shares * price + fee
        self.cash -= cost
        pos = Position(
            token_id=token_id,
            question=signal.market.question,
            outcome=outcome,
            strategy=signal.strategy,
            size=shares,
            entry=price,
            opened_at=now(),
            fair=fair,
            mark=price,
            condition_id=signal.market.condition_id,
            end_ts=signal.market.end_ts,
        )
        self.positions[pos.token_id] = pos
        fill = Fill(now(), pos.token_id, pos.question, "BUY", shares, price, fee,
                    signal.strategy)
        self.fills.append(fill)
        del self.fills[:-500]
        if self.store:
            self.store.add_fill(fill)
        return fill

    def close(self, token_id: str, price: float, fee: float, reason: str) -> Fill | None:
        pos = self.positions.pop(token_id, None)
        if not pos:
            return None
        proceeds = pos.size * price - fee
        self.cash += proceeds
        pnl = proceeds - pos.cost
        fill = Fill(now(), token_id, pos.question, "SELL", pos.size, price, fee,
                    pos.strategy, pnl)
        self.fills.append(fill)
        del self.fills[:-500]
        stats = self.stats_for(pos.strategy)
        stats["pnl"] += pnl
        stats["trades"] += 1
        stats["wins"] += 1 if pnl > 0 else 0
        if self.store:
            self.store.add_fill(fill)
        log.info("EXIT  %s %s @ %.3f  pnl %s (%s)", pos.strategy,
                 short(pos.question, 40), price, money(pnl), reason)
        return fill

    def mark_to_market(self, marks: dict[str, float]) -> None:
        for token_id, pos in self.positions.items():
            mark = marks.get(token_id)
            if mark:
                pos.mark = mark
        equity = self.equity
        self.equity_curve.append(equity)
        del self.equity_curve[:-2000]
        self.peak = max(self.peak, equity)
        key = utc_now().strftime("%Y-%m-%d")
        if key != self.day_key:
            self.day_key = key
            self.day_start_equity = equity
        if self.store:
            self.store.add_equity(equity)

    # --- метрики ----------------------------------------------------------------------
    @property
    def closed_fills(self) -> list[Fill]:
        return [f for f in self.fills if f.pnl != 0.0]

    @property
    def win_rate(self) -> float:
        closed = self.closed_fills
        if not closed:
            return 0.0
        return sum(1 for f in closed if f.pnl > 0) / len(closed)

    @property
    def profit_factor(self) -> float:
        wins = sum(f.pnl for f in self.closed_fills if f.pnl > 0)
        losses = -sum(f.pnl for f in self.closed_fills if f.pnl < 0)
        if losses <= 0:
            return 99.0 if wins > 0 else 0.0
        return wins / losses

    @property
    def max_drawdown(self) -> float:
        peak = -math.inf
        worst = 0.0
        for value in self.equity_curve:
            peak = max(peak, value)
            if peak > 0:
                worst = max(worst, (peak - value) / peak)
        return worst

    @property
    def sharpe(self) -> float:
        """Грубый Sharpe по тикам эквити — ориентир стабильности, не годовая цифра."""
        curve = self.equity_curve[-500:]
        if len(curve) < 20:
            return 0.0
        rets = [(b - a) / a for a, b in zip(curve, curve[1:]) if a > 0]
        if len(rets) < 10:
            return 0.0
        mean = statistics.fmean(rets)
        try:
            sd = statistics.stdev(rets)
        except statistics.StatisticsError:
            return 0.0
        if sd < 1e-9:
            return 0.0
        return mean / sd * math.sqrt(len(rets))


# --------------------------------------------------------------------------------------
# Дашборд
# --------------------------------------------------------------------------------------
SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float], width: int) -> str:
    if len(values) < 2 or width < 2:
        return ""
    step = max(1, len(values) // width)
    sampled = values[::step][-width:]
    lo, hi = min(sampled), max(sampled)
    span = hi - lo
    if span <= 0:
        return SPARK[0] * len(sampled)
    return "".join(SPARK[int((v - lo) / span * (len(SPARK) - 1))] for v in sampled)


class Dashboard:
    """Полноэкранный ANSI-дашборд. Без curses — просто перерисовка кадра."""

    def __init__(self, engine: "Engine") -> None:
        self.engine = engine
        self.enabled = sys.stdout.isatty()

    def start(self) -> None:
        if self.enabled:
            sys.stdout.write("\x1b[?25l\x1b[2J")   # спрятать курсор, очистить

    def stop(self) -> None:
        if self.enabled:
            sys.stdout.write("\x1b[?25h\x1b[0m\n")
            sys.stdout.flush()

    def render(self) -> None:
        if not self.enabled:
            return
        width = max(shutil.get_terminal_size((120, 40)).columns, 80)
        lines: list[str] = []
        lines += self._header(width)
        lines += self._strategies(width)
        lines += self._positions(width)
        lines += self._executions(width)
        lines += self._equity(width)
        lines += self._footer(width)
        frame = "\x1b[H" + "\n".join(pad(line, width) + "\x1b[K" for line in lines) + "\x1b[J"
        sys.stdout.write(frame)
        sys.stdout.flush()

    # --- блоки ------------------------------------------------------------------------
    def _rule(self, width: int, title: str = "") -> str:
        if not title:
            return C.GREY + "─" * width + C.RESET
        head = f"─── {title} "
        return C.GREY + head + "─" * max(0, width - len(head)) + C.RESET

    def _header(self, width: int) -> list[str]:
        eng = self.engine
        pf = eng.portfolio
        mode = (C.RED + "LIVE" if eng.broker.live else C.BLUE + "PAPER") + C.RESET
        total = pf.equity - eng.cfg.bankroll
        color = C.GREEN if total >= 0 else C.RED
        title = (f"{C.BOLD}POLYBOT{C.RESET} {mode} {C.GREY}│{C.RESET} "
                 f"эквити {C.WHITE}${pf.equity:,.2f}{C.RESET} "
                 f"{color}{money(total)} ({pct(total / max(eng.cfg.bankroll, 1))}){C.RESET}")
        status = f"{C.GREY}{datetime.now().strftime('%H:%M:%S')}  цикл {eng.cycles}{C.RESET}"
        left = pad(title, width - len(strip_ansi(status)) - 1)
        return [left + status, self._rule(width)]

    def _strategies(self, width: int) -> list[str]:
        cols = self.engine.strategies
        col_w = max(24, (width - (len(cols) - 1) * 3) // len(cols))
        blocks: list[list[str]] = []
        for strat in cols:
            stats = self.engine.portfolio.stats_for(strat.name)
            pnl = stats["pnl"]
            color = C.GREEN if pnl >= 0 else C.RED
            head = f"{C.BOLD}{strat.title}{C.RESET}"
            summary = (f"{C.GREY}сигналов{C.RESET} {int(strat.signals)}  "
                       f"{C.GREY}сделок{C.RESET} {int(stats['trades'])}  "
                       f"{color}{money(pnl)}{C.RESET}")
            body = [head, summary]
            for note in strat.notes[-5:][::-1]:
                body.append(C.DIM + short(note, col_w) + C.RESET)
            while len(body) < 7:
                body.append(C.DIM + "—" + C.RESET)
            blocks.append([pad(line, col_w) for line in body[:7]])

        rows = []
        for i in range(7):
            rows.append((C.GREY + " │ " + C.RESET).join(block[i] for block in blocks))
        return rows + [self._rule(width)]

    def _positions(self, width: int) -> list[str]:
        pf = self.engine.portfolio
        rows = [self._rule(width, "ПОЗИЦИИ")]
        if not pf.positions:
            rows.append(C.DIM + "  позиций нет — ждём сигнал" + C.RESET)
            return rows
        q_w = max(20, width - 70)
        rows.append(C.GREY + "  " + pad("РЫНОК", q_w) + pad("ИСХОД", 8) + pad("СТРАТ", 16) +
                    pad("ВХОД", 8) + pad("ЦЕНА", 8) + pad("РАЗМЕР", 10) + "P&L" + C.RESET)
        for pos in sorted(pf.positions.values(), key=lambda p: -abs(p.upnl())):
            upnl = pos.upnl()
            color = C.GREEN if upnl >= 0 else C.RED
            rows.append(
                "  " + pad(short(pos.question, q_w), q_w) +
                pad(C.YELLOW + short(pos.outcome, 7) + C.RESET, 8) +
                pad(C.BLUE + pos.strategy + C.RESET, 16) +
                pad(f"{pos.entry:.3f}", 8) +
                pad(f"{pos.mark:.3f}", 8) +
                pad(f"${pos.cost:,.0f}", 10) +
                color + money(upnl) + C.RESET
            )
        return rows

    def _executions(self, width: int) -> list[str]:
        pf = self.engine.portfolio
        rows = [self._rule(width, "ИСПОЛНЕНИЯ")]
        recent = pf.fills[-8:][::-1]
        if not recent:
            rows.append(C.DIM + "  сделок пока не было" + C.RESET)
            return rows
        q_w = max(20, width - 58)
        for fill in recent:
            side_color = C.GREEN if fill.side == "BUY" else C.RED
            pnl = ""
            if fill.pnl:
                pnl_color = C.GREEN if fill.pnl > 0 else C.RED
                pnl = pnl_color + money(fill.pnl) + C.RESET
            rows.append(
                "  " + C.GREY + datetime.fromtimestamp(fill.ts).strftime("%H:%M:%S") + C.RESET +
                "  " + pad(side_color + fill.side + C.RESET, 6) +
                pad(short(fill.question, q_w), q_w) +
                pad(f"{fill.size:,.0f} × {fill.price:.3f}", 18) + pnl
            )
        return rows

    def _equity(self, width: int) -> list[str]:
        pf = self.engine.portfolio
        rows = [self._rule(width, "КРИВАЯ ЭКВИТИ")]
        curve = pf.equity_curve
        spark = sparkline(curve, width - 4)
        color = C.GREEN if len(curve) > 1 and curve[-1] >= curve[0] else C.RED
        rows.append("  " + color + spark + C.RESET)
        return rows

    def _footer(self, width: int) -> list[str]:
        eng = self.engine
        pf = eng.portfolio
        parts = [
            f"{C.GREY}win rate{C.RESET} {pf.win_rate*100:.0f}%",
            f"{C.GREY}profit factor{C.RESET} {pf.profit_factor:.2f}",
            f"{C.GREY}max DD{C.RESET} {pf.max_drawdown*100:.1f}%",
            f"{C.GREY}sharpe{C.RESET} {pf.sharpe:.2f}",
            f"{C.GREY}экспозиция{C.RESET} ${pf.exposure:,.0f}",
            f"{C.GREY}кэш{C.RESET} ${pf.cash:,.0f}",
            f"{C.GREY}рынков{C.RESET} {len(eng.universe)}",
        ]
        status = eng.status_line()
        return [self._rule(width), "  " + "   ".join(parts),
                "  " + C.DIM + short(status, width - 4) + C.RESET]


# --------------------------------------------------------------------------------------
# Движок
# --------------------------------------------------------------------------------------
class Engine:
    def __init__(self, cfg: Config, http: Http, store: Store | None = None) -> None:
        self.cfg = cfg
        self.http = http
        self.data = MarketData(http, cfg)
        self.strategies = build_strategies(cfg)
        self.risk = RiskManager(cfg)
        self.broker: PaperBroker = ClobBroker(cfg) if cfg.live else PaperBroker(cfg)
        self.portfolio = Portfolio(cfg, store)
        self.store = store
        self.universe: list[Market] = []
        self.snapshots: dict[str, Snapshot] = {}
        self.cycles = 0
        self.last_scan = 0.0
        self.stopping = False
        self.message = "старт"

    def status_line(self) -> str:
        blocked = self.risk.blocked_reason
        if blocked:
            return f"ТОРГОВЛЯ НА ПАУЗЕ: {blocked}"
        err = f"  ошибок сети: {self.http.errors}" if self.http.errors else ""
        return f"{self.message}{err}"

    # --- сканирование -----------------------------------------------------------------
    async def scan(self) -> None:
        self.message = "обновляю вселенную рынков…"
        universe = await self.data.universe()
        if universe:
            self.universe = universe
        if not self.universe:
            self.message = f"нет рынков после фильтров ({self.http.last_error or 'проверь сеть'})"
            return

        sem = asyncio.Semaphore(self.cfg.max_concurrency)

        async def one(market: Market) -> tuple[str, Snapshot | None]:
            async with sem:
                try:
                    return market.token_id, await self.data.snapshot(market)
                except Exception as exc:  # сеть/формат — рынок просто пропускаем
                    log.debug("snapshot %s: %s", market.slug, exc)
                    return market.token_id, None

        results = await asyncio.gather(*(one(m) for m in self.universe))
        self.snapshots = {tid: snap for tid, snap in results if snap}
        self.message = f"снимков рынка: {len(self.snapshots)}"
        self.last_scan = now()

    # --- торговые решения -------------------------------------------------------------
    def held_markets(self) -> set[str]:
        """Рынки, где уже есть позиция, — второй раз в тот же рынок не лезем."""
        return {p.condition_id for p in self.portfolio.positions.values()}

    def signals(self) -> list[Signal]:
        held = self.held_markets()
        out: list[Signal] = []
        for snap in self.snapshots.values():
            if snap.market.condition_id in held:
                continue
            for strat in self.strategies:
                try:
                    signal = strat.evaluate(snap)
                except Exception as exc:
                    log.debug("strategy %s: %s", strat.name, exc)
                    continue
                if signal:
                    out.append(signal)
        out.sort(key=lambda s: s.edge * s.confidence, reverse=True)
        return out

    async def open_positions(self, signals: Iterable[Signal]) -> None:
        pf = self.portfolio
        blocked = self.risk.check_portfolio(pf.equity, pf.peak, pf.day_start_equity)
        if blocked:
            return
        held = self.held_markets()
        for signal in signals:
            if len(pf.positions) >= self.cfg.max_positions:
                return
            market = signal.market
            if market.condition_id in held:
                continue
            usd = self.risk.size_for(signal, pf.equity, len(pf.positions))
            if usd <= 0 or usd > pf.cash:
                continue
            snap = self.snapshots.get(market.token_id)
            if not snap:
                continue

            # Шортов на Polymarket нет: ставка против исхода — это покупка парного токена
            # (продать YES = купить NO по цене 1-p). Поэтому SELL разворачиваем в BUY NO.
            if signal.side == "BUY":
                token_id, outcome, book, fair = (
                    market.token_id, market.outcome, snap.book, signal.fair)
            else:
                if not market.no_token_id:
                    continue
                token_id, outcome, fair = (
                    market.no_token_id, market.no_outcome, 1 - signal.fair)
                book = await self.data.book(token_id)
                if not book.bids or not book.asks or book.spread > self.cfg.max_spread:
                    continue

            result = self.broker.execute("BUY", book, usd)
            if not result:
                continue
            shares, price, fee = result
            if fair - price < self.cfg.min_edge:    # на парном стакане перевес мог пропасть
                continue
            if self.broker.live and isinstance(self.broker, ClobBroker):
                try:
                    self.broker.place(token_id, "BUY", price, shares)
                except Exception as exc:
                    log.error("ордер не прошёл: %s", exc)
                    self.message = f"ордер отклонён: {exc}"
                    continue
            pf.open(signal, shares, price, fee, token_id, outcome, fair)
            held.add(market.condition_id)
            log.info("ENTRY %s %s [%s] @ %.3f  $%.0f  (%s)", signal.strategy,
                     short(market.question, 40), outcome, price, shares * price, signal.reason)

    async def manage_positions(self) -> None:
        """Переоценка открытых позиций и выходы: TP / SL / время / близкий расчёт."""
        pf = self.portfolio
        if not pf.positions:
            pf.mark_to_market({})
            return

        token_ids = list(pf.positions)
        books = await asyncio.gather(*(self.data.book(tid) for tid in token_ids))
        marks: dict[str, float] = {}
        for token_id, book in zip(token_ids, books):
            if book.bids:
                marks[token_id] = book.best_bid
        pf.mark_to_market(marks)

        for token_id, book in zip(token_ids, books):
            pos = pf.positions.get(token_id)
            if not pos or not book.bids:
                continue
            mark = book.best_bid
            move = mark - pos.entry
            reason = ""
            if move >= self.cfg.take_profit:
                reason = "тейк-профит"
            elif move <= -self.cfg.stop_loss:
                reason = "стоп-лосс"
            elif pos.age_min >= self.cfg.time_stop_min:
                reason = "тайм-стоп"
            elif pos.end_ts and (pos.end_ts - now()) / 3600 <= 1.0:
                reason = "рынок закрывается"
            elif self.risk.blocked_reason:
                reason = "риск-стоп портфеля"
            if not reason:
                continue
            result = self.broker.execute("SELL", book, pos.size * mark)
            if not result:
                continue
            _, price, fee = result
            if self.broker.live and isinstance(self.broker, ClobBroker):
                try:
                    self.broker.place(token_id, "SELL", price, pos.size)
                except Exception as exc:
                    log.error("выход не прошёл: %s", exc)
                    continue
            pf.close(token_id, price, fee, reason)

    # --- циклы ------------------------------------------------------------------------
    async def cycle(self) -> None:
        self.cycles += 1
        if now() - self.last_scan >= self.cfg.scan_interval or not self.snapshots:
            await self.scan()
            await self.open_positions(self.signals())
        await self.manage_positions()

    async def run(self, dashboard: Dashboard | None, once: bool = False) -> None:
        if dashboard:
            dashboard.start()
        try:
            while not self.stopping:
                await self.cycle()
                if dashboard:
                    dashboard.render()
                if once:
                    break
                await asyncio.sleep(self.cfg.tick_interval)
        finally:
            if dashboard:
                dashboard.stop()

    def request_stop(self) -> None:
        self.stopping = True
        self.message = "останавливаюсь…"


# --------------------------------------------------------------------------------------
# Бэктест
# --------------------------------------------------------------------------------------
INTERVALS = {"1d": "1d", "7d": "1w", "1w": "1w", "30d": "max", "max": "max"}


async def backtest(cfg: Config, http: Http, period: str) -> None:
    """Прогон стратегий по истории цен.

    Честное предупреждение: истории сделок за прошлое у нас нет, поэтому CVD и VWAP
    в бэктесте не работают — гоняются только те стратегии, которым хватает цен.
    Исполнение считается по цене свечи плюс половина типичного спреда.
    """
    interval = INTERVALS.get(period, "1w")
    data = MarketData(http, cfg)
    markets = await data.universe()
    if not markets:
        print("Не удалось получить рынки — проверь сеть.")
        return

    print(f"Бэктест: {len(markets)} рынков, интервал {interval}, банк ${cfg.bankroll:,.0f}")
    strategies = [s for s in build_strategies(cfg) if s.name == "mean_reversion"]
    equity = cfg.bankroll
    trades: list[float] = []
    half_spread = cfg.max_spread / 2

    for market in markets:
        history = await data.prices(market.token_id, interval=interval, fidelity=10)
        if len(history) < 80:
            continue
        position: tuple[float, float] | None = None    # (цена входа, штук)
        for i in range(60, len(history)):
            window = history[: i + 1]
            price = window[-1][1]
            snap = Snapshot(
                market=market,
                book=Book(bids=[(price - half_spread, 10_000)],
                          asks=[(price + half_spread, 10_000)]),
                prices=window,
            )
            if position is None:
                for strat in strategies:
                    signal = strat.evaluate(snap)
                    if signal and signal.side == "BUY":
                        usd = min(equity * cfg.risk_per_trade, cfg.max_position_usd)
                        entry = price + half_spread
                        position = (entry, usd / entry)
                        break
            else:
                entry, shares = position
                move = (price - half_spread) - entry
                if move >= cfg.take_profit or move <= -cfg.stop_loss:
                    pnl = move * shares
                    equity += pnl
                    trades.append(pnl)
                    position = None

    if not trades:
        print("Сделок не нашлось: на этой истории условия входа не сработали.")
        return
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    gross_loss = -sum(losses)
    print(f"Сделок: {len(trades)}   win rate: {len(wins)/len(trades)*100:.0f}%")
    print(f"P&L: {money(equity - cfg.bankroll)}   итог: ${equity:,.2f}")
    print(f"Средняя прибыль: {money(statistics.fmean(wins)) if wins else '$0'}   "
          f"средний убыток: {money(statistics.fmean(losses)) if losses else '$0'}")
    if gross_loss > 0:
        print(f"Profit factor: {sum(wins)/gross_loss:.2f}")
    print("\nЭто оптимистичная оценка: нет проскальзывания на тонком стакане, "
          "нет очереди в книге и нет неудачных дней рынка. Реальный результат будет хуже.")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def setup_logging(quiet: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(DATA_DIR / "polybot.log",
                                                          encoding="utf-8")]
    if not quiet:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main_async(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    cfg.live = args.live
    if args.bankroll:
        cfg.bankroll = args.bankroll
        cfg.max_position_usd = min(cfg.max_position_usd, cfg.bankroll * 0.15)

    use_ui = not args.no_ui and not args.backtest and sys.stdout.isatty()
    # с дашбордом логи уходят только в файл, иначе они рвут кадр
    setup_logging(quiet=use_ui)

    async with Http(cfg.max_concurrency) as http:
        if args.backtest:
            await backtest(cfg, http, args.backtest)
            return 0

        store = Store(DB_PATH)
        engine = Engine(cfg, http, store)
        if cfg.live:
            log.warning("БОЕВОЙ РЕЖИМ: ордера уходят на биржу, банк %s", f"${cfg.bankroll:,.0f}")

        dashboard = Dashboard(engine) if use_ui else None

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, engine.request_stop)
            except (NotImplementedError, RuntimeError):
                pass

        try:
            await engine.run(dashboard, once=args.once)
        except KeyboardInterrupt:
            engine.request_stop()
        finally:
            pf = engine.portfolio
            store.close()
            print(f"\nИтог: эквити ${pf.equity:,.2f} "
                  f"({money(pf.equity - cfg.bankroll)}), "
                  f"сделок {len(pf.closed_fills)}, win rate {pf.win_rate*100:.0f}%")
        return 0


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(description="Polybot — торговый бот для Polymarket")
    parser.add_argument("--live", action="store_true",
                        help="боевые ордера через CLOB (нужны ключи в .env)")
    parser.add_argument("--once", action="store_true", help="один цикл и выход")
    parser.add_argument("--no-ui", action="store_true", help="без дашборда, только логи")
    parser.add_argument("--backtest", metavar="ПЕРИОД", nargs="?", const="7d",
                        help="прогон по истории: 1d, 7d, 30d, max")
    parser.add_argument("--bankroll", type=float, help="переопределить банк в USDC")
    args = parser.parse_args()

    if args.live:
        confirm = os.environ.get("POLYBOT_CONFIRM_LIVE", "")
        if confirm != "yes":
            print("Боевой режим выключен предохранителем.\n"
                  "Поставь POLYBOT_CONFIRM_LIVE=yes в .env, если действительно готов "
                  "торговать реальными деньгами.")
            return 1
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
