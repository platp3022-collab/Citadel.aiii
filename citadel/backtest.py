# -*- coding: utf-8 -*-
"""
Бэктест-движок: спот, только лонг, одна позиция на символ.

Правила, чтобы не обманывать себя:
  • сигнал считается по ЗАКРЫТОЙ свече i, вход исполняется по open свечи i+1;
  • стоп/тейк проверяются по high/low свечи, и если в одном баре задело и стоп,
    и тейк — считаем, что сработал стоп (пессимистично);
  • комиссия и проскальзывание списываются с каждой сделки;
  • размер позиции считается от риска на стоп, ограничен долей эквити и кэшем.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import BARS_PER_YEAR, Config
from .features import Features
from .genome import Genome


@dataclass
class Trade:
    entry_i: int
    exit_i: int
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float                  # чистый P&L в quote-валюте, с комиссиями
    pnl_pct: float              # доходность на вложенный объём, %
    reason: str                 # stop / take / trail / signal / max_hold / end
    bars: int


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    start_equity: float = 0.0
    end_equity: float = 0.0
    net_return: float = 0.0     # доля, 0.25 = +25%
    max_dd: float = 0.0         # доля, 0.2 = просадка 20%
    sharpe: float = 0.0         # годовой, по барной доходности
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_trade_pct: float = 0.0
    exposure: float = 0.0       # доля времени в позиции
    n_trades: int = 0
    buy_hold: float = 0.0       # доходность «купил и держи» за тот же период

    def summary(self) -> str:
        return (f"сделок {self.n_trades} | доход {self.net_return*100:+.1f}% "
                f"(buy&hold {self.buy_hold*100:+.1f}%) | просадка {self.max_dd*100:.1f}% | "
                f"Sharpe {self.sharpe:.2f} | винрейт {self.win_rate*100:.0f}% | "
                f"PF {self.profit_factor:.2f} | в рынке {self.exposure*100:.0f}%")

    def as_dict(self) -> dict:
        return {"n_trades": self.n_trades, "net_return": self.net_return, "max_dd": self.max_dd,
                "sharpe": self.sharpe, "win_rate": self.win_rate, "profit_factor": self.profit_factor,
                "avg_trade_pct": self.avg_trade_pct, "exposure": self.exposure,
                "buy_hold": self.buy_hold, "end_equity": self.end_equity}


def run_backtest(f: Features, g: Genome, cfg: Config,
                 start: int | None = None, end: int | None = None,
                 balance: float | None = None) -> Result:
    c = f.candles
    n = len(c)
    start = f.warmup if start is None else max(start, 1)
    end = n if end is None else min(end, n)
    equity = balance if balance is not None else cfg.start_balance
    res = Result(start_equity=equity)
    if end - start < 10:
        res.end_equity = equity
        res.equity = [equity]
        return res

    entry_sigs = [f.signals[k] for k in g.entry if k in f.signals]
    exit_sigs = [f.signals[k] for k in g.exit if k in f.signals]
    atr = f.series["atr14"]
    fee, slip = cfg.taker_fee, cfg.slippage_bps / 10000.0

    cash = equity
    qty = 0.0
    entry_price = stop = take = trail_stop = 0.0
    entry_i = 0
    peak = 0.0
    bars_held = 0
    cooldown_until = start
    in_market_bars = 0
    curve: list[float] = []
    peak_equity = equity
    max_dd = 0.0

    def close_position(i: int, price: float, reason: str) -> None:
        nonlocal cash, qty, bars_held
        fill = price * (1 - slip)
        proceeds = fill * qty * (1 - fee)
        cost = entry_price * qty * (1 + fee)   # entry_price уже со слиппеджем
        cash += proceeds
        pnl = proceeds - cost
        res.trades.append(Trade(
            entry_i=entry_i, exit_i=i, entry_ts=c.ts[entry_i], exit_ts=c.ts[i],
            entry_price=entry_price, exit_price=fill, qty=qty, pnl=pnl,
            pnl_pct=(pnl / cost * 100.0) if cost else 0.0, reason=reason, bars=i - entry_i,
        ))
        qty = 0.0
        bars_held = 0

    for i in range(start, end):
        prev = i - 1

        # ── управление открытой позицией внутри свечи i ─────────────────────
        if qty > 0:
            bars_held += 1
            eff_stop = max(stop, trail_stop)
            if c.low[i] <= eff_stop:
                close_position(i, min(eff_stop, c.open[i]),
                               "trail" if trail_stop > stop else "stop")
                cooldown_until = i + 1 + g.cooldown
            elif take and c.high[i] >= take:
                close_position(i, max(take, c.open[i]), "take")
                cooldown_until = i + 1 + g.cooldown
            else:
                if g.trail_atr and atr[i] == atr[i]:
                    peak = max(peak, c.high[i])
                    trail_stop = max(trail_stop, peak - g.trail_atr * atr[i])

        # ── выход по сигналу/времени: решение на закрытии i, исполнение i+1 ─
        if qty > 0 and i + 1 < end:
            hit_exit = any(s[i] for s in exit_sigs)
            if hit_exit or bars_held >= g.max_hold:
                close_position(i + 1, c.open[i + 1], "signal" if hit_exit else "max_hold")
                cooldown_until = i + 2 + g.cooldown

        # ── вход: сигнал на закрытой свече prev, покупка по open i ──────────
        if qty == 0 and i >= cooldown_until and entry_sigs:
            if all(s[prev] for s in entry_sigs):
                a = atr[prev]
                if a == a and a > 0:
                    fill = c.open[i] * (1 + slip)
                    stop_price = fill - g.stop_atr * a
                    risk_per_unit = fill - stop_price
                    if risk_per_unit > 0:
                        eq = cash
                        by_risk = eq * (g.risk_pct / 100.0) / risk_per_unit
                        by_cap = eq * cfg.max_position_frac / fill
                        by_cash = (cash / (fill * (1 + fee))) * 0.999
                        size = min(by_risk, by_cap, by_cash)
                        if size * fill >= max(cfg.min_notional, 1e-9):
                            qty = size
                            entry_price = fill
                            entry_i = i
                            cash -= fill * qty * (1 + fee)
                            stop = stop_price
                            take = fill + g.take_atr * a if g.take_atr else 0.0
                            trail_stop = 0.0
                            peak = c.high[i]
                            bars_held = 0

        eq_now = cash + qty * c.close[i]
        curve.append(eq_now)
        if qty > 0:
            in_market_bars += 1
        peak_equity = max(peak_equity, eq_now)
        if peak_equity > 0:
            max_dd = max(max_dd, 1.0 - eq_now / peak_equity)

    if qty > 0:                                   # закрываем хвост по последней цене
        close_position(end - 1, c.close[end - 1], "end")
        curve[-1] = cash

    res.equity = curve
    res.end_equity = curve[-1] if curve else equity
    res.net_return = res.end_equity / res.start_equity - 1.0 if res.start_equity else 0.0
    res.max_dd = max_dd
    res.n_trades = len(res.trades)
    res.exposure = in_market_bars / max(1, len(curve))

    wins = [t for t in res.trades if t.pnl > 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in res.trades if t.pnl <= 0)
    res.win_rate = len(wins) / res.n_trades if res.n_trades else 0.0
    res.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)
    res.avg_trade_pct = sum(t.pnl_pct for t in res.trades) / res.n_trades if res.n_trades else 0.0
    res.sharpe = _sharpe(curve, cfg.timeframe)
    base = c.close[start]
    res.buy_hold = (c.close[end - 1] / base - 1.0) if base else 0.0
    return res


def _sharpe(curve: list[float], timeframe: str) -> float:
    if len(curve) < 3:
        return 0.0
    rets = []
    for i in range(1, len(curve)):
        prev = curve[i - 1]
        if prev > 0:
            rets.append(curve[i] / prev - 1.0)
    if len(rets) < 3:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return m / sd * math.sqrt(BARS_PER_YEAR.get(timeframe, 8760))
