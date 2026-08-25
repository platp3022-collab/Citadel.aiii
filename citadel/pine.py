# -*- coding: utf-8 -*-
"""
Экспорт стратегии в Pine Script для TradingView.

Геном, который бот вывел себе сам, переводится в готовый скрипт стратегии:
его можно вставить в Pine-редактор, увидеть входы и выходы прямо на графике и
прогнать через Strategy Tester. Второй режим — оверлей с РЕАЛЬНЫМИ сделками
бота из его базы, чтобы сверить график с тем, что он наторговал.
"""
from __future__ import annotations

from .config import TIMEFRAME_SECONDS
from .genome import Genome

# ── объявления индикаторов: ключ → строка Pine (порядок важен) ──────────────
DECLS: dict[str, str] = {
    "atr14": "atr14 = ta.atr(14)",
    "ema9": "ema9 = ta.ema(close, 9)",
    "ema21": "ema21 = ta.ema(close, 21)",
    "ema50": "ema50 = ta.ema(close, 50)",
    "ema100": "ema100 = ta.ema(close, 100)",
    "ema200": "ema200 = ta.ema(close, 200)",
    "rsi14": "rsi14 = ta.rsi(close, 14)",
    "rsi7": "rsi7 = ta.rsi(close, 7)",
    "dmi": "[diPlus, diMinus, adxVal] = ta.dmi(14, 14)",
    "macd": "[macdLine, macdSignal, macdHist] = ta.macd(close, 12, 26, 9)",
    "bb": "[bbBasis, bbUpper, bbLower] = ta.bb(close, 20, 2.0)",
    "dcHigh20": "dcHigh20 = ta.highest(high, 20)[1]",
    "dcHigh55": "dcHigh55 = ta.highest(high, 55)[1]",
    "dcLow20": "dcLow20 = ta.lowest(low, 20)[1]",
    "volSma20": "volSma20 = ta.sma(volume, 20)",
    "mom10": "mom10 = (close / close[10] - 1) * 100",
    "mom20": "mom20 = (close / close[20] - 1) * 100",
    "mom50": "mom50 = (close / close[50] - 1) * 100",
    "atrPct": "atrPct = atr14 / close * 100",
    "atrPctSma": "atrPctSma50 = ta.sma(atrPct, 50)",
}
DECL_ORDER = list(DECLS)

# ── сигнал → (выражение Pine, нужные объявления) ────────────────────────────
SIGNALS: dict[str, tuple[str, tuple[str, ...]]] = {
    "always": ("true", ()),
    "green_candle": ("close > open", ()),
    "price_over_ema50": ("close > ema50", ("ema50",)),
    "price_over_ema100": ("close > ema100", ("ema100",)),
    "price_over_ema200": ("close > ema200", ("ema200",)),
    "rsi14_over_50": ("rsi14 > 50", ("rsi14",)),
    "rsi14_over_60": ("rsi14 > 60", ("rsi14",)),
    "rsi14_under_40": ("rsi14 < 40", ("rsi14",)),
    "rsi14_under_30": ("rsi14 < 30", ("rsi14",)),
    "rsi14_cross_30": ("ta.crossover(rsi14, 30)", ("rsi14",)),
    "rsi14_cross_50": ("ta.crossover(rsi14, 50)", ("rsi14",)),
    "rsi7_under_25": ("rsi7 < 25", ("rsi7",)),
    "macd_hist_positive": ("macdHist > 0", ("macd",)),
    "macd_cross_signal": ("ta.crossover(macdLine, macdSignal)", ("macd",)),
    "macd_over_zero": ("macdLine > 0", ("macd",)),
    "breakout_dc20": ("close > dcHigh20", ("dcHigh20",)),
    "breakout_dc55": ("close > dcHigh55", ("dcHigh55",)),
    "breakdown_dc20": ("close < dcLow20", ("dcLow20",)),
    "close_over_bb_upper": ("close > bbUpper", ("bb",)),
    "close_under_bb_lower": ("close < bbLower", ("bb",)),
    "bounce_from_bb_lower": ("close[1] < bbLower[1] and close > bbLower", ("bb",)),
    "adx_over_20": ("adxVal > 20", ("dmi",)),
    "adx_over_25": ("adxVal > 25", ("dmi",)),
    "adx_under_20": ("adxVal < 20", ("dmi",)),
    "vol_over_avg": ("volume > volSma20", ("volSma20",)),
    "vol_spike": ("volume > 1.8 * volSma20", ("volSma20",)),
    "volatility_high": ("atrPct > atrPctSma50", ("atrPct", "atrPctSma")),
    "volatility_low": ("atrPct < atrPctSma50", ("atrPct", "atrPctSma")),
    "mom10_positive": ("mom10 > 0", ("mom10",)),
    "mom20_positive": ("mom20 > 0", ("mom20",)),
    "mom50_positive": ("mom50 > 0", ("mom50",)),
    "mom20_strong": ("mom20 > 5", ("mom20",)),
}
for _f, _s in ((9, 21), (9, 50), (21, 50), (21, 100), (50, 200)):
    SIGNALS[f"ema{_f}_over_ema{_s}"] = (f"ema{_f} > ema{_s}", (f"ema{_f}", f"ema{_s}"))
    SIGNALS[f"ema{_f}_cross_ema{_s}"] = (f"ta.crossover(ema{_f}, ema{_s})",
                                         (f"ema{_f}", f"ema{_s}"))

