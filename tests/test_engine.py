# -*- coding: utf-8 -*-
"""Тесты индикаторов, бэктеста и генома."""
from __future__ import annotations

import math
import unittest

from citadel.backtest import run_backtest
from citadel.config import Config
from citadel.features import build_features, ema, rsi, sma
from citadel.genome import Genome, crossover, mutate, random_genome

from .synth import make_candles


class TestIndicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sma([1, 2, 3, 4, 5], 3)[2:], [2.0, 3.0, 4.0])
        self.assertTrue(math.isnan(sma([1, 2, 3], 3)[1]))

    def test_ema_converges_to_constant(self):
        out = ema([5.0] * 50, 10)
        self.assertAlmostEqual(out[-1], 5.0, places=9)

    def test_rsi_bounds_and_extremes(self):
        up = [float(i) for i in range(1, 60)]
        self.assertAlmostEqual(rsi(up, 14)[-1], 100.0, places=6)
        down = [float(i) for i in range(60, 1, -1)]
        self.assertAlmostEqual(rsi(down, 14)[-1], 0.0, places=6)
        mixed = make_candles(300, seed=3).close
        for v in rsi(mixed, 14):
            if v == v:
                self.assertTrue(0.0 <= v <= 100.0)

    def test_no_lookahead(self):
        """Значения индикаторов на баре i не меняются от появления баров после i."""
        c = make_candles(600, seed=9)
        full = build_features(c)
        cut = build_features(c.slice(0, 400))
        for key in ("ema50", "rsi14", "atr14", "adx14", "macd_hist", "dc_high20"):
            a, b = full.series[key][399], cut.series[key][399]
            if a == a or b == b:
                self.assertAlmostEqual(a, b, places=9, msg=key)
        for key in ("breakout_dc20", "ema21_over_ema50", "rsi14_cross_50"):
            self.assertEqual(full.signals[key][399], cut.signals[key][399], key)

    def test_donchian_excludes_current_bar(self):
        c = make_candles(200, seed=4)
        f = build_features(c)
        i = 150
        self.assertAlmostEqual(f.series["dc_high20"][i], max(c.high[i - 20:i]), places=9)


