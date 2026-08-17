#!/usr/bin/env python3
"""
AI Market Screener & Analyst Bot
================================

Pipeline:  Data Ingestion -> Technical Processing -> LLM Analysis -> Output

  1. Pulls daily OHLCV for a list of tickers (yfinance for equities/ETFs/indices,
     ccxt for crypto pairs).
  2. Computes indicators locally in pandas (EMA20 / SMA50 / SMA200 / RSI14 /
     ATR14 / RVOL / clustered swing support & resistance).
  3. Serialises everything into one compact JSON snapshot.
  4. Sends the snapshot to the Claude API under a strict system prompt and a
     JSON schema, so the model can only answer in the exact structure we want.
  5. Renders a Markdown trading plan from that JSON and writes it to disk.

The model never sees raw candles and never computes numbers: arithmetic happens
in pandas, judgement happens in the LLM. That split is what keeps the output
reproducible and auditable.

Usage:
    python main.py --tickers AAPL,MSFT,NVDA,TSLA
    python main.py --tickers BTC/USDT,ETH/USDT --exchange binance
    python main.py --tickers-file watchlist.txt --out reports/
    python main.py --tickers AAPL --dry-run      # no API call, prints the payload

NOT FINANCIAL ADVICE. This is an information-filtering tool.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# SECTION 0 — configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent

MODEL = "claude-opus-5"
MAX_TOKENS = 32_000
DEFAULT_EFFORT = "high"          # low | medium | high | xhigh | max

# Server-side refusal fallback: if Claude's safety classifiers decline the
# request, the API silently re-runs it on a fallback model instead of handing
# us back an empty response. Costs nothing when it never fires.
ENABLE_REFUSAL_FALLBACK = True
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ANALYSIS_WINDOW = 100            # trading days summarised for the model
HISTORY_BARS = 420               # bars actually fetched — SMA200 needs 200+
MAX_EXECUTION_PLANS = 5
NEWS_PER_TICKER = 4

DEFAULT_BENCHMARKS = ["SPY", "QQQ", "^VIX"]

log = logging.getLogger("screener")


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader — avoids a python-dotenv dependency."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# ---------------------------------------------------------------------------
# SECTION 1 — data ingestion
# ---------------------------------------------------------------------------

def is_crypto(symbol: str) -> bool:
    """Crypto pairs are written exchange-style: BTC/USDT, ETH/USDC."""
    return "/" in symbol


def fetch_equity(symbol: str) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=True)
    if raw.empty:
        raise ValueError(f"yfinance returned no rows for {symbol!r}")
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    return df.dropna(subset=["close"]).tail(HISTORY_BARS)


def fetch_crypto(symbol: str, exchange_id: str) -> pd.DataFrame:
    import ccxt

    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=min(HISTORY_BARS, 1000))
    if not ohlcv:
        raise ValueError(f"{exchange_id} returned no candles for {symbol!r}")
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df.dropna(subset=["close"]).tail(HISTORY_BARS)


def fetch_bars(symbol: str, exchange_id: str) -> pd.DataFrame:
    return fetch_crypto(symbol, exchange_id) if is_crypto(symbol) else fetch_equity(symbol)


def fetch_all(symbols: Iterable[str], exchange_id: str, workers: int = 4) -> dict[str, pd.DataFrame]:
    """Fetch in parallel; a failed symbol is logged and dropped, not fatal."""
    symbols = list(symbols)
    out: dict[str, pd.DataFrame] = {}

    def one(sym: str) -> tuple[str, pd.DataFrame | None]:
        try:
            return sym, fetch_bars(sym, exchange_id)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not kill the run
            log.warning("skip %s: %s", sym, exc)
            return sym, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sym, df in pool.map(one, symbols):
            if df is not None and len(df) >= 30:
                out[sym] = df
            elif df is not None:
                log.warning("skip %s: only %d bars, need >= 30", sym, len(df))
    return out


def fetch_news(symbol: str, limit: int = NEWS_PER_TICKER) -> list[dict[str, str]]:
    """Headlines only. yfinance has changed this payload shape twice, so be defensive."""
    if is_crypto(symbol):
        return []
    try:
        import yfinance as yf

        items = yf.Ticker(symbol).news or []
    except Exception as exc:  # noqa: BLE001
        log.debug("news unavailable for %s: %s", symbol, exc)
        return []

    out: list[dict[str, str]] = []
    for item in items[:limit]:
        body = item.get("content") if isinstance(item.get("content"), dict) else item
        title = body.get("title") or item.get("title")
        if not title:
            continue
        stamp = body.get("pubDate") or item.get("providerPublishTime")
        if isinstance(stamp, (int, float)):
            stamp = datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="minutes")
        out.append({"title": str(title)[:200], "published": str(stamp)[:19] if stamp else None})
    return out


# ---------------------------------------------------------------------------
# SECTION 2 — technical indicators (pure pandas, no pandas_ta)
# ---------------------------------------------------------------------------
# pandas_ta 0.3.14b0 still does `from numpy import NaN`, which was removed in
# numpy 2.0 — it fails at import time on any modern install. These are ~40 lines
# of pandas and remove that whole class of dependency breakage.

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (EWM with alpha = 1/period is Wilder smoothing)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss          # avg_loss == 0 -> inf -> RSI 100
        out = 100.0 - (100.0 / (1.0 + rs))
    flat = (avg_gain == 0) & (avg_loss == 0)
    return out.mask(flat, 50.0)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range, Wilder-smoothed. Used for ATR-based stops."""
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rvol(volume: pd.Series, period: int = 20) -> pd.Series:
    """Relative volume: today vs the average of the *prior* N sessions."""
    baseline = volume.shift(1).rolling(period, min_periods=period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        return volume / baseline.replace(0.0, np.nan)


def swing_pivots(df: pd.DataFrame, left: int = 3, right: int = 3) -> tuple[list[float], list[float]]:
    """Local highs/lows: a bar that is the extreme of its centred window."""
    window = left + right + 1
    high, low = df["high"], df["low"]
    highs = high[high == high.rolling(window, center=True).max()].dropna()
    lows = low[low == low.rolling(window, center=True).min()].dropna()
    return [float(x) for x in highs], [float(x) for x in lows]


def cluster_levels(levels: list[float], tolerance: float) -> list[float]:
    """Merge pivots that sit within `tolerance` of each other into one level."""
    clean = sorted(x for x in levels if np.isfinite(x))
    if not clean:
        return []
    groups: list[list[float]] = [[clean[0]]]
    for value in clean[1:]:
        if value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [round(sum(g) / len(g), 4) for g in groups]


# ---------------------------------------------------------------------------
# SECTION 3 — snapshot builder (the JSON handed to the model)
# ---------------------------------------------------------------------------

def _round(value: Any, digits: int = 2) -> float | None:
    """Round for the payload; NaN/inf become null so the model sees 'unknown'."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(number) else round(number, digits)


def _pct_change(series: pd.Series, bars: int) -> float | None:
    if len(series) <= bars:
        return None
    past = series.iloc[-1 - bars]
    return None if past == 0 else _round((series.iloc[-1] / past - 1) * 100)


def _ma_stack(price: float, e20: float | None, s50: float | None, s200: float | None) -> str:
    values = [e20, s50, s200]
    if any(v is None for v in values):
        return "insufficient_history"
    if price > e20 > s50 > s200:
        return "bullish"
    if price < e20 < s50 < s200:
        return "bearish"
    return "mixed"


def build_snapshot(symbol: str, df: pd.DataFrame, window: int = ANALYSIS_WINDOW) -> dict[str, Any]:
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]

    e20 = ema(close, 20)
    s50 = sma(close, 50)
    s200 = sma(close, 200)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    rv = rvol(volume, 20)

    price = float(close.iloc[-1])
    atr_now = _round(atr14.iloc[-1], 4)
    e20_now, s50_now, s200_now = (_round(x.iloc[-1], 4) for x in (e20, s50, s200))

    # Support / resistance from the analysis window only — older pivots are noise.
    recent = df.tail(window)
    pivot_highs, pivot_lows = swing_pivots(recent)
    tolerance = (atr_now or price * 0.01) * 0.75
    resistances = [lv for lv in cluster_levels(pivot_highs, tolerance) if lv > price]
    supports = [lv for lv in cluster_levels(pivot_lows, tolerance) if lv < price]

    nearest_res = min(resistances) if resistances else None
    nearest_sup = max(supports) if supports else None

    def _distance(level: float | None) -> float | None:
        return None if level is None else _round((level / price - 1) * 100)

    w20, w100 = df.tail(20), df.tail(window)

    return {
        "ticker": symbol,
        "asset_class": "crypto" if is_crypto(symbol) else "equity",
        "last_bar": df.index[-1].date().isoformat(),
        "bars_available": int(len(df)),
        "price": _round(price, 4),
        "change_1d_pct": _pct_change(close, 1),
        "change_5d_pct": _pct_change(close, 5),
        "change_20d_pct": _pct_change(close, 20),
        "ema20": e20_now,
        "sma50": s50_now,
        "sma200": s200_now,
        "price_vs_ema20_pct": _distance(e20_now),
        "price_vs_sma50_pct": _distance(s50_now),
        "price_vs_sma200_pct": _distance(s200_now),
        "ma_stack": _ma_stack(price, e20_now, s50_now, s200_now),
        "rsi14": _round(rsi14.iloc[-1]),
        "rsi14_5d_ago": _round(rsi14.iloc[-6]) if len(rsi14) > 6 else None,
        "atr14": atr_now,
        "atr14_pct_of_price": _round((atr_now / price) * 100) if atr_now else None,
        "rvol": _round(rv.iloc[-1]),
        "avg_volume_20d": _round(volume.tail(20).mean(), 0),
        "gap_pct": _round((df["open"].iloc[-1] / close.iloc[-2] - 1) * 100) if len(df) > 1 else None,
        "range_20d": {"high": _round(w20["high"].max(), 4), "low": _round(w20["low"].min(), 4)},
        f"range_{window}d": {"high": _round(w100["high"].max(), 4), "low": _round(w100["low"].min(), 4)},
        "resistance_levels": sorted(resistances)[:4],
        "support_levels": sorted(supports, reverse=True)[:4],
        "nearest_resistance": nearest_res,
        "nearest_support": nearest_sup,
        "pct_to_resistance": _distance(nearest_res),
        "pct_to_support": _distance(nearest_sup),
        "news": [],
    }


def build_breadth(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(snapshots)
    if not total:
        return {}

    def share(predicate) -> float | None:
        eligible = [s for s in snapshots if predicate(s) is not None]
        if not eligible:
            return None
        return _round(100 * sum(1 for s in eligible if predicate(s)) / len(eligible))

    rsi_values = [s["rsi14"] for s in snapshots if s["rsi14"] is not None]
    return {
        "instruments": total,
        "above_sma50_pct": share(lambda s: None if s["sma50"] is None else s["price"] > s["sma50"]),
        "above_sma200_pct": share(lambda s: None if s["sma200"] is None else s["price"] > s["sma200"]),
        "advancers_1d_pct": share(lambda s: None if s["change_1d_pct"] is None else s["change_1d_pct"] > 0),
        "avg_rsi14": _round(sum(rsi_values) / len(rsi_values)) if rsi_values else None,
    }


def build_payload(
    instruments: list[dict[str, Any]],
    benchmarks: list[dict[str, Any]],
    window: int,
) -> dict[str, Any]:
    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date": max(s["last_bar"] for s in instruments),
        "analysis_window_days": window,
        "data_notes": (
            "All indicators are pre-computed from daily bars. A null value means "
            "there was not enough history to compute it. Support/resistance are "
            "clustered swing pivots from the analysis window. rvol compares today's "
            "volume to the mean of the prior 20 sessions."
        ),
        "market_context": {"benchmarks": benchmarks, "breadth": build_breadth(instruments)},
        "instruments": instruments,
    }


# ---------------------------------------------------------------------------
# SECTION 4 — Claude integration: system prompt + output schema
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a disciplined technical strategist writing one pre-market trading plan \
for a single self-directed trader. You are the judgement layer of an automated \
screener: the arithmetic is already done and handed to you as JSON.

# Input contract
You receive one JSON snapshot per run. Read it as ground truth and as your only \
source of market data — you have no live feed, no chart, and no knowledge of \
anything that happened after the snapshot's `session_date`.

- Every indicator is pre-computed from daily bars. Do not recompute them.
- `null` means "not enough history to compute". Treat it as unknown. Never \
substitute a guess, and never treat a null as zero or as neutral.
- `ma_stack` is `bullish` (price > EMA20 > SMA50 > SMA200), `bearish` (the exact \
inverse), `mixed`, or `insufficient_history`.
- `rvol` is today's volume divided by the mean of the prior 20 sessions. Below \
0.7 is dead participation; above 1.5 is a genuine crowd.
- `support_levels` / `resistance_levels` are clustered swing pivots taken from \
the analysis window, sorted by distance from the current price.
- `atr14` is the daily true range in price units — this is your unit of noise. \
Any stop closer than 1x ATR from the entry is inside the noise band.
- `market_context.breadth` describes the supplied basket only, not the whole \
market. Do not generalise it into a claim about the broad market.
- `news` holds headlines only, with no body text. Use a headline as a flag for \
event risk, never as a fact you can reason from in detail.

# Hard rules
1. Use only numbers present in the snapshot. Never invent a price, level, \
indicator reading, earnings date, analyst target, or news item. If a fact you \
want is absent, say it is absent.
2. Every price level you output must be a snapshot value (a support or \
resistance level, a moving average, a range boundary) or simple arithmetic on \
one — for example `support - 0.5 * atr14`. Name the derivation in the text so a \
reader can check it.
3. A stop must sit beyond the level that invalidates the idea, and at least \
1x ATR away from the trigger. Never place a stop at a round number chosen for \
looking tidy.
4. Reward-to-risk, computed as `abs(target - trigger) / abs(trigger - stop)`, \
must be at least 1.5. If a setup cannot clear that, it does not get an \
execution plan — say so instead of stretching the target.
5. Score every instrument 1-10 on this rubric, and apply it literally:
   - 1-3: no setup, or the setup fights the dominant trend.
   - 4-5: worth watching, but the trigger is far away or the evidence conflicts.
   - 6-7: a valid setup with a defined trigger; needs confirmation to act.
   - 8-10: trend, momentum, participation (rvol) and proximity to a decision \
level all point the same way.
6. Only instruments scoring 6 or higher may appear in `execution_plans`, and \
there may be at most {MAX_PLANS} plans. Fewer is correct when the tape is thin — \
an empty plan list is a valid, respectable answer.
7. Every instrument in `instruments` must appear exactly once in `watchlist`. \
Instruments in `market_context.benchmarks` must not appear there at all.
8. `avoid_today` is for instruments with a concrete disqualifier: rvol under \
0.7, price pinned inside a tight range with no edge nearby, a headline implying \
binary event risk, or `insufficient_history`. Give the specific reason, not a \
generic one. Return an empty array if nothing qualifies.
9. Do not use RSI in isolation. An extreme RSI in a strong trend is continuation, \
not a reversal signal — treat it as one only when price is also rejecting a \
named level.
10. Write plainly. No hedging filler, no "consult a financial advisor" \
boilerplate, no restating the rules back at the reader. The disclaimer is added \
downstream. Every sentence should carry a fact or a decision.

# Output
Return only the JSON object matching the provided schema. All free-text fields \
must be written in {LANGUAGE}. Ticker symbols and numbers stay as-is.
"""

LANGUAGE_NAMES = {"ru": "Russian", "en": "English"}

SCORE_ENUM = list(range(1, 11))

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "macro": {
            "type": "object",
            "properties": {
                "regime": {"type": "string", "enum": ["risk_on", "neutral", "risk_off"]},
                "summary": {"type": "string"},
                "key_risks": {"type": "array", "items": {"type": "string"}},
                "rules_for_today": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["regime", "summary", "key_risks", "rules_for_today"],
            "additionalProperties": False,
        },
        "watchlist": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "trend": {
                        "type": "string",
                        "enum": ["uptrend", "downtrend", "range", "unclear"],
                    },
                    "score": {"type": "integer", "enum": SCORE_ENUM},
                    "bias": {"type": "string", "enum": ["long", "short", "none"]},
                    "setup": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["ticker", "trend", "score", "bias", "setup", "rationale"],
                "additionalProperties": False,
            },
        },
        "avoid_today": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["ticker", "reason"],
                "additionalProperties": False,
            },
        },
        "execution_plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "direction": {"type": "string", "enum": ["long", "short"]},
                    "trigger": {"type": "string"},
                    "confirmation": {"type": "string"},
                    "stop_loss": {"type": "string"},
                    "target": {"type": "string"},
                    "reward_risk": {"type": "string"},
                    "invalidation": {"type": "string"},
                },
                "required": [
                    "ticker",
                    "direction",
                    "trigger",
                    "confirmation",
                    "stop_loss",
                    "target",
                    "reward_risk",
                    "invalidation",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["macro", "watchlist", "avoid_today", "execution_plans"],
    "additionalProperties": False,
}