#: как таймфрейм бота называется в TradingView
TV_TIMEFRAME = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60",
                "2h": "120", "4h": "240", "6h": "360", "8h": "480", "12h": "720",
                "1d": "1D", "3d": "3D", "1w": "1W"}


class UnsupportedSignal(KeyError):
    """Сигнал бота, для которого нет перевода в Pine."""


def _expr(names, joiner: str) -> tuple[str, list[str]]:
    parts, decls = [], []
    for name in names:
        if name not in SIGNALS:
            raise UnsupportedSignal(name)
        expr, needs = SIGNALS[name]
        parts.append(f"({expr})")
        decls.extend(needs)
    return (joiner.join(parts) if parts else "false"), decls


#: id биржи у нас → как она называется в TradingView
TV_EXCHANGE = {
    "binance": "BINANCE", "binanceus": "BINANCEUS", "bybit": "BYBIT", "okx": "OKX",
    "kucoin": "KUCOIN", "gate": "GATEIO", "gateio": "GATEIO", "mexc": "MEXC",
    "htx": "HTX", "huobi": "HTX", "kraken": "KRAKEN", "coinbase": "COINBASE",
    "bitget": "BITGET", "bitfinex": "BITFINEX", "upbit": "UPBIT", "bingx": "BINGX",
}

#: таймфрейм бота → интервал графика TradingView
TV_INTERVAL = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60",
               "2h": "120", "4h": "240", "6h": "360", "12h": "720", "1d": "D", "1w": "W"}


def tv_symbol(symbol: str, exchange: str = "") -> str:
    """BTC/USDT + binance → BINANCE:BTCUSDT (как пара называется в TradingView)."""
    pair = symbol.replace("/", "").replace(":", "").upper()
    if not exchange:
        return pair
    return f"{TV_EXCHANGE.get(exchange.lower(), exchange.upper())}:{pair}"


def tv_site_url(symbol: str, exchange: str = "", timeframe: str = "1h") -> str:
    """Ссылка на график TradingView в браузере."""
    ticker = tv_symbol(symbol, exchange).replace(":", "%3A")
    return (f"https://www.tradingview.com/chart/?symbol={ticker}"
            f"&interval={TV_INTERVAL.get(timeframe, '60')}")


def tv_widget_url(symbol: str, exchange: str = "", timeframe: str = "1h") -> str:
    """Адрес встраиваемого виджета TradingView — тот же график, но во фрейме."""
    ticker = tv_symbol(symbol, exchange).replace(":", "%3A")
    return (f"https://s.tradingview.com/widgetembed/?symbol={ticker}"
            f"&interval={TV_INTERVAL.get(timeframe, '60')}&theme=dark&style=1&locale=ru"
            f"&timezone=Etc%2FUTC&withdateranges=1&hide_side_toolbar=0"
            f"&allow_symbol_change=1&save_image=0")


