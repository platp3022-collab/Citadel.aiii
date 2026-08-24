# -*- coding: utf-8 -*-
"""
Геном стратегии — то, что бот придумывает себе сам.

Стратегия = набор условий входа (все должны сработать), условия выхода
(достаточно одного) и параметры риска, привязанные к ATR. Спот — только лонг,
шортов нет, поэтому «стратегия» — это правила «когда купить» и «когда продать».
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field

from .features import ENTRY_POOL, EXIT_POOL


@dataclass
class Genome:
    entry: tuple[str, ...] = ()          # условия входа, объединяются через И
    exit: tuple[str, ...] = ()           # условия выхода, объединяются через ИЛИ
    stop_atr: float = 2.0                # стоп-лосс = N * ATR
    take_atr: float = 4.0                # тейк-профит = N * ATR (0 — без тейка)
    trail_atr: float = 0.0               # трейлинг-стоп = N * ATR (0 — выключен)
    max_hold: int = 96                   # максимум баров в позиции
    risk_pct: float = 1.0                # риск на сделку, % от эквити
    cooldown: int = 2                    # пауза в барах после выхода
    meta: dict = field(default_factory=dict)

    # ── сериализация ────────────────────────────────────────────────────────
    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["entry"] = list(self.entry)
        d["exit"] = list(self.exit)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Genome":
        return cls(
            entry=tuple(d.get("entry", ())),
            exit=tuple(d.get("exit", ())),
            stop_atr=float(d.get("stop_atr", 2.0)),
            take_atr=float(d.get("take_atr", 0.0)),
            trail_atr=float(d.get("trail_atr", 0.0)),
            max_hold=int(d.get("max_hold", 96)),
            risk_pct=float(d.get("risk_pct", 1.0)),
            cooldown=int(d.get("cooldown", 2)),
            meta=dict(d.get("meta", {})),
        )

    @classmethod
    def from_json(cls, raw: str) -> "Genome":
        return cls.from_dict(json.loads(raw))

    # ── человекочитаемое описание ───────────────────────────────────────────
    def describe(self) -> str:
        entry = " И ".join(_RU.get(x, x) for x in self.entry) or "—"
        exits = [f"стоп {self.stop_atr:.1f}×ATR"]
        if self.take_atr:
            exits.append(f"тейк {self.take_atr:.1f}×ATR")
        if self.trail_atr:
            exits.append(f"трейлинг {self.trail_atr:.1f}×ATR")
        if self.exit:
            exits.append("сигнал: " + " ИЛИ ".join(_RU.get(x, x) for x in self.exit))
        exits.append(f"или {self.max_hold} баров в позиции")
        return (f"ВХОД: {entry}\n"
                f"ВЫХОД: {', '.join(exits)}\n"
                f"РИСК: {self.risk_pct:.2f}% эквити на сделку, пауза {self.cooldown} бар(а) после выхода")

    def key(self) -> str:
        """Ключ для дедупликации в популяции."""
        return json.dumps([sorted(self.entry), sorted(self.exit), round(self.stop_atr, 2),
                           round(self.take_atr, 2), round(self.trail_atr, 2), self.max_hold,
                           round(self.risk_pct, 2), self.cooldown], sort_keys=True)


# ════════════════════════════════════════════════════════════════════════════
#  Случайная генерация и мутации
# ════════════════════════════════════════════════════════════════════════════
STOP_GRID = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
TAKE_GRID = (0.0, 2.0, 3.0, 4.0, 6.0, 8.0)
TRAIL_GRID = (0.0, 0.0, 1.5, 2.0, 3.0)
HOLD_GRID = (12, 24, 48, 96, 168, 336)
RISK_GRID = (0.5, 0.75, 1.0, 1.5, 2.0)
COOLDOWN_GRID = (0, 1, 2, 4, 8)


def random_genome(rnd: random.Random) -> Genome:
    n_entry = rnd.choice((1, 2, 2, 3, 3))
    n_exit = rnd.choice((0, 0, 1, 1, 2))
    return Genome(
        entry=tuple(rnd.sample(ENTRY_POOL, n_entry)),
        exit=tuple(rnd.sample(EXIT_POOL, n_exit)) if n_exit else (),
        stop_atr=rnd.choice(STOP_GRID),
        take_atr=rnd.choice(TAKE_GRID),
        trail_atr=rnd.choice(TRAIL_GRID),
        max_hold=rnd.choice(HOLD_GRID),
        risk_pct=rnd.choice(RISK_GRID),
        cooldown=rnd.choice(COOLDOWN_GRID),
    )


def mutate(g: Genome, rnd: random.Random, rate: float = 0.3) -> Genome:
    entry, exits = list(g.entry), list(g.exit)

    if rnd.random() < rate:                                   # правка условий входа
        roll = rnd.random()
        if roll < 0.4 and len(entry) < 3:
            cand = [x for x in ENTRY_POOL if x not in entry]
            entry.append(rnd.choice(cand))
        elif roll < 0.7 and len(entry) > 1:
            entry.pop(rnd.randrange(len(entry)))
        elif entry:
            entry[rnd.randrange(len(entry))] = rnd.choice(ENTRY_POOL)

    if rnd.random() < rate:                                   # правка условий выхода
        roll = rnd.random()
        if roll < 0.4 and len(exits) < 2:
            cand = [x for x in EXIT_POOL if x not in exits]
            exits.append(rnd.choice(cand))
        elif roll < 0.7 and exits:
            exits.pop(rnd.randrange(len(exits)))
        elif exits:
            exits[rnd.randrange(len(exits))] = rnd.choice(EXIT_POOL)

    def maybe(value, grid):
        return rnd.choice(grid) if rnd.random() < rate else value

    entry = list(dict.fromkeys(entry)) or [rnd.choice(ENTRY_POOL)]
    exits = list(dict.fromkeys(exits))
    return Genome(
        entry=tuple(entry),
        exit=tuple(exits),
        stop_atr=maybe(g.stop_atr, STOP_GRID),
        take_atr=maybe(g.take_atr, TAKE_GRID),
        trail_atr=maybe(g.trail_atr, TRAIL_GRID),
        max_hold=maybe(g.max_hold, HOLD_GRID),
        risk_pct=maybe(g.risk_pct, RISK_GRID),
        cooldown=maybe(g.cooldown, COOLDOWN_GRID),
    )


def crossover(a: Genome, b: Genome, rnd: random.Random) -> Genome:
    pool_entry = list(dict.fromkeys(list(a.entry) + list(b.entry)))
    pool_exit = list(dict.fromkeys(list(a.exit) + list(b.exit)))
    n_entry = min(len(pool_entry), rnd.choice((1, 2, 2, 3)))
    n_exit = min(len(pool_exit), rnd.choice((0, 1, 1, 2)))
    pick = lambda x, y: x if rnd.random() < 0.5 else y
    return Genome(
        entry=tuple(rnd.sample(pool_entry, n_entry)) if pool_entry else (rnd.choice(ENTRY_POOL),),
        exit=tuple(rnd.sample(pool_exit, n_exit)) if n_exit else (),
        stop_atr=pick(a.stop_atr, b.stop_atr),
        take_atr=pick(a.take_atr, b.take_atr),
        trail_atr=pick(a.trail_atr, b.trail_atr),
        max_hold=pick(a.max_hold, b.max_hold),
        risk_pct=pick(a.risk_pct, b.risk_pct),
        cooldown=pick(a.cooldown, b.cooldown),
    )


_RU = {
    "ema9_over_ema21": "EMA9 > EMA21", "ema9_over_ema50": "EMA9 > EMA50",
    "ema21_over_ema50": "EMA21 > EMA50", "ema21_over_ema100": "EMA21 > EMA100",
    "ema50_over_ema200": "EMA50 > EMA200", "ema9_cross_ema21": "EMA9 пересекла EMA21 вверх",
    "ema21_cross_ema50": "EMA21 пересекла EMA50 вверх", "ema50_cross_ema200": "золотой крест EMA50/200",
    "price_over_ema50": "цена выше EMA50", "price_over_ema100": "цена выше EMA100",
    "price_over_ema200": "цена выше EMA200",
    "mom10_positive": "рост за 10 баров", "mom20_positive": "рост за 20 баров",
    "mom50_positive": "рост за 50 баров", "mom20_strong": "рост >5% за 20 баров",
    "rsi14_over_50": "RSI14 > 50", "rsi14_over_60": "RSI14 > 60",
    "rsi14_under_40": "RSI14 < 40", "rsi14_under_30": "RSI14 < 30",
    "rsi14_cross_30": "RSI14 вышел из перепроданности", "rsi14_cross_50": "RSI14 пробил 50 вверх",
    "rsi7_under_25": "RSI7 < 25 (перепроданность)",
    "macd_hist_positive": "гистограмма MACD > 0", "macd_cross_signal": "MACD пересёк сигнальную вверх",
    "macd_over_zero": "MACD выше нуля",
    "breakout_dc20": "пробой максимума 20 баров", "breakout_dc55": "пробой максимума 55 баров",
    "breakdown_dc20": "пробой минимума 20 баров вниз",
    "close_over_bb_upper": "закрытие выше верхней Боллинджера",
    "close_under_bb_lower": "закрытие ниже нижней Боллинджера",
    "bounce_from_bb_lower": "отскок от нижней Боллинджера",
    "adx_over_20": "ADX > 20 (есть тренд)", "adx_over_25": "ADX > 25 (сильный тренд)",
    "adx_under_20": "ADX < 20 (флэт)",
    "vol_over_avg": "объём выше среднего", "vol_spike": "всплеск объёма",
    "volatility_high": "волатильность выше нормы", "volatility_low": "волатильность ниже нормы",
    "green_candle": "зелёная свеча", "always": "без фильтра",
}