class TestBacktest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.min_notional = 1.0

    def test_uptrend_makes_money(self):
        f = build_features(make_candles(1500, seed=11, drift=0.0015, vol=0.01))
        g = Genome(entry=("ema21_over_ema50",), exit=("breakdown_dc20",), stop_atr=3.0,
                   take_atr=0.0, trail_atr=0.0, max_hold=336, risk_pct=1.0, cooldown=1)
        r = run_backtest(f, g, self.cfg)
        self.assertGreater(r.n_trades, 0)
        self.assertGreater(r.net_return, 0.0)

    def test_stop_loss_caps_single_trade_loss(self):
        f = build_features(make_candles(1200, seed=5, drift=-0.001, vol=0.02))
        g = Genome(entry=("always",), exit=(), stop_atr=1.0, take_atr=0.0, trail_atr=0.0,
                   max_hold=336, risk_pct=1.0, cooldown=0)
        r = run_backtest(f, g, self.cfg)
        self.assertGreater(r.n_trades, 5)
        for t in r.trades:
            # риск на сделку 1% эквити + допуск на гэп через стоп
            self.assertLess(-t.pnl, r.start_equity * 0.06, f"слишком большой убыток: {t}")

    def test_equity_matches_cash_after_all_trades(self):
        f = build_features(make_candles(900, seed=17, drift=0.0008))
        g = Genome(entry=("rsi14_over_50",), exit=("rsi14_under_40",), stop_atr=2.0,
                   take_atr=3.0, trail_atr=0.0, max_hold=48, risk_pct=1.0, cooldown=1)
        r = run_backtest(f, g, self.cfg)
        pnl = sum(t.pnl for t in r.trades)
        self.assertAlmostEqual(r.end_equity, r.start_equity + pnl, places=6)

    def test_fees_hurt(self):
        f = build_features(make_candles(900, seed=21, drift=0.0006))
        g = Genome(entry=("green_candle",), exit=("always",), stop_atr=3.0, take_atr=0.0,
                   trail_atr=0.0, max_hold=4, risk_pct=1.0, cooldown=0)
        cheap = Config(taker_fee=0.0, slippage_bps=0.0, min_notional=1.0)
        pricey = Config(taker_fee=0.005, slippage_bps=20.0, min_notional=1.0)
        self.assertGreater(run_backtest(f, g, cheap).end_equity,
                           run_backtest(f, g, pricey).end_equity)

    def test_no_trades_without_signal(self):
        f = build_features(make_candles(600, seed=2))
        g = Genome(entry=("rsi14_under_30",), exit=(), stop_atr=2.0, take_atr=0.0,
                   trail_atr=0.0, max_hold=10, risk_pct=1.0, cooldown=0)
        cfg = Config(min_notional=10**9)          # ни один ордер не проходит по минималке
        r = run_backtest(f, g, cfg)
        self.assertEqual(r.n_trades, 0)
        self.assertAlmostEqual(r.end_equity, r.start_equity, places=9)

    def test_backtest_is_deterministic(self):
        f = build_features(make_candles(800, seed=31))
        g = Genome(entry=("macd_hist_positive", "adx_over_20"), exit=("rsi14_under_40",),
                   stop_atr=2.0, take_atr=4.0, trail_atr=2.0, max_hold=72, risk_pct=1.0, cooldown=2)
        a, b = run_backtest(f, g, self.cfg), run_backtest(f, g, self.cfg)
        self.assertEqual(a.as_dict(), b.as_dict())

    def test_past_result_unaffected_by_future_bars(self):
        """Прогон по первым 700 барам одинаков и на урезанной, и на полной истории."""
        c = make_candles(1400, seed=13, drift=0.0007)
        g = Genome(entry=("ema9_over_ema21",), exit=("breakdown_dc20",), stop_atr=2.0,
                   take_atr=4.0, trail_atr=0.0, max_hold=48, risk_pct=1.0, cooldown=1)
        full = run_backtest(build_features(c), g, self.cfg, end=700)
        cut = run_backtest(build_features(c.slice(0, 700)), g, self.cfg)
        self.assertEqual([t.entry_i for t in full.trades], [t.entry_i for t in cut.trades])
        self.assertAlmostEqual(full.end_equity, cut.end_equity, places=6)


class TestGenome(unittest.TestCase):
    def test_roundtrip(self):
        import random as _r
        rnd = _r.Random(1)
        for _ in range(50):
            g = random_genome(rnd)
            self.assertEqual(Genome.from_json(g.to_json()), g)

    def test_mutation_keeps_genome_valid(self):
        import random as _r
        rnd = _r.Random(2)
        g = random_genome(rnd)
        for _ in range(200):
            g = mutate(g, rnd)
            self.assertTrue(1 <= len(g.entry) <= 3)
            self.assertLessEqual(len(g.exit), 2)
            self.assertEqual(len(set(g.entry)), len(g.entry))
            self.assertGreater(g.stop_atr, 0)

    def test_crossover_mixes_parents(self):
        import random as _r
        rnd = _r.Random(3)
        a, b = random_genome(rnd), random_genome(rnd)
        for _ in range(50):
            ch = crossover(a, b, rnd)
            for cond in ch.entry:
                self.assertIn(cond, set(a.entry) | set(b.entry))

    def test_describe_is_human_readable(self):
        g = Genome(entry=("rsi14_under_30", "adx_over_25"), exit=("rsi14_over_60",))
        text = g.describe()
        self.assertIn("RSI14 < 30", text)
        self.assertIn("ВХОД", text)


if __name__ == "__main__":
    unittest.main()