def to_pine(g: Genome, symbol: str, timeframe: str, strategy_id: int | None = None,
            score: float | None = None, capital: float = 1000.0,
            commission_pct: float = 0.1, max_position_frac: float = 0.35,
            exchange: str = "") -> str:
    """Собирает готовый Pine-скрипт стратегии по геному."""
    entry_expr, d1 = _expr(g.entry, " and ")
    exit_expr, d2 = _expr(g.exit, " or ") if g.exit else ("false", [])

    needed = ["atr14"] + d1 + d2
    decls = [DECLS[k] for k in DECL_ORDER if k in set(needed)]

    title = f"Citadel · {symbol}" + (f" #{strategy_id}" if strategy_id else "")
    tv_tf = TV_TIMEFRAME.get(timeframe, "60")
    head = [
        "//@version=6",
        f"// {title}" + (f" · скор на валидации {score:.2f}" if score is not None else ""),
        "// Стратегию вывел бот Citadel Trader (python tradebot.py pine).",
        f"// Инструмент: {tv_symbol(symbol, exchange)}, таймфрейм: {timeframe} "
        f"— ставь на график именно с таким ТФ.",
        "//",
    ] + [f"// {line}" for line in g.describe().splitlines()] + [
        "//",
        "// Спот, только лонг. Сигнал считается по закрытой свече, вход — по открытию следующей,",
        "// поэтому process_orders_on_close оставлен выключенным (как в бэктесте бота).",
        "",
        f'strategy("{title}", overlay=true, initial_capital={capital:g}, pyramiding=0,',
        "     default_qty_type=strategy.percent_of_equity, default_qty_value=100,",
        f"     commission_type=strategy.commission.percent, commission_value={commission_pct:g},",
        "     calc_on_every_tick=false, process_orders_on_close=false,",
        "     max_labels_count=500)",
        "",
        "// ── параметры риска (правь и смотри, как меняется результат) ──────────────",
        f'riskPct  = input.float({g.risk_pct:g}, "Риск на сделку, % эквити", minval=0.05, step=0.05)',
        f'stopAtr  = input.float({g.stop_atr:g}, "Стоп-лосс, ×ATR", minval=0.1, step=0.1)',
        f'takeAtr  = input.float({g.take_atr:g}, "Тейк-профит, ×ATR (0 — без тейка)", minval=0, step=0.1)',
        f'trailAtr = input.float({g.trail_atr:g}, "Трейлинг-стоп, ×ATR (0 — выключен)", minval=0, step=0.1)',
        f'maxHold  = input.int({g.max_hold}, "Максимум баров в позиции", minval=1)',
        f'cooldown = input.int({g.cooldown}, "Пауза после выхода, баров", minval=0)',
        f'posFrac  = input.float({max_position_frac * 100:g}, "Максимум % эквити в позиции", minval=1, maxval=100)',
        "",
        "// ── индикаторы ───────────────────────────────────────────────────────────",
    ] + decls + [
        "",
        "// ── условия стратегии ────────────────────────────────────────────────────",
        f"entryCond = {entry_expr}",
        f"exitCond  = {exit_expr}",
        "",
        "// ── исполнение ───────────────────────────────────────────────────────────",
        "var float stopLevel = na",
        "var float takeLevel = na",
        "var float peakPrice = na",
        "var int   entryBar  = na",
        "var int   lastExit  = na",
        "",
        "flat     = strategy.position_size == 0",
        "cooled   = na(lastExit) or (bar_index - lastExit) > cooldown",
        "canEnter = flat and cooled and not na(atr14) and atr14 > 0",
        "",
        "if entryCond and canEnter",
        "    stopDist = stopAtr * atr14",
        "    qtyRisk  = strategy.equity * riskPct / 100 / stopDist",
        "    qtyCap   = strategy.equity * posFrac / 100 / close",
        "    qty      = math.min(qtyRisk, qtyCap)",
        "    if qty > 0",
        '        strategy.entry("long", strategy.long, qty=qty)',
        '        alert("Citadel: покупка " + syminfo.ticker, alert.freq_once_per_bar_close)',
        "",
        "// позиция только что открылась — фиксируем уровни от фактической цены входа",
        "justOpened = strategy.position_size > 0 and nz(strategy.position_size[1]) == 0",
        "if justOpened",
        "    entryBar  := bar_index",
        "    stopLevel := strategy.position_avg_price - stopAtr * atr14",
        "    takeLevel := takeAtr > 0 ? strategy.position_avg_price + takeAtr * atr14 : na",
        "    peakPrice := high",
        "",
        "if strategy.position_size > 0",
        "    peakPrice := math.max(nz(peakPrice, high), high)",
        "    if trailAtr > 0",
        "        stopLevel := math.max(nz(stopLevel, low), peakPrice - trailAtr * atr14)",
        "    if not na(stopLevel) or not na(takeLevel)",
        '        strategy.exit("выход", from_entry="long", stop=stopLevel, limit=takeLevel)',
        "    if exitCond",
        '        strategy.close("long", comment="сигнал")',
        "    else if not na(entryBar) and (bar_index - entryBar) >= maxHold",
        '        strategy.close("long", comment="время")',
        "",
        "if strategy.position_size == 0 and nz(strategy.position_size[1]) != 0",
        "    lastExit  := bar_index",
        "    stopLevel := na",
        "    takeLevel := na",
        "    peakPrice := na",
        "    entryBar  := na",
        '    alert("Citadel: выход " + syminfo.ticker, alert.freq_once_per_bar_close)',
        "",
        "// ── что видно на графике ─────────────────────────────────────────────────",
        'plot(strategy.position_size > 0 ? stopLevel : na, "Стоп", color.new(color.red, 0),',
        "     style=plot.style_linebr, linewidth=2)",
        'plot(strategy.position_size > 0 ? takeLevel : na, "Тейк", color.new(color.teal, 0),',
        "     style=plot.style_linebr, linewidth=2)",
        'bgcolor(strategy.position_size > 0 ? color.new(color.teal, 92) : na, title="В позиции")',
        'plotshape(entryCond and canEnter, "Сигнал входа", shape.triangleup, location.belowbar,',
        "          color.new(color.teal, 0), size=size.tiny)",
    ]
    if g.exit:
        head += ['plotshape(exitCond and strategy.position_size > 0, "Сигнал выхода", shape.triangledown,',
                 "          location.abovebar, color.new(color.orange, 0), size=size.tiny)"]
    head += ["", f"// таймфрейм графика должен быть {tv_tf} ({timeframe})"]
    return "\n".join(head) + "\n"


