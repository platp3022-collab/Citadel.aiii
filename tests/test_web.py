# -*- coding: utf-8 -*-
"""Тесты локальной веб-панели: доступ по токену, состояние, запуск команд."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from citadel.config import Config
from citadel.genome import Genome
from citadel.storage import Storage
from citadel.web.server import COMMANDS, EDITABLE, Panel, Runner, serve


class TestRunner(unittest.TestCase):
    def test_log_buffer_is_incremental(self):
        r = Runner()
        r.emit("одна\nдве")
        lines, cursor = r.tail(0)
        self.assertEqual(lines, ["одна", "две"])
        r.emit("три")
        fresh, _ = r.tail(cursor)
        self.assertEqual(fresh, ["три"])

    def test_log_buffer_is_bounded(self):
        r = Runner(limit=10)
        for i in range(50):
            r.emit(str(i))
        lines, _ = r.tail(0)
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[-1], "49")

    def test_unknown_command_is_refused(self):
        r = Runner()
        self.assertIn("неизвестная команда", r.start("cex", "rm -rf /", [], [], {}))
        self.assertFalse(r.running)

    def test_command_whitelist_has_no_shell(self):
        for mode, table in COMMANDS.items():
            for cmd, args in table.items():
                for a in args:
                    self.assertFalse(any(c in a for c in ";|&$`"), f"{mode}/{cmd}: {a}")

    def test_real_command_runs_and_logs(self):
        r = Runner()
        self.assertEqual(r.start("cex", "report", ["--dry", "--offline"], [], {}), "")
        for _ in range(100):
            if not r.running:
                break
            time.sleep(0.1)
        lines, _ = r.tail(0)
        self.assertTrue(lines[0].startswith("$ python tradebot.py --dry --offline report"))
        self.assertTrue(any("завершена" in x for x in lines))

    def test_parser_flags_go_before_subcommand(self):
        """--dry/--offline argparse принимает только до команды, --live — после."""
        r = Runner()
        r.start("cex", "trade", ["--dry"], ["--live", "--yes"], {})
        r.stop()
        self.assertEqual(r.command, "tradebot.py --dry trade --live --yes")


class TestPanelState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(symbols=("BTC/USDT",),
                          db_path=str(Path(self.tmp.name) / "t.db"),
                          cache_dir=str(Path(self.tmp.name) / "c"))
        store = Storage(self.cfg.db_path)
        sid = store.save_strategy("BTC/USDT", "1h", Genome(entry=("rsi14_over_50",)), 1.5,
                                  {"valid": {"n_trades": 9, "net_return": 0.1}})
        store.activate(sid, "BTC/USDT")
        store.set("paper_cash", 1234.5)
        store.set("paper_start", 1000.0)
        store.log_trade("BTC/USDT", "sell", 1.0, 100.0, 100.0, 0.1, 12.3, "take", False)
        store.log_equity(1234.5, 1234.5)
        store.close()
        self.panel = Panel("cex")
        self.panel.overrides = {"CITADEL_DB_PATH": self.cfg.db_path,
                                "CITADEL_CACHE_DIR": self.cfg.cache_dir,
                                "CITADEL_SYMBOLS": "BTC/USDT"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_reads_bot_database(self):
        s = self.panel.state()
        self.assertAlmostEqual(s["cash"], 1234.5)
        self.assertAlmostEqual(s["pnl_pct"], 23.45, places=2)
        self.assertEqual(s["strategies"][0]["id"], 1)
        self.assertIn("RSI14 > 50", s["strategies"][0]["describe"])
        self.assertEqual(s["strategies"][0]["metrics"]["valid"]["n_trades"], 9)
        self.assertEqual(len(s["trades"]), 1)
        self.assertEqual(len(s["curve"]), 1)
        self.assertFalse(s["running"])

    def test_settings_are_editable_subset(self):
        keys = {row["key"] for row in self.panel.settings()}
        self.assertEqual(keys, set(EDITABLE))
        self.assertNotIn("EXCHANGE_API_KEY", keys)          # ключи биржи через панель не трогаем
        self.assertNotIn("SOLANA_PRIVATE_KEY", keys)

    def test_overrides_reach_config(self):
        self.panel.overrides["CITADEL_TIMEFRAME"] = "4h"
        self.assertEqual(self.panel.config().timeframe, "4h")
        self.assertEqual(self.panel.state()["timeframe"], "4h")

    def test_dex_mode_switches_config_class(self):
        self.panel.mode = "dex"
        cfg = self.panel.config()
        self.assertEqual(cfg.quote, "USD")
        self.assertTrue(hasattr(cfg, "chain"))


class TestChartData(unittest.TestCase):
    """Данные для живого графика входов и выходов."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(symbols=("BTC/USDT", "ETH/USDT"),
                          db_path=str(Path(self.tmp.name) / "t.db"),
                          cache_dir=str(Path(self.tmp.name) / "c"))
        store = Storage(self.cfg.db_path)
        sid = store.save_strategy("BTC/USDT", "1h", Genome(entry=("rsi14_over_50",)), 1.2, {})
        store.activate(sid, "BTC/USDT")
        now = int(time.time())
        for i, (side, price, pnl, ts) in enumerate([
                ("buy", 100.0, 0.0, now - 5000), ("sell", 110.0, 9.0, now - 4000),
                ("buy", 105.0, 0.0, now - 3000), ("sell", 99.0, -6.0, now - 2000)]):
            store.log_trade("BTC/USDT", side, 1.0, price, price, 0.1, pnl, "take", False)
            store.db.execute("UPDATE trades SET ts=? WHERE id=?", (ts, i + 1))
        store.log_trade("ETH/USDT", "buy", 2.0, 50.0, 100.0, 0.1, 0.0, "entry", False)
        store.upsert_position("BTC/USDT", qty=1.0, entry_price=120.0, entry_fee=0.1, stop=110.0,
                              take=140.0, trail=0.0, peak=125.0, opened_at=now - 1000,
                              opened_bar=0, bars=3, strategy_id=sid)
        store.set("prices", {"BTC/USDT": [123.45, now]})
        store.db.commit()
        store.close()
        from citadel import candlecache
        rows = [[(now - (200 - i) * 3600) * 1000, 100, 105, 95, 100 + i, 1.0] for i in range(200)]
        candlecache.write(candlecache.path_for(self.cfg.cache_dir, self.cfg.exchange,
                                               "BTC/USDT", "1h"), rows)
        self.panel = Panel("cex")
        self.panel.overrides = {"CITADEL_DB_PATH": self.cfg.db_path,
                                "CITADEL_CACHE_DIR": self.cfg.cache_dir,
                                "CITADEL_SYMBOLS": "BTC/USDT,ETH/USDT"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_chart_has_everything_the_page_draws(self):
        d = self.panel.chart("BTC/USDT")
        self.assertEqual(d["symbol"], "BTC/USDT")
        self.assertEqual(d["symbols"], ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(len(d["candles"]), 200)
        self.assertEqual(len(d["trades"]), 4)
        self.assertEqual(d["position"]["entry"], 120.0)
        self.assertEqual(d["position"]["stop"], 110.0)
        self.assertAlmostEqual(d["price"], 123.45)      # живая цена, а не закрытие свечи
        self.assertEqual(d["strategy"]["id"], 1)

    def test_trades_are_sorted_for_pairing(self):
        trades = self.panel.chart("BTC/USDT")["trades"]
        self.assertEqual([t["side"] for t in trades], ["buy", "sell", "buy", "sell"])
        self.assertEqual(trades, sorted(trades, key=lambda t: t["ts"]))
        self.assertLess(trades[3]["pnl"], 0)            # вторая сделка убыточная

    def test_bars_limit_is_respected(self):
        self.assertEqual(len(self.panel.chart("BTC/USDT", bars=50)["candles"]), 50)

    def test_unknown_symbol_falls_back_to_first(self):
        self.assertEqual(self.panel.chart("НЕТ-ТАКОГО")["symbol"], "BTC/USDT")

    def test_symbol_without_candles_still_answers(self):
        d = self.panel.chart("ETH/USDT")
        self.assertEqual(d["candles"], [])
        self.assertEqual(len(d["trades"]), 1)
        self.assertIsNone(d["position"])
        self.assertIsNone(d["strategy"])

    def test_no_symbols_at_all(self):
        self.panel.overrides["CITADEL_SYMBOLS"] = "ZZZ/USDT"
        d = self.panel.chart("")
        self.assertEqual(d["symbol"], "ZZZ/USDT")
        self.assertEqual(d["candles"], [])


class TestHttpApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.url = serve(port=0, mode="cex", allow_live=False)
        cls.token = cls.url.split("token=")[1]
        cls.base = cls.url.split("/?")[0]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, path, body=None, token=None):
        token = self.token if token is None else token
        sep = "&" if "?" in path else "?"
        url = urllib.parse.quote(f"{self.base}{path}{sep}token={token}", safe=":/?=&")
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_requires_token(self):
        code, data = self.call("/api/state", token="неправильный")
        self.assertEqual(code, 403)
        self.assertIn("нет доступа", data["error"])

    def test_page_without_token_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/", timeout=10)
        self.assertEqual(ctx.exception.code, 403)

    def test_page_with_token_has_no_placeholder_left(self):
        with urllib.request.urlopen(f"{self.base}/?token={self.token}", timeout=10) as r:
            html = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertNotIn("__TOKEN__", html)
        self.assertIn(self.token, html)
        self.assertIn("Citadel", html)

    def test_chart_endpoint(self):
        code, data = self.call("/api/chart?bars=30")
        self.assertEqual(code, 200)
        self.assertIn("candles", data)
        self.assertIn("symbols", data)

    def test_state_and_log_endpoints(self):
        code, state = self.call("/api/state")
        self.assertEqual(code, 200)
        self.assertIn("equity", state)
        code, logs = self.call("/api/log?since=0")
        self.assertEqual(code, 200)
        self.assertIn("lines", logs)

    def test_live_trading_blocked_without_flag(self):
        code, data = self.call("/api/run", {"cmd": "trade", "live": True})
        self.assertEqual(code, 403)
        self.assertIn("--allow-live", data["error"])

    def test_unknown_command_and_mode_are_refused(self):
        _, data = self.call("/api/run", {"cmd": "sudo"})
        self.assertIn("неизвестная команда", data["error"])
        code, data = self.call("/api/run", {"cmd": "report", "mode": "хакер"})
        self.assertEqual(code, 400)

    def test_settings_roundtrip_ignores_unknown_keys(self):
        _, data = self.call("/api/settings",
                            {"settings": {"CITADEL_TIMEFRAME": "4h", "PATH": "/зло"}})
        values = {row["key"]: row["value"] for row in data["settings"]}
        self.assertEqual(values["CITADEL_TIMEFRAME"], "4h")
        self.assertNotIn("PATH", values)

    def test_mode_switch(self):
        _, data = self.call("/api/mode", {"mode": "dex"})
        self.assertEqual(data["mode"], "dex")
        self.call("/api/mode", {"mode": "cex"})

    def test_unknown_path(self):
        code, _ = self.call("/api/секрет")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
