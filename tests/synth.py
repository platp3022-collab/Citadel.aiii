# -*- coding: utf-8 -*-
"""Синтетические свечи для тестов (детерминированные)."""
from __future__ import annotations

import math
import random

from citadel.features import Candles


def make_candles(n: int = 1200, seed: int = 42, drift: float = 0.0005,
                 vol: float = 0.012, cycle: float = 0.0, start_ts: int = 0,
                 step_ms: int = 3600000, price: float = 100.0) -> Candles:
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        mu = drift + (cycle * math.sin(i / 200.0) if cycle else 0.0)
        price *= math.exp(rnd.gauss(mu, vol))
        o = price * (1 + rnd.gauss(0, 0.002))
        h = max(o, price) * (1 + abs(rnd.gauss(0, 0.004)))
        l = min(o, price) * (1 - abs(rnd.gauss(0, 0.004)))
        rows.append([start_ts + i * step_ms, o, h, l, price, rnd.uniform(50, 900)])
    return Candles.from_ohlcv(rows)