def trades_overlay(symbol: str, timeframe: str, buys: list[tuple[int, float]],
                   sells: list[tuple[int, float, float]], limit: int = 250) -> str:
    """Индикатор-оверлей с реальными сделками бота: метки покупок и продаж на графике."""
    buys = buys[-limit:]
    sells = sells[-limit:]
    step_ms = TIMEFRAME_SECONDS.get(timeframe, 3600) * 1000

    def ints(values) -> str:
        return ", ".join(str(int(v)) for v in values)

    def floats(values) -> str:
        # в Pine array.from() тип выводится по литералам: без точки получится
        # массив int, и str.tostring() отработает не так, как ждём
        out = []
        for v in values:
            text = f"{float(v):.10g}"
            if "." not in text and "e" not in text and "E" not in text:
                text += ".0"
            out.append(text)
        return ", ".join(out)

    lines = [
        "//@version=6",
        f"// Citadel · реальные сделки бота по {tv_symbol(symbol)} ({timeframe})",
        "// Сгенерировано: python tradebot.py pine --trades",
        f'indicator("Citadel · сделки {symbol}", overlay=true, max_labels_count=500)',
        "",
        f"// покупки: время в мс и цена исполнения ({len(buys)} шт.)",
        f"buyTs = array.from({ints(t for t, _ in buys)})" if buys else "buyTs = array.new<int>()",
        f"buyPx = array.from({floats(p for _, p in buys)})" if buys else "buyPx = array.new<float>()",
        f"// продажи: время, цена, P&L ({len(sells)} шт.)",
        f"sellTs  = array.from({ints(t for t, _, _ in sells)})" if sells else "sellTs = array.new<int>()",
        f"sellPx  = array.from({floats(p for _, p, _ in sells)})" if sells else "sellPx = array.new<float>()",
        f"sellPnl = array.from({floats(x for _, _, x in sells)})" if sells else "sellPnl = array.new<float>()",
        "",
        f"barMs = {step_ms}",
        "// указатели: массивы отсортированы по времени, поэтому идём по ним один раз",
        "var int bi = 0",
        "var int si = 0",
        "",
        "barStart = time",
        "barEnd   = time + barMs",
        "",
        "while bi < array.size(buyTs) and array.get(buyTs, bi) < barEnd",
        "    if array.get(buyTs, bi) >= barStart",
        "        label.new(bar_index, low, \"куп \" + str.tostring(array.get(buyPx, bi), format.mintick),",
        "                  style=label.style_label_up, color=color.new(color.teal, 20),",
        "                  textcolor=color.white, size=size.small)",
        "    bi += 1",
        "",
        "while si < array.size(sellTs) and array.get(sellTs, si) < barEnd",
        "    if array.get(sellTs, si) >= barStart",
        "        pnl = array.get(sellPnl, si)",
        "        label.new(bar_index, high, \"прод \" + str.tostring(array.get(sellPx, si), format.mintick) +",
        "                  \"\\n\" + (pnl >= 0 ? \"+\" : \"\") + str.tostring(pnl, \"#.##\"),",
        "                  style=label.style_label_down,",
        "                  color=pnl >= 0 ? color.new(color.green, 20) : color.new(color.red, 20),",
        "                  textcolor=color.white, size=size.small)",
        "    si += 1",
        "",
        "// метки рисуются только на истории, которую прогрузил график:",
        "// если сделок не видно — промотай график левее или уменьши таймфрейм",
    ]
    return "\n".join(lines) + "\n"
