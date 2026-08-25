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
from citadel.web.feed import Feed, tf_seconds
from citadel.web.stream import BinanceStream, supports as stream_supports
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


class TestLivePrices(unittest.TestCase):
    """Собственный опрос цен: панель показывает живую цену и без запущенного бота."""

    def setUp(self):
        self.panel = Panel("cex")

    def test_ticks_accumulate_and_filter_by_time(self):
        self.panel.push_ticks({"BTC/USDT": 100.0})
        self.panel.push_ticks({"BTC/USDT": 101.0})
        tail = self.panel.tick_tail("BTC/USDT")
        self.assertEqual([p for _, p in tail], [100.0, 101.0])
        self.assertEqual(self.panel.tick_tail("BTC/USDT", tail[0][0]), [tail[1]])
        self.assertEqual(self.panel.last_tick("BTC/USDT")[1], 101.0)

    def test_same_price_is_not_duplicated(self):
        for _ in range(10):
            self.panel.push_ticks({"BTC/USDT": 100.0})
        self.assertEqual(len(self.panel.tick_tail("BTC/USDT")), 1)

    def test_buffer_is_bounded(self):
        for i in range(25000):
            self.panel.push_ticks({"BTC/USDT": 100.0 + i})
        self.assertLessEqual(len(self.panel.tick_tail("BTC/USDT")), 20000)

    def test_no_ticks_for_unknown_symbol(self):
        self.assertEqual(self.panel.tick_tail("НЕТ"), [])
        self.assertIsNone(self.panel.last_tick("НЕТ"))

    def test_cex_poller_uses_one_batch_request(self):
        calls = []

        class FakeEx:
            def fetch_tickers(self, symbols):
                calls.append(list(symbols))
                return {s: {"last": 10.0 + i} for i, s in enumerate(symbols)}

        class FakeMarket:
            ex = FakeEx()

        feed = Feed()
        feed._clients["cex"] = FakeMarket()
        prices = feed._cex_prices(Config(), ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(prices, {"BTC/USDT": 10.0, "ETH/USDT": 11.0})
        self.assertEqual(calls, [["BTC/USDT", "ETH/USDT"]])          # один запрос на все пары

    def test_cex_poller_falls_back_to_single_tickers(self):
        class FakeEx:
            def fetch_tickers(self, symbols):
                raise RuntimeError("биржа не умеет пачкой")

        class FakeMarket:
            ex = FakeEx()

            def last_price(self, symbol):
                return 42.0

        feed = Feed()
        feed._clients["cex"] = FakeMarket()
        self.assertEqual(feed._cex_prices(Config(), ["BTC/USDT"]), {"BTC/USDT": 42.0})

    def test_dex_poller_batches_by_chain(self):
        from citadel.dex.dexscreener import Pair

        calls = []

        class FakeScreener:
            def pairs(self, chain, pools):
                calls.append((chain, list(pools)))
                return [Pair(chain=chain, dex="raydium", pair_address=pool, base_symbol="X",
                             base_address="M", quote_symbol="SOL", quote_address="S",
                             price_usd=1.5) for pool in pools]

        feed = Feed()
        feed._clients["screener"] = FakeScreener()
        prices = feed._dex_prices(["solana:P1", "solana:P2", "base:P3"])
        self.assertEqual(prices["solana:P1"], 1.5)
        self.assertEqual(prices["base:P3"], 1.5)
        self.assertEqual(sorted(c[0] for c in calls), ["base", "solana"])   # по одному на сеть
        self.assertEqual(len(calls), 2)


class TestTradeStream(unittest.TestCase):
    """Поток сделок Binance: разбор сообщений и аккуратное поведение без aiohttp."""

    def test_supported_only_for_exchange_mode(self):
        self.assertTrue(stream_supports("binance", "cex"))
        self.assertFalse(stream_supports("bybit", "cex"))
        self.assertFalse(stream_supports("binance", "dex"))

    def test_trade_message_becomes_tick(self):
        feed = Feed()
        st = BinanceStream(feed, ["BTC/USDT", "ETH/USDT"])
        back = {"btcusdt": "BTC/USDT", "ethusdt": "ETH/USDT"}
        st._handle('{"stream":"btcusdt@trade","data":{"s":"BTCUSDT","p":"64123.45"}}', back)
        st._handle('{"stream":"ethusdt@trade","data":{"s":"ETHUSDT","p":"3210.5"}}', back)
        self.assertAlmostEqual(feed.last("BTC/USDT")[1], 64123.45)
        self.assertAlmostEqual(feed.last("ETH/USDT")[1], 3210.5)
        self.assertEqual(st.messages, 2)

    def test_garbage_is_ignored(self):
        feed = Feed()
        st = BinanceStream(feed, ["BTC/USDT"])
        for bad in ("не json", "{}", '{"data":{}}', '{"stream":"x@trade","data":{"p":null}}'):
            st._handle(bad, {"btcusdt": "BTC/USDT"})
        self.assertEqual(st.messages, 0)
        self.assertIsNone(feed.last("BTC/USDT"))

    def test_thread_is_alive_check_works(self):
        """Поле stop_event не должно перекрывать Thread._stop — иначе падает is_alive()."""
        st = BinanceStream(Feed(), ["BTC/USDT"])
        self.assertFalse(st.is_alive())
        st.start()
        st.join(timeout=5)
        self.assertFalse(st.is_alive())          # без aiohttp поток корректно завершается

    def test_alive_requires_recent_messages(self):
        st = BinanceStream(Feed(), ["BTC/USDT"])
        st.connected = True
        st.last_msg = time.time()
        self.assertTrue(st.alive())
        st.last_msg = time.time() - 120
        self.assertFalse(st.alive())             # тишина минуту — считаем мёртвым


class TestPollTempo(unittest.TestCase):
    """Темп опроса подстраивается под то, что смотрят."""

    def setUp(self):
        self.panel = Panel("cex")
        self.panel.base_interval = 4.0

    def test_default_and_seconds(self):
        self.assertEqual(self.panel.poll_interval(), 4.0)
        self.panel.focus_tf = "1s"
        self.assertLessEqual(self.panel.poll_interval(), 1.5)

    def test_live_stream_slows_polling_down(self):
        st = BinanceStream(Feed(), ["BTC/USDT"])
        st.connected = True
        st.last_msg = time.time()
        self.panel.stream = st
        self.panel.focus_tf = "1s"
        self.assertGreaterEqual(self.panel.poll_interval(), 10.0)


class TestCandleFeed(unittest.TestCase):
    """Свечи: секундные собираются из тиков, минутные и старше берутся с рынка."""

    def setUp(self):
        self.feed = Feed()
        base = int(time.time() * 1000) // 1000 * 1000
        # 60 секунд цен по 4 тика в секунду, пила 100→104
        for i in range(240):
            self.feed.push({"X": 100.0 + (i % 5)})
            self.feed.ticks["X"][-1] = (base + i * 250, 100.0 + (i % 5))

    def test_tf_seconds(self):
        self.assertEqual(tf_seconds("1s"), 1)
        self.assertEqual(tf_seconds("15s"), 15)
        self.assertEqual(tf_seconds("5m"), 300)
        self.assertEqual(tf_seconds("4h"), 14400)
        self.assertEqual(tf_seconds("1d"), 86400)

    def test_tick_candles_have_correct_ohlc(self):
        candles = self.feed.tick_candles("X", 1)
        self.assertGreater(len(candles), 30)
        for ts, o, h, l, c, v in candles:
            self.assertLessEqual(l, o)
            self.assertLessEqual(l, c)
            self.assertGreaterEqual(h, o)
            self.assertGreaterEqual(h, c)
            self.assertEqual(v, 0.0)                  # объёма у тиковой свечи нет
            self.assertEqual(ts % 1000, 0)            # секунды выровнены

    def test_bigger_timeframe_has_fewer_candles(self):
        one = self.feed.tick_candles("X", 1)
        five = self.feed.tick_candles("X", 5)
        self.assertGreater(len(one), len(five))
        self.assertLessEqual(abs(len(one) / 5 - len(five)), 2)

    def test_tick_candles_limit(self):
        self.assertEqual(len(self.feed.tick_candles("X", 1, limit=10)), 10)

    def test_no_ticks_no_candles(self):
        self.assertEqual(self.feed.tick_candles("НЕТ", 1), [])

    def test_market_candles_fall_back_to_bot_cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(cache_dir=str(Path(tmp.name)), timeframe="1h", exchange="binance")
        from citadel import candlecache
        rows = [[i * 3600_000, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(50)]
        candlecache.write(candlecache.path_for(cfg.cache_dir, "binance", "BTC/USDT", "1h"), rows)
        feed = Feed()
        feed._clients["cex"] = type("Dead", (), {"ex": None})()      # сети нет
        candles, source = feed.market_candles(cfg, "cex", "BTC/USDT", "1h", 30)
        self.assertEqual(source, "кэш")
        self.assertEqual(len(candles), 30)

    def test_market_candles_use_exchange_when_available(self):
        class FakeEx:
            def fetch_ohlcv(self, symbol, tf, limit=None):
                return [[i * 60_000, 1.0, 2.0, 0.5, 1.5, 3.0] for i in range(limit or 5)]

        feed = Feed()
        feed._clients["cex"] = type("M", (), {"ex": FakeEx()})()
        candles, source = feed.market_candles(Config(), "cex", "BTC/USDT", "1m", 20)
        self.assertEqual(source, "рынок")
        self.assertEqual(len(candles), 20)
        self.assertEqual(len(candles[0]), 6)                          # с объёмом

    def test_failed_market_request_is_not_repeated_at_once(self):
        """Панель не должна ждать сеть на каждом запросе, если рынок только что молчал."""
        calls = []

        class DeadEx:
            def fetch_ohlcv(self, symbol, tf, limit=None):
                calls.append(tf)
                raise RuntimeError("сеть недоступна")

        feed = Feed()
        feed._clients["cex"] = type("M", (), {"ex": DeadEx()})()
        for _ in range(5):
            candles, source = feed.market_candles(Config(), "cex", "BTC/USDT", "1m", 10)
            self.assertEqual(source, "кэш")
        self.assertEqual(len(calls), 1)                       # одна попытка, потом пауза

    def test_market_candles_are_cached_briefly(self):
        calls = []

        class FakeEx:
            def fetch_ohlcv(self, symbol, tf, limit=None):
                calls.append(tf)
                return [[i * 60_000, 1.0, 2.0, 0.5, 1.5, 3.0] for i in range(5)]

        feed = Feed(ttl=60)
        feed._clients["cex"] = type("M", (), {"ex": FakeEx()})()
        for _ in range(4):
            feed.market_candles(Config(), "cex", "BTC/USDT", "1m", 5)
        self.assertEqual(len(calls), 1)                               # один запрос на все


class TestWatchOnlyPools(unittest.TestCase):
    """Монету из ленты можно посмотреть, не беря в работу, а потом взять."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.panel = Panel("dex")
        self.panel.overrides = {"CITADEL_SYMBOLS": "solana:POOL1",
                                "CITADEL_DB_PATH": str(Path(self.tmp.name) / "d.db"),
                                "CITADEL_CACHE_DIR": str(Path(self.tmp.name) / "c")}
        self.panel.extra_pairs["solana:NEWPOOL"] = {
            "chain": "solana", "dex": "raydium", "pair_address": "NEWPOOL",
            "base_symbol": "SOLDOG", "base_address": "M", "quote_symbol": "SOL",
            "quote_address": "S", "price_usd": 1.5, "liquidity_usd": 1.2e6,
            "volume_h24": 4.1e6, "volume_h1": 0.0, "txns_h24_buys": 7200,
            "txns_h24_sells": 6800, "price_change_h24": 0.0, "fdv": 1e7,
            "created_at_ms": 0, "url": "", "socials": [], "websites": []}
        self.panel.feed._fail_until[("dex", "solana:NEWPOOL", "15m", 220)] = time.time() + 60

    def tearDown(self):
        self.tmp.cleanup()

    def test_chart_shows_pool_that_is_not_in_work(self):
        d = self.panel.chart("solana:NEWPOOL", 220, "15m")
        self.assertEqual(d["symbol"], "solana:NEWPOOL")
        self.assertTrue(d["watch_only"])
        self.assertIn("solana:NEWPOOL", d["symbols"])
        self.assertIn("solana:POOL1", d["symbols"])          # рабочие пары остались
        self.assertEqual(d["labels"]["solana:NEWPOOL"], "SOLDOG/SOL")

    def test_watching_sets_focus_for_price_polling(self):
        self.panel.chart("solana:NEWPOOL", 50, "1m")
        self.assertEqual(self.panel.focus_symbol, "solana:NEWPOOL")

    def test_taking_into_work_updates_universe_and_pairs(self):
        res = self.panel.add_to_universe("solana:NEWPOOL")
        self.assertTrue(res.get("ok"))
        self.assertIn("solana:NEWPOOL", res["universe"])
        store = Storage(self.panel.config().db_path)
        self.assertIn("solana:NEWPOOL", store.get("universe"))
        store.close()
        pairs = self.panel._read_pairs_file(self.panel.config())
        self.assertEqual(pairs["solana:NEWPOOL"]["base_symbol"], "SOLDOG")
        d = self.panel.chart("solana:NEWPOOL", 50, "15m")
        self.assertFalse(d["watch_only"])                    # теперь она рабочая

    def test_unknown_pool_is_refused(self):
        self.assertIn("error", self.panel.add_to_universe("solana:НЕИЗВЕСТНЫЙ"))
        self.assertIn("error", self.panel.add_to_universe("мусор"))

    def test_new_coins_only_in_dex_mode(self):
        self.assertIn("error", Panel("cex").new_coins())


class TestEmbeddedCharts(unittest.TestCase):
    """Настоящие графики сайтов: TradingView для биржи, DexScreener/GeckoTerminal для пула."""

    def test_exchange_gets_tradingview_with_right_ticker(self):
        panel = Panel("cex")
        panel.overrides = {"CITADEL_SYMBOLS": "BTC/USDT,ETH/USDT",
                           "CITADEL_EXCHANGE": "bybit", "CITADEL_TIMEFRAME": "4h"}
        views = panel.embeds()["views"]
        self.assertEqual([v["id"] for v in views], ["tradingview"])
        self.assertIn("BYBIT%3ABTCUSDT", views[0]["url"])
        self.assertIn("interval=240", views[0]["url"])           # 4h
        self.assertTrue(views[0]["site"].startswith("https://www.tradingview.com/chart/"))

    def test_exchange_alias_is_translated(self):
        panel = Panel("cex")
        panel.overrides = {"CITADEL_SYMBOLS": "BTC/USDT", "CITADEL_EXCHANGE": "gate"}
        self.assertIn("GATEIO%3ABTCUSDT", panel.embeds()["views"][0]["url"])

    def test_dex_gets_pool_charts(self):
        panel = Panel("dex")
        panel.overrides = {"CITADEL_SYMBOLS": "solana:POOL1"}
        ids = [v["id"] for v in panel.embeds()["views"]]
        self.assertIn("dexscreener", ids)
        self.assertIn("geckoterminal", ids)
        urls = {v["id"]: v["url"] for v in panel.embeds()["views"]}
        self.assertIn("dexscreener.com/solana/POOL1", urls["dexscreener"])
        self.assertIn("embed=1", urls["dexscreener"])
        self.assertIn("geckoterminal.com/solana/pools/POOL1", urls["geckoterminal"])

    def test_unknown_chain_still_gives_dexscreener(self):
        panel = Panel("dex")
        panel.overrides = {"CITADEL_SYMBOLS": "какая-то-сеть:POOL9"}
        ids = [v["id"] for v in panel.embeds()["views"]]
        self.assertIn("dexscreener", ids)
        self.assertNotIn("geckoterminal", ids)                   # сети нет в GeckoTerminal

    def test_no_symbols_no_views(self):
        panel = Panel("cex")
        panel.overrides = {"CITADEL_SYMBOLS": " "}
        self.assertEqual(Panel("cex").embeds("")["views"][0]["id"], "tradingview")

    def test_chart_payload_carries_embeds(self):
        panel = Panel("cex")
        panel.overrides = {"CITADEL_SYMBOLS": "BTC/USDT"}
        self.assertIn("embeds", panel.chart("BTC/USDT"))


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

    def test_chart_carries_live_ticks(self):
        self.panel.push_ticks({"BTC/USDT": 200.0})
        d = self.panel.chart("BTC/USDT")
        self.assertEqual(len(d["ticks"]), 1)
        self.assertAlmostEqual(d["price"], 200.0)          # тик свежее записи бота
        self.assertIn("live_prices", d)

    def test_seconds_timeframe_is_built_from_ticks(self):
        for i in range(60):
            self.panel.push_ticks({"BTC/USDT": 100.0 + (i % 4)})
        d = self.panel.chart("BTC/USDT", tf="1s")
        self.assertEqual(d["source"], "тики")
        self.assertEqual(d["timeframe"], "1s")
        self.assertEqual(d["tf_seconds"], 1)
        self.assertGreaterEqual(len(d["candles"]), 1)
        for candle in d["candles"]:
            self.assertEqual(len(candle), 6)                   # ts + OHLC + объём

    def test_minute_timeframe_falls_back_to_cache(self):
        d = self.panel.chart("BTC/USDT", tf="1h")
        self.assertEqual(d["source"], "кэш")                   # рынка в тестах нет
        self.assertEqual(d["timeframe"], "1h")
        self.assertGreater(len(d["candles"]), 100)

    def test_fresh_trades_are_reported_for_instant_markers(self):
        store = Storage(self.cfg.db_path)
        before = store.last_trade_id()
        store.log_trade("BTC/USDT", "buy", 1.0, 150.0, 150.0, 0.1, 0.0, "entry", False)
        store.close()
        upd = self.panel.ticks_update("BTC/USDT", 0, before)
        self.assertEqual(len(upd["trades"]), 1)
        self.assertEqual(upd["trades"][0]["side"], "buy")
        self.assertGreater(upd["last_trade_id"], before)
        self.assertIn("position", upd)
        self.assertIn("stream", upd)

    def test_no_new_trades_no_noise(self):
        store = Storage(self.cfg.db_path)
        last = store.last_trade_id()
        store.close()
        upd = self.panel.ticks_update("BTC/USDT", 0, last)
        self.assertEqual(upd["trades"], [])

    def test_chart_reports_why_the_bot_is_waiting(self):
        """Панель должна показывать, какие условия входа выполнены прямо сейчас."""
        d = self.panel.chart("BTC/USDT", tf="1h")
        st = d["strategy"]
        self.assertIsNotNone(st)
        self.assertEqual([c["name"] for c in st["entry"]], ["rsi14_over_50"])
        self.assertEqual(st["entry"][0]["title"], "RSI14 > 50")
        self.assertIn(st["entry"][0]["ok"], (True, False))
        self.assertIn("bot_running", d)

    def test_chart_counts_trades_per_symbol(self):
        d = self.panel.chart("BTC/USDT", tf="1h")
        self.assertEqual(d["trades_total"].get("BTC/USDT"), 4)
        self.assertEqual(d["trades_total"].get("ETH/USDT"), 1)

    def test_backtest_returns_trades_of_the_active_strategy(self):
        d = self.panel.backtest("BTC/USDT", "1h", 200)
        self.assertNotIn("error", d)
        self.assertIn("summary", d)
        self.assertTrue(all(t["side"] in ("buy", "sell") for t in d["trades"]))
        for t in d["trades"]:
            self.assertGreater(t["ts"], 1_000_000_000_000)      # время в миллисекундах

    def test_backtest_explains_when_it_cannot_run(self):
        self.assertIn("error", self.panel.backtest("ETH/USDT", "1h", 200))   # нет стратегии
        self.assertIn("минуты", self.panel.backtest("BTC/USDT", "1s", 200)["error"])

    def test_payload_lists_timeframes(self):
        d = self.panel.chart("BTC/USDT")
        tfs = [t["tf"] for t in d["timeframes"]]
        self.assertEqual(tfs[:4], ["1s", "5s", "15s", "30s"])
        self.assertIn("1h", tfs)
        self.assertEqual(d["bot_timeframe"], "1h")

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

    def test_ticks_endpoint(self):
        code, data = self.call("/api/ticks?symbol=BTC/USDT&since=0")
        self.assertEqual(code, 200)
        self.assertIn("ticks", data)
        self.assertIn("live", data)

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

    def test_chart_library_is_served_locally(self):
        with urllib.request.urlopen(
                f"{self.base}/vendor/lightweight-charts.standalone.production.js", timeout=10) as r:
            body = r.read()
        self.assertEqual(r.status, 200)                     # без токена: это статика
        self.assertIn("javascript", r.headers["Content-Type"])
        self.assertGreater(len(body), 100_000)
        self.assertIn(b"LightweightCharts", body)

    def test_vendor_path_cannot_escape(self):
        for bad in ("/vendor/../server.py", "/vendor/nope.js", "/vendor/"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.base + bad, timeout=10)
            self.assertEqual(ctx.exception.code, 404, bad)

    def test_page_loads_the_library(self):
        with urllib.request.urlopen(f"{self.base}/?token={self.token}", timeout=10) as r:
            html = r.read().decode()
        self.assertIn("/vendor/lightweight-charts.standalone.production.js", html)
        self.assertNotIn("https://unpkg.com", html)         # ничего из интернета

    def test_browser_remembers_the_token_in_a_cookie(self):
        """Открыл с ключом — дальше панель работает и по короткому адресу."""
        import http.cookiejar

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with opener.open(f"{self.base}/?token={self.token}", timeout=10) as r:
            self.assertEqual(r.status, 200)
        self.assertTrue(any(c.name == "citadel_token" for c in jar),
                        "панель не оставила ключ браузеру")
        with opener.open(f"{self.base}/", timeout=10) as r:      # уже без ?token=
            self.assertEqual(r.status, 200)
            self.assertIn("Citadel", r.read().decode())
        with opener.open(f"{self.base}/api/state", timeout=10) as r:
            self.assertEqual(r.status, 200)

    def test_locked_page_explains_what_to_do(self):
        try:
            urllib.request.urlopen(f"{self.base}/", timeout=10)
            self.fail("страница отдалась без ключа")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)
            body = e.read().decode()
        self.assertIn("token=", body)
        self.assertIn("окно", body)
        self.assertNotIn(self.token, body)            # сам ключ на странице не печатаем

    def test_cookie_with_wrong_value_is_refused(self):
        req = urllib.request.Request(f"{self.base}/api/state",
                                     headers={"Cookie": "citadel_token=poddelka"})   # заголовки — только ascii
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)

    def test_broken_cookie_header_does_not_crash(self):
        req = urllib.request.Request(f"{self.base}/api/state",
                                     headers={"Cookie": "=;; broken=\\"}) 
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)

    def test_unknown_path(self):
        code, _ = self.call("/api/секрет")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