def build_system_prompt(language: str) -> str:
    return SYSTEM_PROMPT.replace("{MAX_PLANS}", str(MAX_EXECUTION_PLANS)).replace(
        "{LANGUAGE}", LANGUAGE_NAMES.get(language, "English")
    )


def _stream_final(client, use_fallback: bool, **kwargs):
    """One request. Streaming keeps a long, high-effort turn under the HTTP timeout."""
    if use_fallback:
        stream = client.beta.messages.stream(
            betas=[FALLBACK_BETA], fallbacks="default", **kwargs
        )
    else:
        stream = client.messages.stream(**kwargs)
    with stream as active:
        return active.get_final_message()


def analyse_with_claude(
    payload: dict[str, Any],
    language: str,
    effort: str,
    model: str = MODEL,
) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from the environment

    user_content = (
        "Market snapshot for today's session. Produce the trading plan.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "system": [
            {
                "type": "text",
                "text": build_system_prompt(language),
                # The system prompt is byte-identical between runs, so it caches.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        "messages": [{"role": "user", "content": user_content}],
    }

    try:
        message = _stream_final(client, ENABLE_REFUSAL_FALLBACK, **kwargs)
    except TypeError as exc:
        # Installed SDK predates the fallbacks parameter — retry without it.
        log.warning("refusal fallback unavailable (%s); retrying without it", exc)
        message = _stream_final(client, False, **kwargs)

    if message.stop_reason == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        raise RuntimeError(f"Request declined by safety classifiers (category={category})")
    if message.stop_reason == "max_tokens":
        raise RuntimeError("Response hit max_tokens and is truncated — raise MAX_TOKENS")

    text = next((b.text for b in message.content if b.type == "text"), None)
    if not text:
        raise RuntimeError(f"No text block in response (stop_reason={message.stop_reason})")

    usage = message.usage
    log.info(
        "tokens in=%s cached=%s out=%s",
        usage.input_tokens,
        getattr(usage, "cache_read_input_tokens", 0),
        usage.output_tokens,
    )
    return json.loads(text)


# ---------------------------------------------------------------------------
# SECTION 5 — Markdown renderer
# ---------------------------------------------------------------------------

LABELS = {
    "ru": {
        "title": "Торговый план",
        "generated": "Сгенерировано",
        "model": "Модель",
        "instruments": "Инструментов",
        "macro": "Макро-риск и правила дня",
        "regime": "Режим",
        "risks": "Ключевые риски",
        "rules": "Правила на сегодня",
        "watchlist": "Watchlist",
        "avoid": "Не торгуем сегодня",
        "plans": "Планы исполнения",
        "trigger": "Триггер",
        "confirmation": "Подтверждение",
        "stop": "Стоп-лосс",
        "target": "Цель",
        "rr": "Risk / Reward",
        "invalidation": "Отмена идеи",
        "cols": ["Тикер", "Тренд", "Score", "Bias", "Сетап"],
        "none_avoid": "Нет инструментов с явными противопоказаниями.",
        "none_plans": "Валидных сетапов на сегодня нет — торговать нечего.",
        "disclaimer": (
            "Не является инвестиционной рекомендацией. Инструмент фильтрации "
            "информации: все уровни и сценарии — гипотезы, а не сигналы."
        ),
    },
    "en": {
        "title": "Trading Plan",
        "generated": "Generated",
        "model": "Model",
        "instruments": "Instruments",
        "macro": "Macro Risk & Rules",
        "regime": "Regime",
        "risks": "Key risks",
        "rules": "Rules for today",
        "watchlist": "Watchlist",
        "avoid": "Avoid Today",
        "plans": "Execution Plans",
        "trigger": "Trigger",
        "confirmation": "Confirmation",
        "stop": "Stop-loss",
        "target": "Target",
        "rr": "Risk / Reward",
        "invalidation": "Invalidation",
        "cols": ["Ticker", "Trend", "Score", "Bias", "Setup"],
        "none_avoid": "Nothing disqualified today.",
        "none_plans": "No valid setups today — nothing to trade.",
        "disclaimer": (
            "Not investment advice. This is an information-filtering tool: every "
            "level and scenario is a hypothesis, not a signal."
        ),
    },
}

REGIME_ICON = {"risk_on": "🟢", "neutral": "🟡", "risk_off": "🔴"}


def render_markdown(plan: dict[str, Any], payload: dict[str, Any], language: str, model: str) -> str:
    label = LABELS.get(language, LABELS["en"])
    macro = plan["macro"]
    lines: list[str] = []

    lines.append(f"# {label['title']} — {payload['session_date']}")
    lines.append(
        f"> {label['generated']}: {payload['as_of_utc']} UTC · "
        f"{label['model']}: `{model}` · "
        f"{label['instruments']}: {len(payload['instruments'])}"
    )
    lines.append("")

    lines.append(f"## {label['macro']}")
    lines.append(
        f"**{label['regime']}:** {REGIME_ICON.get(macro['regime'], '')} "
        f"`{macro['regime'].upper()}`"
    )
    lines.append("")
    lines.append(macro["summary"])
    if macro["key_risks"]:
        lines.append("")
        lines.append(f"**{label['risks']}:**")
        lines.extend(f"- {risk}" for risk in macro["key_risks"])
    if macro["rules_for_today"]:
        lines.append("")
        lines.append(f"**{label['rules']}:**")
        lines.extend(f"{i}. {rule}" for i, rule in enumerate(macro["rules_for_today"], 1))
    lines.append("")

    lines.append(f"## {label['watchlist']}")
    lines.append("| " + " | ".join(label["cols"]) + " |")
    lines.append("|" + "---|" * len(label["cols"]))
    for row in sorted(plan["watchlist"], key=lambda r: -r["score"]):
        lines.append(
            f"| `{row['ticker']}` | {row['trend']} | **{row['score']}/10** | "
            f"{row['bias']} | {row['setup']} |"
        )
    lines.append("")
    for row in sorted(plan["watchlist"], key=lambda r: -r["score"]):
        lines.append(f"- **{row['ticker']}** ({row['score']}/10) — {row['rationale']}")
    lines.append("")

    lines.append(f"## {label['avoid']}")
    if plan["avoid_today"]:
        lines.extend(f"- **{item['ticker']}** — {item['reason']}" for item in plan["avoid_today"])
    else:
        lines.append(f"_{label['none_avoid']}_")
    lines.append("")

    lines.append(f"## {label['plans']}")
    if not plan["execution_plans"]:
        lines.append(f"_{label['none_plans']}_")
    for item in plan["execution_plans"]:
        arrow = "🔼" if item["direction"] == "long" else "🔽"
        lines.append("")
        lines.append(f"### {arrow} {item['ticker']} — {item['direction'].upper()}")
        lines.append(f"- **{label['trigger']}:** {item['trigger']}")
        lines.append(f"- **{label['confirmation']}:** {item['confirmation']}")
        lines.append(f"- **{label['stop']}:** {item['stop_loss']}")
        lines.append(f"- **{label['target']}:** {item['target']}")
        lines.append(f"- **{label['rr']}:** {item['reward_risk']}")
        lines.append(f"- **{label['invalidation']}:** {item['invalidation']}")
    lines.append("")

    lines.append("---")
    lines.append(f"_{label['disclaimer']}_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SECTION 6 — CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI Market Screener & Analyst Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tickers", help="Comma-separated: AAPL,MSFT or BTC/USDT,ETH/USDT")
    source.add_argument("--tickers-file", type=Path, help="One ticker per line; # for comments")

    parser.add_argument("--benchmarks", default=",".join(DEFAULT_BENCHMARKS),
                        help="Context instruments, not scored. Empty string disables.")
    parser.add_argument("--exchange", default="binance", help="ccxt exchange id for crypto pairs")
    parser.add_argument("--days", type=int, default=ANALYSIS_WINDOW, help="Analysis window in bars")
    parser.add_argument("--effort", default=DEFAULT_EFFORT,
                        choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--lang", default="ru", choices=sorted(LANGUAGE_NAMES))
    parser.add_argument("--out", type=Path, default=ROOT / "reports")
    parser.add_argument("--no-news", action="store_true", help="Skip headline fetching")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and print the payload, make no API call")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def resolve_tickers(args: argparse.Namespace) -> list[str]:
    if args.tickers:
        raw = args.tickers.split(",")
    else:
        raw = args.tickers_file.read_text(encoding="utf-8").splitlines()
    seen: dict[str, None] = {}
    for item in raw:
        item = item.split("#")[0].strip().upper()
        if item:
            seen[item] = None
    return list(seen)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    load_env()

    tickers = resolve_tickers(args)
    benchmarks = [b.strip().upper() for b in args.benchmarks.split(",") if b.strip()]
    if not tickers:
        log.error("no tickers resolved")
        return 2

    log.info("fetching %d instruments (+%d benchmarks)", len(tickers), len(benchmarks))
    bars = fetch_all(tickers + benchmarks, args.exchange)
    if not bars:
        log.error("no data fetched for any symbol")
        return 1

    instrument_snaps = [build_snapshot(t, bars[t], args.days) for t in tickers if t in bars]
    benchmark_snaps = [build_snapshot(b, bars[b], args.days) for b in benchmarks if b in bars]
    if not instrument_snaps:
        log.error("no tradable instrument survived data fetching")
        return 1

    if not args.no_news:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for snap, headlines in zip(
                instrument_snaps, pool.map(lambda s: fetch_news(s["ticker"]), instrument_snaps)
            ):
                snap["news"] = headlines

    payload = build_payload(instrument_snaps, benchmark_snaps, args.days)

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info("dry run: no API call made")
        return 0

    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        log.error("ANTHROPIC_API_KEY is not set — put it in .env or export it")
        return 2

    log.info("calling %s (effort=%s)", args.model, args.effort)
    plan = analyse_with_claude(payload, args.lang, args.effort, args.model)
    report = render_markdown(plan, payload, args.lang, args.model)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    md_path = args.out / f"plan_{stamp}.md"
    json_path = args.out / f"plan_{stamp}.json"
    md_path.write_text(report, encoding="utf-8")
    json_path.write_text(
        json.dumps({"payload": payload, "plan": plan}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(report)
    log.info("saved %s and %s", md_path, json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
