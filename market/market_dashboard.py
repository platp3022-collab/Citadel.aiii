#!/usr/bin/env python3
"""Market Dashboard & Trading Plan — институциональный дневной бриф.

Тянет реальные котировки (Yahoo Finance, запасной источник — Stooq), считает
мультитаймфреймовую техничку (20 EMA / 50 SMA / 200 SMA, RSI, ATR, RVOL),
оценивает качество сетапа по прозрачному рубрикатору и печатает бриф
в формате торгового деска.

Инструмент фильтрации информации, не финансовый совет.

Запуск:
    python market_dashboard.py                     # тикеры из watchlist.txt
    python market_dashboard.py AAPL NVDA BTC-USD   # тикеры из аргументов
    python market_dashboard.py --offline           # только из кеша, без сети
    python market_dashboard.py --self-test         # проверка математики
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("dashboard")

# ════════════════════════════════════════════════════════════════════════════
#  КОНФИГ — правь прямо здесь
# ════════════════════════════════════════════════════════════════════════════

CONFIG: dict[str, Any] = {
    "data": {
        "history_days": 760,        # ~3 года: хватает на 200 SMA + недельный ТФ
        "cache_hours": 6,           # свежесть кеша, дальше — перезапрос
        "workers": 6,               # параллельных загрузок
        "retries": 3,
        "timeout_seconds": 25,
    },
    "benchmarks": {
        # Рыночный контекст для секции макро. Пустой список — секция короче.
        "SPY": "S&P 500",
        "QQQ": "Nasdaq 100",
        "^VIX": "Волатильность",
        "DX-Y.NYB": "Индекс доллара",
        "^TNX": "10Y доходность",
    },
    "indicators": {
        "ema_fast": 20,
        "sma_mid": 50,
        "sma_slow": 200,
        "rsi_period": 14,
        "atr_period": 14,
        "rvol_lookback": 20,        # средний объём за N сессий
        "breakout_lookback": 20,    # окно для локальных хай/лоу
        "weekly_ema": 10,           # ≈ 50 дней
        "weekly_sma": 30,           # ≈ 150 дней
    },
    "scoring": {
        "tradable_min": 6.0,        # ниже — в секцию AVOID
        "plan_setups": 3,           # сколько сетапов расписывать поштучно
        "extended_atr": 2.5,        # отрыв от 20 EMA в ATR = перерастянут
        "chop_range_atr": 3.0,      # ширина диапазона в ATR ниже этой = пила
        "min_rvol": 0.6,            # ниже — нет участия
        "earnings_blackout_days": 2,  # бинарное событие на горизонте
    },
    "execution": {
        "stop_atr_buffer": 0.25,    # запас за структурный уровень, в ATR
        "max_stop_atr": 2.0,        # шире — сетап не берём
        "t1_atr": 1.5,
        "t2_atr": 3.0,
        "min_reward_risk": 1.5,     # ниже — сетап не проходит в план
        "confirm_rvol": 1.5,        # объём подтверждения пробоя
    },
    "chart": {
        "history": 760,             # сессий отдаётся в браузер (можно отлистать назад)
        "view_bars": 130,           # сессий видно при открытии (~полгода)
        "compare_max": 6,           # линий на графике относительной динамики
    },
    "paths": {
        "watchlist": "watchlist.txt",
        "events": "events.json",
        "cache": "data/cache",
        "briefs": "briefs",
    },
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; market-dashboard/1.0; +research)"}


def cfg(path: str, default: Any = None) -> Any:
    node: Any = CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# ════════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ════════════════════════════════════════════════════════════════════════════

def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        f = float(value)
        return default if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return default


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def fmt_price(v: float) -> str:
    if v == 0:
        return "n/a"
    if v >= 1000:
        return f"${v:,.2f}"
    if v >= 1:
        return f"${v:.2f}"
    return f"${v:.4f}"


def fmt_level(v: float) -> str:
    """Уровень без знака доллара — для стопов, целей, MA."""
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"


def fmt_pct(v: float, sign: bool = True) -> str:
    return f"{v:+.2f}%" if sign else f"{v:.2f}%"


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ════════════════════════════════════════════════════════════════════════════
#  ИНДИКАТОРЫ
# ════════════════════════════════════════════════════════════════════════════

def sma(values: Sequence[float], n: int) -> list[float | None]:
    if n <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    running = sum(values[:n])
    out[n - 1] = running / n
    for i in range(n, len(values)):
        running += values[i] - values[i - n]
        out[i] = running / n
    return out


def ema(values: Sequence[float], n: int) -> list[float | None]:
    """EMA с посевом от SMA первых n значений — стандарт большинства платформ."""
    if n <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1.0)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], n: int = 14) -> list[float | None]:
    """RSI по Уайлдеру (сглаживание 1/n)."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / n
    avg_loss = losses / n
    out[n] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(n + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(delta, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-delta, 0.0)) / n
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        n: int = 14) -> list[float | None]:
    """ATR по Уайлдеру."""
    size = len(closes)
    out: list[float | None] = [None] * size
    if size <= n:
        return out
    trs: list[float] = [highs[0] - lows[0]]
    for i in range(1, size):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    prev = sum(trs[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, size):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


# ════════════════════════════════════════════════════════════════════════════
#  ЗАГРУЗКА ДАННЫХ
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Bars:
    """Дневные свечи, отсортированы по возрастанию даты."""
    symbol: str
    dates: list[date] = field(default_factory=list)
    opens: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    source: str = ""
    currency: str = "USD"

    def __len__(self) -> int:
        return len(self.closes)


class DataError(RuntimeError):
    pass


def _http_get(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_with_retry(url: str) -> bytes:
    retries = int(cfg("data.retries", 3))
    timeout = int(cfg("data.timeout_seconds", 25))
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return _http_get(url, timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403, 404):     # не ретраим отказ по существу
                break
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise DataError(f"{urllib.parse.urlsplit(url).netloc} недоступен ({last})")


def fetch_yahoo(symbol: str) -> Bars:
    days = int(cfg("data.history_days", 760))
    end = int(time.time())
    start = end - days * 86400
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?period1={start}&period2={end}"
           "&interval=1d&events=div%2Csplit")
    payload = json.loads(_fetch_with_retry(url).decode("utf-8", "replace"))
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        err = ((payload.get("chart") or {}).get("error") or {}).get("description")
        raise DataError(f"{symbol}: Yahoo не вернул данные ({err or 'пустой ответ'})")
    node = result[0]
    stamps = node.get("timestamp") or []
    quote = ((node.get("indicators") or {}).get("quote") or [{}])[0]
    meta = node.get("meta") or {}
    bars = Bars(symbol=symbol, source="yahoo", currency=meta.get("currency") or "USD")
    for i, ts in enumerate(stamps):
        o, h, l, c = (quote.get("open"), quote.get("high"),
                      quote.get("low"), quote.get("close"))
        close = (c or [])[i] if c and i < len(c) else None
        if close is None:                      # дырка в ряду — свечу пропускаем
            continue
        bars.dates.append(datetime.fromtimestamp(ts, timezone.utc).date())
        bars.closes.append(float(close))
        bars.opens.append(num((o or [])[i] if o and i < len(o) else close, float(close)))
        bars.highs.append(num((h or [])[i] if h and i < len(h) else close, float(close)))
        bars.lows.append(num((l or [])[i] if l and i < len(l) else close, float(close)))
        vol = quote.get("volume")
        bars.volumes.append(num((vol or [])[i] if vol and i < len(vol) else 0.0))
    if not bars.closes:
        raise DataError(f"{symbol}: Yahoo вернул пустой ряд")
    return bars


_STOOQ_INDEX = {"^GSPC": "^spx", "^IXIC": "^ndq", "^DJI": "^dji", "^VIX": "^vix"}


def _stooq_symbol(symbol: str) -> str:
    if symbol in _STOOQ_INDEX:
        return _STOOQ_INDEX[symbol]
    if symbol.endswith("-USD"):                # крипта: BTC-USD → btcusd
        return symbol[:-4].lower() + "usd"
    if symbol.startswith("^") or "." in symbol or "=" in symbol:
        raise DataError(f"{symbol}: у Stooq нет такого символа")
    return symbol.lower() + ".us"


def fetch_stooq(symbol: str) -> Bars:
    """Запасной источник: CSV без ключа. Индексы/форекс покрывает частично."""
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(_stooq_symbol(symbol))}&i=d"
    text = _fetch_with_retry(url).decode("utf-8", "replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Close" not in (rows[0] or {}):
        raise DataError(f"{symbol}: Stooq вернул пустой CSV")
    bars = Bars(symbol=symbol, source="stooq")
    for row in rows:
        try:
            d = datetime.strptime(row["Date"], "%Y-%m-%d").date()
            close = float(row["Close"])
        except (KeyError, ValueError, TypeError):
            continue
        bars.dates.append(d)
        bars.opens.append(num(row.get("Open"), close))
        bars.highs.append(num(row.get("High"), close))
        bars.lows.append(num(row.get("Low"), close))
        bars.closes.append(close)
        bars.volumes.append(num(row.get("Volume")))
    if not bars.closes:
        raise DataError(f"{symbol}: Stooq не дал ни одной свечи")
    cutoff = date.today() - timedelta(days=int(cfg("data.history_days", 760)))
    keep = [i for i, d in enumerate(bars.dates) if d >= cutoff]
    if keep:
        lo = keep[0]
        for attr in ("dates", "opens", "highs", "lows", "closes", "volumes"):
            setattr(bars, attr, getattr(bars, attr)[lo:])
    return bars


def _cache_path(symbol: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in symbol)
    return ROOT / str(cfg("paths.cache", "data/cache")) / f"{safe}.json"


def _cache_write(bars: Bars) -> None:
    path = _cache_path(bars.symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": bars.symbol,
        "source": bars.source,
        "currency": bars.currency,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "dates": [d.isoformat() for d in bars.dates],
        "opens": bars.opens, "highs": bars.highs, "lows": bars.lows,
        "closes": bars.closes, "volumes": bars.volumes,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _cache_read(symbol: str, max_age_hours: float | None) -> Bars | None:
    path = _cache_path(symbol)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        saved = datetime.fromisoformat(payload["saved_at"])
    except (ValueError, KeyError, OSError):
        return None
    if max_age_hours is not None:
        age = (datetime.now(timezone.utc) - saved).total_seconds() / 3600.0
        if age > max_age_hours:
            return None
    bars = Bars(symbol=payload["symbol"], source=payload.get("source", "cache") + "/cache",
                currency=payload.get("currency", "USD"))
    bars.dates = [date.fromisoformat(s) for s in payload["dates"]]
    for attr in ("opens", "highs", "lows", "closes", "volumes"):
        setattr(bars, attr, [float(x) for x in payload[attr]])
    return bars


def load_bars(symbol: str, offline: bool = False, no_cache: bool = False) -> Bars:
    """Кеш → Yahoo → Stooq → устаревший кеш. Бросает DataError, если всё мимо."""
    if not no_cache:
        cached = _cache_read(symbol, None if offline else num(cfg("data.cache_hours"), 6))
        if cached:
            return cached
    if offline:
        raise DataError(f"{symbol}: нет кеша, а сеть отключена (--offline)")
    errors: list[str] = []
    for provider in (fetch_yahoo, fetch_stooq):
        try:
            bars = provider(symbol)
            _cache_write(bars)
            return bars
        except DataError as e:
            errors.append(str(e))
        except Exception as e:                 # чужой источник, форма ответа не гарантирована
            errors.append(f"{provider.__name__}: {type(e).__name__}: {e}")
    stale = _cache_read(symbol, None)
    if stale:
        log.warning("%s: источники недоступны, беру устаревший кеш", symbol)
        return stale
    raise DataError(f"{symbol}: " + " | ".join(errors))


def load_many(symbols: Iterable[str], offline: bool = False,
              no_cache: bool = False) -> tuple[dict[str, Bars], dict[str, str]]:
    symbols = list(dict.fromkeys(symbols))
    bars: dict[str, Bars] = {}
    failed: dict[str, str] = {}
    workers = max(1, min(int(cfg("data.workers", 6)), len(symbols) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(load_bars, s, offline, no_cache): s for s in symbols}
        for future, symbol in futures.items():
            try:
                bars[symbol] = future.result()
            except DataError as e:
                failed[symbol] = str(e)
                log.warning("%s", e)
    return bars, failed


# ════════════════════════════════════════════════════════════════════════════
#  НЕДЕЛЬНЫЙ ТАЙМФРЕЙМ
# ════════════════════════════════════════════════════════════════════════════

def to_weekly(bars: Bars) -> Bars:
    """Свёртка дневных свечей в недельные (по ISO-неделе)."""
    out = Bars(symbol=bars.symbol, source=bars.source, currency=bars.currency)
    key: tuple[int, int] | None = None
    for i, d in enumerate(bars.dates):
        iso = d.isocalendar()
        week = (iso[0], iso[1])
        if week != key:
            key = week
            out.dates.append(d)
            out.opens.append(bars.opens[i])
            out.highs.append(bars.highs[i])
            out.lows.append(bars.lows[i])
            out.closes.append(bars.closes[i])
            out.volumes.append(bars.volumes[i])
        else:
            out.dates[-1] = d
            out.highs[-1] = max(out.highs[-1], bars.highs[i])
            out.lows[-1] = min(out.lows[-1], bars.lows[i])
            out.closes[-1] = bars.closes[i]
            out.volumes[-1] += bars.volumes[i]
    return out


# ════════════════════════════════════════════════════════════════════════════
#  СНИМОК ТИКЕРА
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Snapshot:
    symbol: str
    as_of: date
    source: str
    close: float
    prev_close: float
    change_pct: float
    day_high: float
    day_low: float
    ema20: float | None
    sma50: float | None
    sma200: float | None
    rsi14: float | None
    atr14: float | None
    rvol: float | None
    high_20d: float
    low_20d: float
    high_52w: float
    low_52w: float
    trend_d: str
    trend_w: str
    trend_d_score: int
    trend_w_score: int
    ext_atr: float | None        # отрыв от 20 EMA в ATR (знаковый)
    range_atr: float | None      # ширина 20-дневного диапазона в ATR
    bars: int
    spark: list[float] = field(default_factory=list)   # хвост закрытий для спарклайна
    # Хвост истории для графиков: свечи и выровненные по ним ряды средних.
    hist: list[tuple[date, float, float, float, float, float]] = field(default_factory=list)
    ma_hist: dict[str, list[float | None]] = field(default_factory=dict)

    @property
    def trend_pair(self) -> str:
        return f"{self.trend_d} / {self.trend_w}"

    @property
    def atr_pct(self) -> float:
        return safe_div((self.atr14 or 0.0) * 100.0, self.close)


def _last(series: Sequence[float | None]) -> float | None:
    return series[-1] if series and series[-1] is not None else None


def _trend_label(score: int) -> str:
    if score >= 2:
        return "Bullish"
    if score == 1:
        return "Constructive"
    if score == 0:
        return "Neutral"
    if score == -1:
        return "Weak"
    return "Bearish"


def _daily_trend_score(close: float, e20: float | None, s50: float | None,
                       s200: float | None) -> int:
    """От -3 (полный медвежий стек) до +3 (полный бычий)."""
    score = 0
    if e20 is not None:
        score += 1 if close > e20 else -1
    if s50 is not None:
        score += 1 if close > s50 else -1
    if s200 is not None:
        score += 1 if close > s200 else -1
    if None not in (e20, s50, s200):
        if e20 > s50 > s200:                   # type: ignore[operator]
            score = min(3, score + 1)
        elif e20 < s50 < s200:                 # type: ignore[operator]
            score = max(-3, score - 1)
    return clamp_int(score, -3, 3)


def clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def build_snapshot(bars: Bars) -> Snapshot:
    ind = cfg("indicators", {})
    closes, highs, lows = bars.closes, bars.highs, bars.lows
    n = len(closes)
    if n < 2:
        raise DataError(f"{bars.symbol}: слишком короткий ряд ({n} свечей)")

    e20_s = ema(closes, int(ind["ema_fast"]))
    s50_s = sma(closes, int(ind["sma_mid"]))
    s200_s = sma(closes, int(ind["sma_slow"]))
    e20, s50, s200 = _last(e20_s), _last(s50_s), _last(s200_s)
    r14 = _last(rsi(closes, int(ind["rsi_period"])))
    a14 = _last(atr(highs, lows, closes, int(ind["atr_period"])))

    look = int(ind["rvol_lookback"])
    prior_vol = bars.volumes[-look - 1:-1]
    avg_vol = safe_div(sum(prior_vol), len(prior_vol))
    rvol = safe_div(bars.volumes[-1], avg_vol) if avg_vol > 0 else None

    bk = int(ind["breakout_lookback"])
    window_hi = highs[-bk - 1:-1] or highs[-1:]
    window_lo = lows[-bk - 1:-1] or lows[-1:]
    high_20d, low_20d = max(window_hi), min(window_lo)

    yr = closes[-252:] if n >= 252 else closes
    yr_h = highs[-252:] if n >= 252 else highs
    yr_l = lows[-252:] if n >= 252 else lows

    weekly = to_weekly(bars)
    w_close = weekly.closes[-1] if weekly.closes else closes[-1]
    w_ema = _last(ema(weekly.closes, int(ind["weekly_ema"])))
    w_sma = _last(sma(weekly.closes, int(ind["weekly_sma"])))
    w_score = 0
    if w_ema is not None:
        w_score += 1 if w_close > w_ema else -1
    if w_sma is not None:
        w_score += 1 if w_close > w_sma else -1
    if w_ema is not None and w_sma is not None and w_ema > w_sma:
        w_score = min(3, w_score + 1)
    elif w_ema is not None and w_sma is not None and w_ema < w_sma:
        w_score = max(-3, w_score - 1)

    d_score = _daily_trend_score(closes[-1], e20, s50, s200)
    cb = int(cfg("chart.history", 760))
    prev_close = closes[-2]
    ext = safe_div(closes[-1] - e20, a14) if (e20 is not None and a14) else None
    rng = safe_div(high_20d - low_20d, a14) if a14 else None

    return Snapshot(
        symbol=bars.symbol, as_of=bars.dates[-1], source=bars.source,
        close=closes[-1], prev_close=prev_close,
        change_pct=safe_div((closes[-1] - prev_close) * 100.0, prev_close),
        day_high=highs[-1], day_low=lows[-1],
        ema20=e20, sma50=s50, sma200=s200, rsi14=r14, atr14=a14, rvol=rvol,
        high_20d=high_20d, low_20d=low_20d,
        high_52w=max(yr_h) if yr_h else max(yr), low_52w=min(yr_l) if yr_l else min(yr),
        trend_d=_trend_label(d_score), trend_w=_trend_label(w_score),
        trend_d_score=d_score, trend_w_score=w_score,
        ext_atr=ext, range_atr=rng, bars=n,
        spark=list(closes[-60:]),
        hist=list(zip(bars.dates[-cb:], bars.opens[-cb:], highs[-cb:],
                      lows[-cb:], closes[-cb:], bars.volumes[-cb:])),
        ma_hist={"ema20": e20_s[-cb:], "sma50": s50_s[-cb:], "sma200": s200_s[-cb:]},
    )


# ════════════════════════════════════════════════════════════════════════════
#  СОБЫТИЯ И КАТАЛИЗАТОРЫ  (events.json — заполняешь сам)
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Events:
    macro: list[dict] = field(default_factory=list)
    tickers: dict[str, dict] = field(default_factory=dict)
    loaded_from: str = ""

    def for_ticker(self, symbol: str) -> dict:
        return self.tickers.get(symbol.upper(), {})


def load_events(path: Path | None = None) -> Events:
    path = path or (ROOT / str(cfg("paths.events", "events.json")))
    if not path.exists():
        return Events()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log.warning("events.json не прочитан: %s", e)
        return Events()
    tickers = {str(k).upper(): v for k, v in (payload.get("tickers") or {}).items()
               if isinstance(v, dict)}
    macro = [m for m in (payload.get("macro") or []) if isinstance(m, dict)]
    return Events(macro=macro, tickers=tickers, loaded_from=str(path))


def days_to_earnings(entry: dict, today: date) -> int | None:
    raw = entry.get("earnings")
    if not raw:
        return None
    try:
        when = datetime.strptime(str(raw), "%Y-%m-%d").date()
    except ValueError:
        return None
    return (when - today).days


# ════════════════════════════════════════════════════════════════════════════
#  ОЦЕНКА СЕТАПА
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Assessment:
    snap: Snapshot
    score: float
    direction: str               # long / short / none
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    catalysts: list[str] = field(default_factory=list)
    binary_event: str = ""

    @property
    def tradable(self) -> bool:
        return (self.direction in ("long", "short")
                and self.score >= num(cfg("scoring.tradable_min"), 6.0)
                and not self.binary_event)


def assess(snap: Snapshot, entry: dict, today: date) -> Assessment:
    sc = cfg("scoring", {})
    reasons: list[str] = []
    warnings: list[str] = []
    catalysts: list[str] = []

    for note in (entry.get("notes") or []):
        if isinstance(note, str) and note.strip():
            catalysts.append(note.strip())

    # ── направление от согласованности таймфреймов ──────────────────────────
    if snap.trend_d_score >= 1 and snap.trend_w_score >= 1:
        direction = "long"
    elif snap.trend_d_score <= -1 and snap.trend_w_score <= -1:
        direction = "short"
    elif snap.trend_d_score >= 2 and snap.trend_w_score >= 0:
        direction = "long"
    elif snap.trend_d_score <= -2 and snap.trend_w_score <= 0:
        direction = "short"
    else:
        direction = "none"
        warnings.append(f"1D и 1W не согласованы ({snap.trend_pair}) — нет направленного edge")

    score = 5.0
    bull = direction == "long"

    # ── тренд ──────────────────────────────────────────────────────────────
    aligned = abs(snap.trend_d_score) + abs(snap.trend_w_score)
    if direction != "none":
        score += clamp(aligned * 0.35, 0.0, 2.0)
        reasons.append(f"тренд {snap.trend_pair}")
    if snap.sma200 is not None:
        above = snap.close > snap.sma200
        score += 0.5 if above == bull and direction != "none" else -0.5
        reasons.append(("выше" if above else "ниже") + f" 200 SMA {fmt_level(snap.sma200)}")

    # ── импульс ────────────────────────────────────────────────────────────
    if snap.rsi14 is not None:
        r = snap.rsi14
        healthy = (50 <= r <= 70) if bull else (30 <= r <= 50)
        stretched = (r > 78) if bull else (r < 22)
        if healthy:
            score += 1.0
            reasons.append(f"RSI {r:.0f} в рабочей зоне")
        elif stretched:
            score -= 0.6
            warnings.append(f"RSI {r:.0f} — импульс перегрет, вход по рынку без edge")
        elif direction != "none":
            score -= 0.4
            reasons.append(f"RSI {r:.0f} против направления")

    # ── участие (объём) ────────────────────────────────────────────────────
    if snap.rvol is not None:
        v = snap.rvol
        if v >= 2.0:
            score += 1.2
            reasons.append(f"RVOL {v:.2f} — институциональное участие")
        elif v >= 1.5:
            score += 0.8
            reasons.append(f"RVOL {v:.2f}")
        elif v >= 1.0:
            score += 0.3
            reasons.append(f"RVOL {v:.2f}")
        elif v < num(sc.get("min_rvol"), 0.6):
            score -= 1.0
            warnings.append(f"RVOL {v:.2f} — участия нет, движение не подтверждается объёмом")

    # ── растянутость от 20 EMA ─────────────────────────────────────────────
    limit = num(sc.get("extended_atr"), 2.5)
    if snap.ext_atr is not None:
        signed = snap.ext_atr if bull else -snap.ext_atr
        if signed > limit:
            score -= 2.0
            warnings.append(
                f"растянут на {abs(snap.ext_atr):.1f} ATR от 20 EMA — риск возврата к средней")
        elif signed > limit * 0.7:
            score -= 0.8
            warnings.append(f"отрыв {abs(snap.ext_atr):.1f} ATR от 20 EMA — вход поздний")
        elif 0 <= signed <= 1.0:
            score += 0.8
            reasons.append(f"цена в {abs(snap.ext_atr):.1f} ATR от 20 EMA — риск контролируем")

    # ── пила ───────────────────────────────────────────────────────────────
    chop = num(sc.get("chop_range_atr"), 3.0)
    if snap.range_atr is not None and snap.range_atr < chop:
        score -= 1.0
        warnings.append(
            f"20-дневный диапазон всего {snap.range_atr:.1f} ATR — сжатие/пила, ложные пробои")

    # ── близость к триггеру ────────────────────────────────────────────────
    if snap.atr14:
        if bull:
            gap = safe_div(snap.high_20d - snap.close, snap.atr14)
            if 0 <= gap <= 0.5:
                score += 1.0
                reasons.append(f"в {gap:.1f} ATR от 20-дневного максимума {fmt_level(snap.high_20d)}")
        elif direction == "short":
            gap = safe_div(snap.close - snap.low_20d, snap.atr14)
            if 0 <= gap <= 0.5:
                score += 1.0
                reasons.append(f"в {gap:.1f} ATR от 20-дневного минимума {fmt_level(snap.low_20d)}")

    # ── катализаторы и бинарный риск ───────────────────────────────────────
    binary = ""
    dte = days_to_earnings(entry, today)
    if dte is not None:
        blackout = int(num(sc.get("earnings_blackout_days"), 2))
        if -1 <= dte <= blackout:
            binary = (f"отчётность {entry['earnings']}"
                      + (" (сегодня/уже вышла)" if dte <= 0 else f" через {dte} дн."))
            score -= 1.5
            warnings.append(f"бинарное событие: {binary} — гэп-риск не хеджируется стопом")
        else:
            catalysts.append(f"отчётность {entry['earnings']}")
    bias = str(entry.get("bias", "")).lower()
    if bias in ("bullish", "bearish"):
        if (bias == "bullish") == bull and direction != "none":
            score += 0.8
            reasons.append(f"новостной фон {bias}")
        else:
            score -= 0.8
            warnings.append(f"новостной фон {bias} против технической картины")

    if direction == "none":
        score = min(score, 4.5)

    return Assessment(snap=snap, score=round(clamp(score, 1.0, 10.0), 1),
                      direction=direction, reasons=reasons, warnings=warnings,
                      catalysts=catalysts, binary_event=binary)


# ════════════════════════════════════════════════════════════════════════════
#  ПЛАН ИСПОЛНЕНИЯ
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Plan:
    symbol: str
    name: str
    direction: str
    trigger: str
    confirmation: str
    stop: str
    targets: str
    reward_risk: float
    # Числовые уровни — для графики и JSON. Текст выше повторяет их словами.
    last: float = 0.0
    trigger_level: float = 0.0
    stop_level: float = 0.0
    t1: float = 0.0
    t2: float = 0.0
    t1_name: str = ""
    t2_name: str = ""


def _target_levels(trigger: float, atr_v: float, structure: Sequence[tuple[float, str]],
                   long: bool) -> tuple[float, str, float, str]:
    """Цели по ближайшей структуре, с откатом на ATR-проекцию, если структуры нет."""
    ex = cfg("execution", {})
    t1_atr = num(ex.get("t1_atr"), 1.5)
    t2_atr = num(ex.get("t2_atr"), 3.0)
    sign = 1.0 if long else -1.0

    def beyond(level: float, ref: float, gap: float) -> bool:
        return (level - ref) * sign >= gap

    cands = sorted(((lvl, name) for lvl, name in structure if beyond(lvl, trigger, 0.5 * atr_v)),
                   key=lambda x: x[0], reverse=not long)

    t1_hit = next((c for c in cands if beyond(c[0], trigger, 1.0 * atr_v)), None)
    if t1_hit:
        t1, t1_name = t1_hit
    else:
        t1, t1_name = trigger + sign * t1_atr * atr_v, f"{t1_atr:.1f} ATR"

    t2_hit = next((c for c in cands if beyond(c[0], t1, 0.5 * atr_v)), None)
    if t2_hit:
        t2, t2_name = t2_hit
    else:
        t2 = trigger + sign * t2_atr * atr_v
        t2_name = f"{t2_atr:.1f} ATR"
        if not beyond(t2, t1, 0.5 * atr_v):        # структурная T1 уже дальше проекции
            t2, t2_name = t1 + sign * t1_atr * atr_v, f"проекция +{t1_atr:.1f} ATR от T1"
    return t1, t1_name, t2, t2_name


def build_plan(a: Assessment) -> Plan | None:
    """Уровни считаются от реальной структуры: 20 EMA, экстремумы, ATR."""
    s = a.snap
    ex = cfg("execution", {})
    if a.direction not in ("long", "short") or not s.atr14:
        return None
    atr_v = s.atr14
    buf = num(ex.get("stop_atr_buffer"), 0.25) * atr_v
    confirm_rvol = num(ex.get("confirm_rvol"), 1.5)

    if a.direction == "long":
        levels = sorted(l for l in (s.day_high, s.high_20d, s.high_52w) if l > s.close)
        trigger = levels[0] if levels else s.day_high
        name = ("Breakout continuation" if trigger >= s.high_20d
                else "Pullback reclaim")
        structure = max(s.ema20 or 0.0, s.low_20d) if s.ema20 else s.low_20d
        structure = min(structure, trigger - 0.5 * atr_v)   # стоп не вплотную к триггеру
        stop = structure - buf
        risk = trigger - stop
        if risk <= 0:
            return None
        if risk > num(ex.get("max_stop_atr"), 2.0) * atr_v:
            stop = trigger - num(ex.get("max_stop_atr"), 2.0) * atr_v
            risk = trigger - stop
        t1, t1_name, t2, t2_name = _target_levels(
            trigger, atr_v,
            [(s.high_20d, "20-дневный максимум"), (s.high_52w, "годовой максимум")], True)
        trigger_txt = (f"пробой и удержание {fmt_level(trigger)} "
                       f"(≥ {fmt_level(trigger + 0.1 * atr_v)} на закрытии часа)")
        stop_txt = (f"{fmt_level(stop)} — под {'20 EMA' if s.ema20 and structure >= s.ema20 - buf else 'структурным минимумом'}"
                    f"; закрытие дня ниже {fmt_level(stop)} убивает тезис")
    else:
        levels = sorted((l for l in (s.day_low, s.low_20d, s.low_52w) if l < s.close),
                        reverse=True)
        trigger = levels[0] if levels else s.day_low
        name = ("Breakdown continuation" if trigger <= s.low_20d
                else "Failed bounce / lower high")
        structure = min(s.ema20 or float("inf"), s.high_20d) if s.ema20 else s.high_20d
        structure = max(structure, trigger + 0.5 * atr_v)
        stop = structure + buf
        risk = stop - trigger
        if risk <= 0:
            return None
        if risk > num(ex.get("max_stop_atr"), 2.0) * atr_v:
            stop = trigger + num(ex.get("max_stop_atr"), 2.0) * atr_v
            risk = stop - trigger
        t1, t1_name, t2, t2_name = _target_levels(
            trigger, atr_v,
            [(s.low_20d, "20-дневный минимум"), (s.low_52w, "годовой минимум")], False)
        trigger_txt = (f"пробой вниз и удержание под {fmt_level(trigger)} "
                       f"(≤ {fmt_level(trigger - 0.1 * atr_v)} на закрытии часа)")
        stop_txt = (f"{fmt_level(stop)} — над {'20 EMA' if s.ema20 and structure <= s.ema20 + buf else 'структурным максимумом'}"
                    f"; закрытие дня выше {fmt_level(stop)} убивает тезис")

    reward = abs(t1 - trigger)
    rr = safe_div(reward, abs(risk))
    confirmation = (f"RVOL ≥ {confirm_rvol:.1f} на пробойной свече, закрытие бара "
                    f"{'выше' if a.direction == 'long' else 'ниже'} уровня; "
                    f"без объёма пробой считаем ложным и ждём ретест "
                    f"{fmt_level(trigger)} как поддержки"
                    if a.direction == "long" else
                    f"RVOL ≥ {confirm_rvol:.1f} на пробойной свече, закрытие бара ниже уровня; "
                    f"без объёма пробой считаем ложным и ждём ретест "
                    f"{fmt_level(trigger)} как сопротивления")
    return Plan(
        symbol=s.symbol, name=name, direction=a.direction, trigger=trigger_txt,
        confirmation=confirmation,
        stop=stop_txt,
        last=s.close, trigger_level=trigger, stop_level=stop,
        t1=t1, t2=t2, t1_name=t1_name, t2_name=t2_name,
        targets=(f"T1 {fmt_level(t1)} ({t1_name}) — фиксируем половину, стоп в безубыток; "
                 f"T2 {fmt_level(t2)} ({t2_name}). Риск на сделку {fmt_level(abs(risk))} "
                 f"({safe_div(abs(risk) * 100, s.close):.1f}% от цены)"),
        reward_risk=round(rr, 2),
    )


# ════════════════════════════════════════════════════════════════════════════
#  МАКРО-КОНТЕКСТ
# ════════════════════════════════════════════════════════════════════════════

def macro_lines(snaps: dict[str, Snapshot], events: Events, missing: dict[str, str]) -> list[str]:
    lines: list[str] = []
    names = cfg("benchmarks", {})

    parts: list[str] = []
    for sym, label in names.items():
        s = snaps.get(sym)
        if s:
            parts.append(f"{label} {fmt_level(s.close)} ({fmt_pct(s.change_pct)})")
    if parts:
        lines.append("**Рыночный контекст:** " + "; ".join(parts) + ".")

    vix = snaps.get("^VIX")
    if vix:
        if vix.close >= 25:
            lines.append(f"**Режим риска:** VIX {vix.close:.1f} — повышенная волатильность. "
                         "Размер позиции режем вдвое, стопы ставим по ATR, а не по проценту.")
        elif vix.close <= 14:
            lines.append(f"**Режим риска:** VIX {vix.close:.1f} — сжатие волатильности. "
                         "Пробои чаще ложные; требуем подтверждение объёмом, вход по ретесту.")
        else:
            lines.append(f"**Режим риска:** VIX {vix.close:.1f} — нормальный режим, "
                         "стандартный размер позиции.")

    spy = snaps.get("SPY")
    if spy:
        if spy.trend_d_score >= 1 and spy.trend_w_score >= 1:
            lines.append("**Широкий рынок:** SPY выше ключевых средних на 1D и 1W — "
                         "лонговые сетапы приоритетны, шорты только против явной слабости.")
        elif spy.trend_d_score <= -1 and spy.trend_w_score <= -1:
            lines.append("**Широкий рынок:** SPY ниже ключевых средних на 1D и 1W — "
                         "лонги против тренда не берём, приоритет шортовым сетапам.")
        else:
            lines.append("**Широкий рынок:** SPY без согласованного тренда (1D/1W расходятся) — "
                         "режим отбора, торгуем только сетапы с идеальной структурой.")

    if events.macro:
        for m in events.macro:
            when = str(m.get("time", "")).strip()
            what = str(m.get("event", "")).strip()
            impact = str(m.get("impact", "")).strip().lower()
            note = str(m.get("note", "")).strip()
            if not what:
                continue
            tag = "🔴 HIGH" if impact == "high" else ("🟡 MED" if impact == "medium" else "⚪ LOW")
            row = f"**{tag} · {when or 'время не указано'} — {what}.**"
            if impact == "high":
                row += (" Новых позиций нет за 2 часа до релиза и 30 минут после; "
                        "открытые позиции — либо стоп в безубыток, либо половина объёма.")
            if note:
                row += f" {note}"
            lines.append(row)
    else:
        lines.append("**Экономический календарь не загружен** — `events.json` пуст или отсутствует. "
                     "Пока календарь не заполнен, считаем каждый день потенциально событийным: "
                     "не открываем новых позиций в первые 15 минут сессии и за 2 часа до "
                     "любого известного тебе релиза (FOMC/CPI/PPI/NFP/PCE).")

    if missing:
        lines.append("**Данные не получены:** " + ", ".join(sorted(missing))
                     + " — эти тикеры исключены из анализа, а не оценены по памяти.")
    return lines


# ════════════════════════════════════════════════════════════════════════════
#  РЕНДЕР БРИФА
# ════════════════════════════════════════════════════════════════════════════

def _setup_summary(a: Assessment) -> str:
    s = a.snap
    bits: list[str] = []
    if s.ema20 is not None and s.sma50 is not None:
        bits.append(f"20 EMA {fmt_level(s.ema20)} / 50 SMA {fmt_level(s.sma50)}")
    if s.sma200 is not None:
        bits.append(f"200 SMA {fmt_level(s.sma200)}")
    metrics = []
    if s.rsi14 is not None:
        metrics.append(f"RSI {s.rsi14:.0f}")
    if s.rvol is not None:
        metrics.append(f"RVOL {s.rvol:.2f}")
    if s.atr14:
        metrics.append(f"ATR {fmt_level(s.atr14)} ({s.atr_pct:.1f}%)")
    if metrics:
        bits.append(", ".join(metrics))
    head = a.reasons[0] if a.reasons else "структура нейтральна"
    tail = a.catalysts[0] if a.catalysts else ""
    body = "; ".join([head] + bits)
    if a.warnings:
        body += f" ⚠️ {a.warnings[0]}"
    if tail:
        body += f" Катализатор: {tail}"
    return body


def render_brief(assessments: list[Assessment], snaps: dict[str, Snapshot],
                 events: Events, missing: dict[str, str], as_of: date) -> str:
    out: list[str] = []
    out.append(f"# MARKET DASHBOARD & TRADING PLAN — {as_of.isoformat()}")
    sources = sorted({a.snap.source for a in assessments}) or ["n/a"]
    out.append(f"_Данные на закрытие {as_of.isoformat()} · источник: {', '.join(sources)} · "
               f"сгенерировано {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    out.append("")

    out.append("#### 1. MACRO & RISK WARNINGS")
    for line in macro_lines(snaps, events, missing):
        out.append(f"> {line}")
        out.append(">")
    if out[-1] == ">":
        out.pop()
    out.append("")

    tradables = [a for a in assessments if a.tradable]
    avoid = [a for a in assessments if not a.tradable]
    tradables.sort(key=lambda a: a.score, reverse=True)
    avoid.sort(key=lambda a: a.score, reverse=True)

    out.append("#### 2. TODAY'S WATCHLIST & SETUP QUALITY")
    out.append("| Ticker | Last Price | 1D Change | Trend (1D/1W) | Setup Quality (TR 1-10) | "
               "Catalysts & Technical Setup |")
    out.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
    for a in sorted(assessments, key=lambda x: x.score, reverse=True):
        s = a.snap
        out.append(f"| {s.symbol} | {fmt_price(s.close)} | {fmt_pct(s.change_pct)} | "
                   f"{s.trend_pair} | {a.score:.1f}/10 | {_setup_summary(a)} |")
    out.append("")

    out.append("#### 3. AVOID TODAY (HIGH RISK / NO EDGE)")
    if avoid:
        for a in avoid:
            reason = (a.binary_event and f"бинарное событие — {a.binary_event}") or \
                     (a.warnings[0] if a.warnings else
                      f"качество сетапа {a.score:.1f}/10 ниже порога "
                      f"{num(cfg('scoring.tradable_min'), 6.0):.1f} — нет асимметрии")
            extra = f" (score {a.score:.1f}/10)" if not a.binary_event else ""
            out.append(f"- **{a.snap.symbol}**: {reason}{extra}.")
    else:
        out.append("- Тикеров с явным отсутствием edge не выявлено. "
                   "Это не значит «бери всё» — исполняем только то, что расписано в п.4.")
    out.append("")

    out.append('#### 4. ACTIONABLE EXECUTION PLAN ("WHAT IF IT ACTUALLY HAPPENS")')
    limit = int(num(cfg("scoring.plan_setups"), 3))
    min_rr = num(cfg("execution.min_reward_risk"), 1.5)
    planned = 0
    skipped = 0
    for a in tradables:
        if planned >= limit:
            break
        plan = build_plan(a)
        if not plan:
            continue
        if plan.reward_risk < min_rr:
            if skipped < 2:                     # не раздуваем секцию отказами
                out.append(f"- **{a.snap.symbol}** — сетап качественный ({a.score:.1f}/10), "
                           f"но R:R до T1 всего {plan.reward_risk:.2f} при минимуме {min_rr:.1f}. "
                           "Ждём вход ближе к 20 EMA — сегодня не берём.")
                skipped += 1
            continue
        side = "Bullish" if a.direction == "long" else "Bearish"
        out.append(f"- **{a.snap.symbol} — {side} {plan.name}** "
                   f"(score {a.score:.1f}/10, R:R до T1 ≈ {plan.reward_risk:.2f})")
        out.append(f"  - **Trigger**: {plan.trigger}")
        out.append(f"  - **Confirmation**: {plan.confirmation}")
        out.append(f"  - **Stop Loss / Invalidations**: {plan.stop}")
        out.append(f"  - **Targets**: {plan.targets}")
        planned += 1
    if planned == 0:
        out.append("- Ни один сетап не прошёл фильтр качества и R:R. "
                   "Сегодня сидим на руках — отсутствие сделки это тоже позиция.")
    out.append("")
    out.append("---")
    out.append("_Уровни рассчитаны от реальной структуры цены (20 EMA, 20-дневные экстремумы, ATR). "
               "Все входы условные: нет триггера — нет сделки. Не финансовый совет._")
    return "\n".join(out)


def to_json(assessments: list[Assessment], as_of: date) -> str:
    rows = []
    for a in assessments:
        s = a.snap
        plan = build_plan(a) if a.tradable else None
        rows.append({
            "symbol": s.symbol, "as_of": s.as_of.isoformat(), "source": s.source,
            "close": s.close, "change_pct": round(s.change_pct, 2),
            "trend_1d": s.trend_d, "trend_1w": s.trend_w,
            "ema20": s.ema20, "sma50": s.sma50, "sma200": s.sma200,
            "rsi14": s.rsi14, "atr14": s.atr14, "rvol": s.rvol,
            "high_20d": s.high_20d, "low_20d": s.low_20d,
            "high_52w": s.high_52w, "low_52w": s.low_52w,
            "score": a.score, "direction": a.direction, "tradable": a.tradable,
            "reasons": a.reasons, "warnings": a.warnings,
            "catalysts": a.catalysts, "binary_event": a.binary_event,
            "plan": None if not plan else {
                "name": plan.name, "trigger": plan.trigger,
                "confirmation": plan.confirmation, "stop": plan.stop,
                "targets": plan.targets, "reward_risk": plan.reward_risk,
            },
        })
    return json.dumps({"as_of": as_of.isoformat(), "tickers": rows},
                      ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════════════════════════════
#  ВОТЧЛИСТ
# ════════════════════════════════════════════════════════════════════════════

def load_watchlist(path: Path | None = None) -> list[str]:
    path = path or (ROOT / str(cfg("paths.watchlist", "watchlist.txt")))
    if not path.exists():
        return []
    tickers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            tickers.append(line.upper())
    return list(dict.fromkeys(tickers))


# ════════════════════════════════════════════════════════════════════════════
#  САМОПРОВЕРКА МАТЕМАТИКИ
# ════════════════════════════════════════════════════════════════════════════

def self_test() -> int:
    """Проверяет индикаторы против независимых наивных реализаций."""
    failures: list[str] = []

    def check(name: str, got: Any, want: Any, tol: float = 1e-9) -> None:
        if got is None or want is None:
            failures.append(f"{name}: got={got} want={want}")
            return
        if abs(float(got) - float(want)) > tol:
            failures.append(f"{name}: got={got!r} want={want!r}")

    # Каноничный ряд Уайлдера (StockCharts): опорные значения RSI известны.
    series = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955,
              45.4245, 45.8433, 46.0826, 45.8931, 46.0328, 45.6140, 46.2820,
              46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439, 46.2122,
              46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783,
              44.2181, 44.5672, 43.4205, 42.6628, 43.1314]

    # SMA — прямой пересчёт окна
    naive_sma = sum(series[-10:]) / 10
    check("SMA(10)", _last(sma(series, 10)), naive_sma, 1e-12)

    # EMA — независимый цикл
    k = 2 / 11
    naive_ema = sum(series[:10]) / 10
    for v in series[10:]:
        naive_ema = v * k + naive_ema * (1 - k)
    check("EMA(10)", _last(ema(series, 10)), naive_ema, 1e-12)

    # RSI — опорные значения из классического примера
    rsi_series = rsi(series, 14)
    check("RSI(14) бар 15", rsi_series[14], 70.53, 0.02)
    check("RSI(14) бар 16", rsi_series[15], 66.32, 0.02)
    check("RSI(14) бар 17", rsi_series[16], 66.55, 0.02)
    if not all(v is None or 0.0 <= v <= 100.0 for v in rsi_series):
        failures.append("RSI вышел за границы 0..100")
    if rsi_series[13] is not None:
        failures.append("RSI заполнен раньше периода прогрева")

    # RSI на монотонном росте = 100, на монотонном падении = 0
    check("RSI рост", _last(rsi([float(i) for i in range(1, 40)], 14)), 100.0, 1e-9)
    check("RSI падение", _last(rsi([float(i) for i in range(40, 1, -1)], 14)), 0.0, 1e-9)

    # ATR — на свечах фиксированной высоты без гэпов ATR равен этой высоте
    highs = [10.0 + i for i in range(30)]
    lows = [8.0 + i for i in range(30)]
    closes = [9.0 + i for i in range(30)]
    atr_series = atr(highs, lows, closes, 14)
    if atr_series[-1] is None or not (1.9 < atr_series[-1] < 2.6):
        failures.append(f"ATR(14) вне ожидаемого коридора: {atr_series[-1]}")

    # Недельная свёртка: 10 полных недель по 5 дней → 10 недельных свечей
    bars = Bars(symbol="TEST")
    start = date(2024, 1, 1)                      # понедельник
    day = start
    price = 100.0
    while len(bars.dates) < 50:
        if day.weekday() < 5:
            bars.dates.append(day)
            bars.opens.append(price)
            bars.highs.append(price + 1)
            bars.lows.append(price - 1)
            bars.closes.append(price)
            bars.volumes.append(1000.0)
            price += 1
        day += timedelta(days=1)
    weekly = to_weekly(bars)
    if len(weekly) != 10:
        failures.append(f"to_weekly: ожидалось 10 недель, получено {len(weekly)}")
    if weekly.closes[0] != bars.closes[4]:
        failures.append("to_weekly: закрытие недели не равно закрытию пятницы")
    if weekly.highs[0] != max(bars.highs[:5]):
        failures.append("to_weekly: максимум недели посчитан неверно")
    if weekly.volumes[0] != sum(bars.volumes[:5]):
        failures.append("to_weekly: объём недели не просуммирован")

    # Снимок и оценка на синтетическом восходящем ряду
    up = Bars(symbol="UPTREND")
    d = date(2023, 1, 2)
    p = 50.0
    while len(up.dates) < 300:
        if d.weekday() < 5:
            up.dates.append(d)
            up.opens.append(p)
            up.highs.append(p * 1.01)
            up.lows.append(p * 0.99)
            up.closes.append(p)
            up.volumes.append(1_000_000.0)
            p *= 1.003
        d += timedelta(days=1)
    snap = build_snapshot(up)
    if snap.trend_d != "Bullish" or snap.trend_w != "Bullish":
        failures.append(f"восходящий ряд определён как {snap.trend_pair}")
    a = assess(snap, {}, snap.as_of)
    if a.direction != "long":
        failures.append(f"направление на росте: {a.direction}")
    if not 1.0 <= a.score <= 10.0:
        failures.append(f"score вне диапазона: {a.score}")

    # Нисходящий ряд — зеркально
    down = Bars(symbol="DOWNTREND")
    d, p = date(2023, 1, 2), 200.0
    while len(down.dates) < 300:
        if d.weekday() < 5:
            down.dates.append(d)
            down.opens.append(p)
            down.highs.append(p * 1.01)
            down.lows.append(p * 0.99)
            down.closes.append(p)
            down.volumes.append(1_000_000.0)
            p *= 0.997
        d += timedelta(days=1)
    dsnap = build_snapshot(down)
    dassess = assess(dsnap, {}, dsnap.as_of)
    if dassess.direction != "short":
        failures.append(f"направление на падении: {dassess.direction}")

    # План: стоп по правильную сторону от триггера, R:R положителен
    for case in (a, dassess):
        plan = build_plan(case)
        if plan is None:
            failures.append(f"{case.snap.symbol}: план не построен")
            continue
        if plan.reward_risk <= 0:
            failures.append(f"{case.snap.symbol}: R:R = {plan.reward_risk}")

    # Цели: T1 и T2 всегда по направлению сделки и разнесены между собой
    struct_long = [(112.0, "20-дневный максимум"), (130.0, "годовой максимум")]
    t1, n1, t2, n2 = _target_levels(100.0, 4.0, struct_long, True)
    if not (100.0 < t1 < t2):
        failures.append(f"лонг: цели не по возрастанию (T1={t1}, T2={t2})")
    if n1 != "20-дневный максимум" or n2 != "годовой максимум":
        failures.append(f"лонг: структурные цели не подхвачены ({n1} / {n2})")

    # Структуры нет (новые хаи) — обе цели считаются проекцией ATR
    t1, n1, t2, n2 = _target_levels(100.0, 4.0, [(101.0, "почти вплотную")], True)
    if not (100.0 < t1 < t2) or "ATR" not in n1 or "ATR" not in n2:
        failures.append(f"лонг без структуры: {t1}/{n1}, {t2}/{n2}")

    t1, n1, t2, n2 = _target_levels(100.0, 4.0,
                                    [(88.0, "20-дневный минимум"), (70.0, "годовой минимум")],
                                    False)
    if not (100.0 > t1 > t2):
        failures.append(f"шорт: цели не по убыванию (T1={t1}, T2={t2})")

    # Бинарное событие снимает тикер с торговли
    today = snap.as_of
    blocked = assess(snap, {"earnings": (today + timedelta(days=1)).isoformat()}, today)
    if blocked.tradable or not blocked.binary_event:
        failures.append("отчётность завтра не заблокировала сетап")

    # Деление на ноль и короткие ряды не роняют расчёт
    if _last(sma([1.0, 2.0], 50)) is not None:
        failures.append("SMA на коротком ряду вернула значение")
    if _last(rsi([1.0, 2.0], 14)) is not None:
        failures.append("RSI на коротком ряду вернул значение")
    try:
        build_snapshot(Bars(symbol="EMPTY", dates=[date.today()], opens=[1.0], highs=[1.0],
                            lows=[1.0], closes=[1.0], volumes=[0.0]))
    except DataError:
        pass
    else:
        failures.append("build_snapshot не отверг ряд из одной свечи")

    if failures:
        print("SELF-TEST: ПРОВАЛ")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("SELF-TEST: OK — индикаторы, свёртка недель, скоринг и построение плана сходятся")
    return 0


# ════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Market Dashboard & Trading Plan — институциональный дневной бриф",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Примеры:\n"
               "  python market_dashboard.py\n"
               "  python market_dashboard.py AAPL NVDA BTC-USD\n"
               "  python market_dashboard.py --json --out brief.json\n"
               "  python market_dashboard.py --offline\n")
    p.add_argument("tickers", nargs="*", help="тикеры (по умолчанию из watchlist.txt)")
    p.add_argument("--watchlist", type=Path, help="путь к файлу вотчлиста")
    p.add_argument("--events", type=Path, help="путь к events.json")
    p.add_argument("--offline", action="store_true", help="только кеш, без сети")
    p.add_argument("--no-cache", action="store_true", help="игнорировать кеш, тянуть заново")
    p.add_argument("--no-benchmarks", action="store_true", help="без макро-тикеров")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--md", action="store_true", help="markdown вместо HTML")
    fmt.add_argument("--json", action="store_true", help="JSON вместо HTML")
    p.add_argument("--no-open", action="store_true",
                   help="не открывать HTML в браузере автоматически")
    p.add_argument("--out", type=Path, help="записать в файл (иначе в briefs/)")
    p.add_argument("--no-save", action="store_true", help="не сохранять бриф в briefs/")
    p.add_argument("--self-test", action="store_true", help="проверка математики, без сети")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    if args.self_test:
        return self_test()

    tickers = [t.upper() for t in args.tickers] or load_watchlist(args.watchlist)
    if not tickers:
        wl = args.watchlist or (ROOT / str(cfg("paths.watchlist", "watchlist.txt")))
        print(f"Пустой вотчлист. Впиши тикеры в {wl} или передай их аргументами:\n"
              "  python market_dashboard.py AAPL NVDA BTC-USD", file=sys.stderr)
        return 2

    benchmarks = [] if args.no_benchmarks else list(cfg("benchmarks", {}).keys())
    events = load_events(args.events)

    log.info("Загружаю %d тикеров%s...", len(tickers) + len(benchmarks),
             " (offline)" if args.offline else "")
    bars, failed = load_many(tickers + benchmarks, args.offline, args.no_cache)

    snaps: dict[str, Snapshot] = {}
    for sym, b in bars.items():
        try:
            snaps[sym] = build_snapshot(b)
        except DataError as e:
            failed[sym] = str(e)
            log.warning("%s", e)

    watch_snaps = [snaps[t] for t in tickers if t in snaps]
    if not watch_snaps:
        print("Ни по одному тикеру не удалось получить данные:", file=sys.stderr)
        for err in failed.values():
            print(f"  {err}", file=sys.stderr)
        print("\nПроверь интернет и названия тикеров (формат Yahoo Finance: NVDA, BTC-USD, ^GSPC).\n"
              "Из облачных песочниц котировки недоступны — запускай на своём компьютере.",
              file=sys.stderr)
        return 1

    as_of = max(s.as_of for s in watch_snaps)
    assessments = [assess(s, events.for_ticker(s.symbol), as_of) for s in watch_snaps]
    watch_failed = {k: v for k, v in failed.items() if k in tickers}

    # Формат: явный флаг → расширение --out → HTML по умолчанию.
    want_json, want_md = args.json, args.md
    if not (want_json or want_md) and args.out:
        suffix = args.out.suffix.lower()
        want_json, want_md = suffix == ".json", suffix in (".md", ".markdown")

    if want_json:
        text, ext = to_json(assessments, as_of), "json"
    elif want_md:
        text, ext = render_brief(assessments, snaps, events, watch_failed, as_of), "md"
    else:
        from html_report import render_html          # тянем только когда нужен HTML
        text, ext = render_html(assessments, snaps, events, watch_failed, as_of), "html"

    target: Path | None = None
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        target = args.out
    elif not args.no_save:
        briefs = ROOT / str(cfg("paths.briefs", "briefs"))
        briefs.mkdir(parents=True, exist_ok=True)
        target = briefs / f"{as_of.isoformat()}.{ext}"
        target.write_text(text, encoding="utf-8")

    if ext == "html":
        # HTML в консоль не печатаем — его читают в браузере.
        planned = sum(1 for a in assessments if a.tradable)
        print(f"Бриф на {as_of.isoformat()}: {len(assessments)} тикеров, "
              f"{planned} с рабочим сетапом.")
        if target:
            print(f"Страница: {target}")
            if not args.no_open:
                import webbrowser
                if webbrowser.open(target.resolve().as_uri()):
                    print("Открываю в браузере...")
                else:
                    print("Браузер не открылся автоматически — открой файл вручную.")
        else:
            print(text)
    else:
        print(text)
        if target:
            log.info("Бриф сохранён: %s", target)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
