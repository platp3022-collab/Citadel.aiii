# -*- coding: utf-8 -*-
"""Тесты самодостаточной HTML-страницы состояния."""
from __future__ import annotations

import re
import tempfile
import time
import unittest
from pathlib import Path

from citadel import dashboard
from citadel.config import Config
from citadel.genome import Genome
from citadel.storage import Storage


class TestCharts(unittest.TestCase):
    def test_line_chart_needs_two_points(self):
        self.assertIn("empty", dashboard.line_chart([]))
        self.assertIn("empty", dashboard.line_chart([(1, 1)]))
        self.assertIn("<svg", dashboard.line_chart([(1, 1), (2, 2)]))

    def test_line_chart_survives_flat_series(self):
        svg = dashboard.line_chart([(i * 1000, 100.0) for i in range(20)])
        self.assertIn("<svg", svg)
        self.assertNotIn("nan", svg.lower())

    def test_price_chart_marks_trades_inside_window(self):
        candles = [[i * 3600_000, 100 + i, 102 + i, 98 + i, 101 + i, 5.0] for i in range(50)]
        trades = [
            {"ts": 10 * 3600, "side": "buy", "price": 110.0, "pnl": 0.0},
            {"ts": 30 * 3600, "side": "sell", "price": 135.0, "pnl": 25.0},
            {"ts": 40 * 3600, "side": "sell", "price": 120.0, "pnl": -8.0},
        ]
        svg = dashboard.price_chart(candles, trades)
        self.assertEqual(svg.count('class="buy"'), 1)
        self.assertEqual(svg.count('class="sell-win"'), 1)
        self.assertEqual(svg.count('class="sell-loss"'), 1)
        self.assertNotIn("вне окна графика", svg)

    def test_price_chart_hints_when_trades_are_outside(self):
        candles = [[i * 3600_000, 100, 102, 98, 101, 5.0] for i in range(50)]
        far = [{"ts": 10 ** 9, "side": "buy", "price": 110.0, "pnl": 0.0}]
        self.assertIn("вне окна графика", dashboard.price_chart(candles, far))

    def test_price_chart_without_candles(self):
        self.assertIn("empty", dashboard.price_chart([], []))


class TestPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(symbols=("BTC/USDT",), start_balance=1000.0,
                          db_path=str(Path(self.tmp.name) / "t.db"),
                          cache_dir=str(Path(self.tmp.name) / "c"))
        self.store = Storage(self.cfg.db_path)
        sid = self.store.save_strategy(
            "BTC/USDT", "1h", Genome(entry=("rsi14_over_50", "adx_over_25")), 1.7,
            {"train": {"n_trades": 20, "net_return": 0.3, "max_dd": 0.05, "profit_factor": 2.1},
             "valid": {"n_trades": 8, "net_return": 0.1, "max_dd": 0.03, "profit_factor": 1.4}})
        self.store.activate(sid, "BTC/USDT")
        self.store.set("paper_cash", 1150.0)
        self.store.set("paper_start", 1000.0)
        now = int(time.time())
        self.store.log_trade("BTC/USDT", "buy", 0.1, 100.0, 10.0, 0.01, 0.0, "entry", False)
        self.store.log_trade("BTC/USDT", "sell", 0.1, 120.0, 12.0, 0.01, 1.9, "take", False)
        self.store.log_equity(1150.0, 1150.0)
        rows = [[(now - (300 - i) * 3600) * 1000, 100, 102, 98, 100 + i, 3.0] for i in range(300)]
        from citadel import candlecache
        candlecache.write(candlecache.path_for(self.cfg.cache_dir, self.cfg.exchange,
                                               "BTC/USDT", "1h"), rows)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_collect_reads_bot_state(self):
        d = dashboard.collect(self.cfg, self.store)
        self.assertAlmostEqual(d["cash"], 1150.0)
        self.assertAlmostEqual(d["pnl_pct"], 15.0)
        self.assertEqual(len(d["trades"]), 2)
        self.assertEqual(d["strategies"][0]["id"], 1)
        self.assertGreater(len(d["strategies"][0]["candles"]), 100)

    def test_page_is_self_contained(self):
        html = dashboard.render(dashboard.collect(self.cfg, self.store))
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertNotIn("<script", html.lower())            # скриптов нет вообще
        self.assertNotIn("src=", html)                       # ничего не подгружается
        self.assertNotIn("@import", html)
        self.assertNotIn("http://", html)
        # единственные внешние адреса — ссылки на биржи/обозреватели, не ресурсы
        for match in re.findall(r'href="(https?://[^"]+)"', html):
            self.assertRegex(match, r"^https://(solscan\.io|dexscreener\.com)/")

    def test_page_shows_strategy_and_metrics(self):
        html = dashboard.render(dashboard.collect(self.cfg, self.store))
        self.assertIn("RSI14 &gt; 50", html)                 # описание экранировано
        self.assertIn("валидация: сделок 8", html)
        self.assertIn("обучение: сделок 20", html)
        self.assertIn("1 150.00", html.replace(" ", " ").replace("\xa0", " "))

    def test_refresh_meta_only_when_asked(self):
        data = dashboard.collect(self.cfg, self.store)
        self.assertNotIn("http-equiv", dashboard.render(data))
        self.assertIn('content="30"', dashboard.render(data, refresh_seconds=30))

    def test_write_creates_file(self):
        path = dashboard.write(self.cfg, self.store)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 5000)
        self.assertIn("Citadel", path.read_text(encoding="utf-8"))

    def test_write_dex_mode_uses_own_name(self):
        path = dashboard.write(self.cfg, self.store, mode="dex")
        self.assertEqual(path.name, "dashboard-dex.html")
        self.assertIn("Citadel DEX", path.read_text(encoding="utf-8"))

    def test_escapes_hostile_symbol_names(self):
        self.store.log_trade('<script>alert(1)</script>', "buy", 1, 1, 1, 0, 0, "entry", False)
        html = dashboard.render(dashboard.collect(self.cfg, self.store))
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
