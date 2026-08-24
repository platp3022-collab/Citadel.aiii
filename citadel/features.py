# -*- coding: utf-8 -*-
"""
Индикаторы и «сигнальная сетка».

Из свечей считаем набор индикаторов, а из них — словарь булевых условий
(`signals`). Геном стратегии не хранит формулы, он хранит имена условий,
поэтому поиск стратегии сводится к перебору комбинаций уже посчитанных
массивов — быстро и без утечки будущего (все значения на баре i считаются
только по барам <= i).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

NAN = float("nan")


# ════════════════════════════════════════════════════════════════════════════
#  Свечи
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class Candles:
    ts: list[int]       # время открытия бара, мс
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    volume: list[float]

    def __len__(self) -> int:
        return len(self.close)

    @classmethod
    def from_ohlcv(cls, rows: Sequence[Sequence[float]]) -> "Candles":
        rows = [r for r in rows if r and r[4] is not None]
        return cls(
            ts=[int(r[0]) for r in rows],
            open=[float(r[1]) for r in rows],
            high=[float(r[2]) for r in rows],
            low=[float(r[3]) for r in rows],
            close=[float(r[4]) for r in rows],
            volume=[float(r[5] or 0.0) for r in rows],
        )

    def to_ohlcv(self) -> list[list[float]]:
        return [[self.ts[i], self.open[i], self.high[i], self.low[i], self.close[i], self.volume[i]]
                for i in range(len(self))]

    def slice(self, start: int, end: int | None = None) -> "Candles":
        end = len(self) if end is None else end
        return Candles(self.ts[start:end], self.open[start:end], self.high[start:end],
                       self.low[start:end], self.close[start:end], self.volume[start:end])


# ════════════════════════════════════════════════════════════════════════════
#  Базовые индикаторы (чистый python, NaN на прогреве)
# ════════════════════════════════════════════════════════════════════════════
def sma(src: list[float], n: int) -> list[float]:
    out, s = [NAN] * len(src), 0.0
    for i, v in enumerate(src):
        s += v
        if i >= n:
            s -= src[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(src: list[float], n: int) -> list[float]:
    out = [NAN] * len(src)
    if len(src) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(src[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(src)):
        prev = src[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rma(src: list[float], n: int) -> list[float]:
    """Сглаживание Уайлдера — база для RSI/ATR/ADX."""
    out = [NAN] * len(src)
    if len(src) < n:
        return out
    prev = sum(src[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(src)):
        prev = (prev * (n - 1) + src[i]) / n
        out[i] = prev
    return out


def stdev(src: list[float], n: int) -> list[float]:
    out = [NAN] * len(src)
    for i in range(n - 1, len(src)):
        window = src[i - n + 1:i + 1]
        m = sum(window) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in window) / n)
    return out


def rolling_max(src: list[float], n: int, shift: int = 0) -> list[float]:
    out = [NAN] * len(src)
    for i in range(len(src)):
        end = i + 1 - shift
        start = end - n
        if start < 0 or end <= 0:
            continue
        out[i] = max(src[start:end])
    return out


def rolling_min(src: list[float], n: int, shift: int = 0) -> list[float]:
    out = [NAN] * len(src)
    for i in range(len(src)):
        end = i + 1 - shift
        start = end - n
        if start < 0 or end <= 0:
            continue
        out[i] = min(src[start:end])
    return out


def rsi(close: list[float], n: int = 14) -> list[float]:
    gains = [0.0] * len(close)
    losses = [0.0] * len(close)
    for i in range(1, len(close)):
        d = close[i] - close[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    ag, al = rma(gains[1:], n), rma(losses[1:], n)
    out = [NAN] * len(close)
    for i in range(len(ag)):
        g, l = ag[i], al[i]
        if g != g or l != l:
            continue
        out[i + 1] = 100.0 if l == 0 else 100.0 - 100.0 / (1 + g / l)
    return out


def true_range(c: Candles) -> list[float]:
    out = [c.high[0] - c.low[0]] if len(c) else []
    for i in range(1, len(c)):
        out.append(max(c.high[i] - c.low[i],
                       abs(c.high[i] - c.close[i - 1]),
                       abs(c.low[i] - c.close[i - 1])))
    return out


def atr(c: Candles, n: int = 14) -> list[float]:
    return rma(true_range(c), n)


def adx(c: Candles, n: int = 14) -> list[float]:
    """Сила тренда по Уайлдеру (без направления)."""
    size = len(c)
    if size < 2:
        return [NAN] * size
    plus_dm, minus_dm = [0.0] * size, [0.0] * size
    for i in range(1, size):
        up = c.high[i] - c.high[i - 1]
        down = c.low[i - 1] - c.low[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    tr = true_range(c)
    atr_ = rma(tr[1:], n)
    pdi_s, mdi_s = rma(plus_dm[1:], n), rma(minus_dm[1:], n)
    dx = [NAN] * size
    for i in range(len(atr_)):
        a = atr_[i]
        if a != a or a == 0:
            continue
        p = 100.0 * pdi_s[i] / a
        m = 100.0 * mdi_s[i] / a
        if p + m == 0:
            continue
        dx[i + 1] = 100.0 * abs(p - m) / (p + m)
    clean = [x for x in dx if x == x]
    smoothed = rma(clean, n)
    out = [NAN] * size
    offset = size - len(clean)
    for i, v in enumerate(smoothed):
        out[offset + i] = v
    return out


def macd(close: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(close, fast), ema(close, slow)
    line = [ef[i] - es[i] for i in range(len(close))]
    valid = [v for v in line if v == v]
    sig_valid = ema(valid, signal)
    sig = [NAN] * len(close)
    offset = len(close) - len(valid)
    for i, v in enumerate(sig_valid):
        sig[offset + i] = v
    hist = [line[i] - sig[i] for i in range(len(close))]
    return line, sig, hist


# ════════════════════════════════════════════════════════════════════════════
#  Сигнальная сетка
# ════════════════════════════════════════════════════════════════════════════
def _cross_up(a: list[float], b: list[float]) -> list[bool]:
    out = [False] * len(a)
    for i in range(1, len(a)):
        if a[i] > b[i] and a[i - 1] <= b[i - 1]:
            out[i] = True
    return out


def _cross_up_level(a: list[float], level: float) -> list[bool]:
    out = [False] * len(a)
    for i in range(1, len(a)):
        if a[i] > level and a[i - 1] <= level:
            out[i] = True
    return out


def _gt(a: list[float], b: list[float]) -> list[bool]:
    return [a[i] > b[i] for i in range(len(a))]     # NaN > x == False, прогрев отсекается сам


def _gt_level(a: list[float], level: float) -> list[bool]:
    return [v > level for v in a]


def _lt_level(a: list[float], level: float) -> list[bool]:
    return [v < level for v in a]


@dataclass
class Features:
    """Посчитанные по свечам ряды + булевы сигналы."""
    candles: Candles
    series: dict[str, list[float]]
    signals: dict[str, list[bool]]
    warmup: int

    @property
    def names(self) -> list[str]:
        return sorted(self.signals)


def build_features(c: Candles) -> Features:
    n = len(c)
    close, high, low, vol = c.close, c.high, c.low, c.volume
    s: dict[str, list[float]] = {}
    sig: dict[str, list[bool]] = {}

    for p in (9, 21, 50, 100, 200):
        s[f"ema{p}"] = ema(close, p)
    s["sma20"] = sma(close, 20)
    s["rsi14"] = rsi(close, 14)
    s["rsi7"] = rsi(close, 7)
    s["atr14"] = atr(c, 14)
    s["adx14"] = adx(c, 14)
    line, sigl, hist = macd(close)
    s["macd"], s["macd_signal"], s["macd_hist"] = line, sigl, hist
    sd = stdev(close, 20)
    s["bb_upper"] = [s["sma20"][i] + 2 * sd[i] for i in range(n)]
    s["bb_lower"] = [s["sma20"][i] - 2 * sd[i] for i in range(n)]
    s["dc_high20"] = rolling_max(high, 20, shift=1)
    s["dc_high55"] = rolling_max(high, 55, shift=1)
    s["dc_low20"] = rolling_min(low, 20, shift=1)
    s["dc_low55"] = rolling_min(low, 55, shift=1)
    s["vol_sma20"] = sma(vol, 20)

    # доходности за N баров, в процентах
    for p in (5, 10, 20, 50):
        r = [NAN] * n
        for i in range(p, n):
            base = close[i - p]
            if base:
                r[i] = (close[i] / base - 1.0) * 100.0
        s[f"mom{p}"] = r

    # нормированная волатильность: ATR к цене
    s["atr_pct"] = [(s["atr14"][i] / close[i] * 100.0) if close[i] else NAN for i in range(n)]
    s["atr_pct_sma50"] = sma([v if v == v else 0.0 for v in s["atr_pct"]], 50)

    # ── тренд ───────────────────────────────────────────────────────────────
    for fast, slow in ((9, 21), (9, 50), (21, 50), (21, 100), (50, 200)):
        sig[f"ema{fast}_over_ema{slow}"] = _gt(s[f"ema{fast}"], s[f"ema{slow}"])
        sig[f"ema{fast}_cross_ema{slow}"] = _cross_up(s[f"ema{fast}"], s[f"ema{slow}"])
    for p in (50, 100, 200):
        sig[f"price_over_ema{p}"] = _gt(close, s[f"ema{p}"])
    for p in (10, 20, 50):
        sig[f"mom{p}_positive"] = _gt_level(s[f"mom{p}"], 0.0)
    sig["mom20_strong"] = _gt_level(s["mom20"], 5.0)

    # ── импульс / осцилляторы ───────────────────────────────────────────────
    sig["rsi14_over_50"] = _gt_level(s["rsi14"], 50.0)
    sig["rsi14_over_60"] = _gt_level(s["rsi14"], 60.0)
    sig["rsi14_under_40"] = _lt_level(s["rsi14"], 40.0)
    sig["rsi14_under_30"] = _lt_level(s["rsi14"], 30.0)
    sig["rsi14_cross_30"] = _cross_up_level(s["rsi14"], 30.0)
    sig["rsi14_cross_50"] = _cross_up_level(s["rsi14"], 50.0)
    sig["rsi7_under_25"] = _lt_level(s["rsi7"], 25.0)
    sig["macd_hist_positive"] = _gt_level(s["macd_hist"], 0.0)
    sig["macd_cross_signal"] = _cross_up(s["macd"], s["macd_signal"])
    sig["macd_over_zero"] = _gt_level(s["macd"], 0.0)

    # ── пробои / возвраты к среднему ────────────────────────────────────────
    sig["breakout_dc20"] = _gt(close, s["dc_high20"])
    sig["breakout_dc55"] = _gt(close, s["dc_high55"])
    sig["breakdown_dc20"] = [close[i] < s["dc_low20"][i] for i in range(n)]
    sig["close_over_bb_upper"] = _gt(close, s["bb_upper"])
    sig["close_under_bb_lower"] = [close[i] < s["bb_lower"][i] for i in range(n)]
    sig["bounce_from_bb_lower"] = [
        i > 0 and close[i - 1] < s["bb_lower"][i - 1] and close[i] > s["bb_lower"][i]
        for i in range(n)
    ]

    # ── режим рынка / объём ─────────────────────────────────────────────────
    sig["adx_over_20"] = _gt_level(s["adx14"], 20.0)
    sig["adx_over_25"] = _gt_level(s["adx14"], 25.0)
    sig["adx_under_20"] = _lt_level(s["adx14"], 20.0)
    sig["vol_over_avg"] = [vol[i] > s["vol_sma20"][i] for i in range(n)]
    sig["vol_spike"] = [vol[i] > 1.8 * s["vol_sma20"][i] for i in range(n)]
    sig["volatility_high"] = _gt(s["atr_pct"], s["atr_pct_sma50"])
    sig["volatility_low"] = [s["atr_pct"][i] < s["atr_pct_sma50"][i] for i in range(n)]
    sig["green_candle"] = [close[i] > c.open[i] for i in range(n)]
    sig["always"] = [True] * n

    warmup = 220 if n > 260 else max(60, n // 4)
    return Features(candles=c, series=s, signals=sig, warmup=warmup)


#: сигналы, которые бессмысленно использовать как условие выхода из лонга
ENTRY_ONLY = {"always"}

#: пул условий входа
ENTRY_POOL = [
    "ema9_over_ema21", "ema9_over_ema50", "ema21_over_ema50", "ema21_over_ema100",
    "ema50_over_ema200", "ema9_cross_ema21", "ema21_cross_ema50", "ema50_cross_ema200",
    "price_over_ema50", "price_over_ema100", "price_over_ema200",
    "mom10_positive", "mom20_positive", "mom50_positive", "mom20_strong",
    "rsi14_over_50", "rsi14_over_60", "rsi14_under_40", "rsi14_under_30",
    "rsi14_cross_30", "rsi14_cross_50", "rsi7_under_25",
    "macd_hist_positive", "macd_cross_signal", "macd_over_zero",
    "breakout_dc20", "breakout_dc55", "close_over_bb_upper",
    "close_under_bb_lower", "bounce_from_bb_lower",
    "adx_over_20", "adx_over_25", "adx_under_20",
    "vol_over_avg", "vol_spike", "volatility_high", "volatility_low", "green_candle",
]

#: пул условий выхода
EXIT_POOL = [
    "rsi14_over_60", "rsi14_under_40", "macd_hist_positive", "macd_over_zero",
    "ema9_over_ema21", "ema21_over_ema50", "price_over_ema50", "price_over_ema100",
    "breakdown_dc20", "close_over_bb_upper", "close_under_bb_lower",
    "mom10_positive", "mom20_positive", "adx_under_20", "volatility_high",
]
