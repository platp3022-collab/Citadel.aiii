# -*- coding: utf-8 -*-
"""Тесты DEX-версии: API-клиенты, фильтры безопасности, влияние на цену, торговый цикл."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from citadel.dex.bot import DexTrader
from citadel.dex.broker import DexPaperBroker, effective_slippage_bps, price_impact
from citadel.dex.config import DexConfig
from citadel.dex.dexscreener import DexScreener, Pair, parse_pair
from citadel.dex.geckoterminal import GeckoTerminal, TIMEFRAMES, network_of
from citadel.dex.http import ApiError
from citadel.dex.market import DexMarket, split_key
from citadel.dex.safety import SafetyLimits, check_pair
from citadel.features import Candles
from citadel.genome import Genome
from citadel.notify import Notifier
from citadel.storage import Storage

from .synth import make_candles

NOW_MS = int(time.time() * 1000)


def raw_pair(symbol="WIF", pool="POOL1", liq=1_250_000.0, vol=2_000_000.0,
             buys=5100, sells=4800, age_days=30.0, chain="solana", fdv=50_000_000.0,
             price=2.34) -> dict:
    return {
        "chainId": chain, "dexId": "raydium", "pairAddress": pool,
        "baseToken": {"address": f"MINT_{symbol}", "symbol": symbol, "name": symbol},
        "quoteToken": {"address": "So111", "symbol": "SOL"},
        "priceUsd": str(price), "liquidity": {"usd": liq},
        "volume": {"h24": vol, "h1": vol / 24}, "txns": {"h24": {"buys": buys, "sells": sells}},
        "priceChange": {"h24": 3.2}, "fdv": fdv,
        "pairCreatedAt": NOW_MS - int(age_days * 86400_000),
        "url": f"https://dexscreener.com/{chain}/{pool}",
        "info": {"socials": [{"type": "twitter", "url": "https://x.com/t"}], "websites": []},
    }


class FakeHttp:
    """Подменяет HttpClient: отдаёт заготовленные ответы и считает запросы."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def get_json(self, url: str, params: dict | None = None) -> dict:
        key = url
        if params:
            key += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        self.calls.append(key)
        for pattern, value in self.responses.items():
            if pattern in key:
                return value(params) if callable(value) else value
        raise ApiError(f"нет заготовки для {key}")

    def post_json(self, url: str, payload: dict) -> dict:
        return self.get_json(url)


class TestDexScreener(unittest.TestCase):
    def test_parse(self):
        p = parse_pair(raw_pair())
        self.assertEqual(p.key, "solana:POOL1")
        self.assertEqual(p.name, "WIF/SOL")
        self.assertAlmostEqual(p.price_usd, 2.34)
        self.assertAlmostEqual(p.age_hours, 30 * 24, places=0)
        self.assertEqual(p.socials, ["https://x.com/t"])

    def test_parse_rejects_garbage(self):
        self.assertIsNone(parse_pair({}))
        self.assertIsNone(parse_pair({"pairAddress": "P"}))            # нет baseToken
        p = parse_pair({**raw_pair(), "priceUsd": None, "liquidity": {}})
        self.assertEqual(p.price_usd, 0.0)
        self.assertEqual(p.liquidity_usd, 0.0)

    def test_search_and_pairs(self):
        http = FakeHttp({"latest/dex/search": {"pairs": [raw_pair(), raw_pair("BONK", "POOL2")]},
                         "latest/dex/pairs": {"pairs": [raw_pair()]}})
        ds = DexScreener(http)
        found = ds.search("SOL")
        self.assertEqual([p.base_symbol for p in found], ["WIF", "BONK"])
        self.assertEqual(ds.pair("solana", "POOL1").key, "solana:POOL1")

    def test_pairs_chunks_requests(self):
        http = FakeHttp({"latest/dex/pairs": {"pairs": [raw_pair()]}})
        DexScreener(http).pairs("solana", [f"P{i}" for i in range(65)])
        self.assertEqual(len(http.calls), 3)                            # 30 + 30 + 5


class TestGeckoTerminal(unittest.TestCase):
    def test_ohlcv_sorted_and_in_ms(self):
        base = int(time.time()) - 100 * 900
        rows = [[base + i * 900, 1.0 + i, 2.0, 0.5, 1.5, 1000.0] for i in range(100)]
        http = FakeHttp({"ohlcv": {"data": {"attributes": {"ohlcv_list": list(reversed(rows))}}}})
        out = GeckoTerminal(http).ohlcv("solana", "POOL1", "15m", limit=50)
        self.assertEqual(len(out), 50)
        self.assertEqual(out, sorted(out, key=lambda r: r[0]))
        self.assertEqual(out[0][0], out[0][0] // 1000 * 1000)
        self.assertEqual(out[-1][0], rows[-1][0] * 1000)

    def test_ohlcv_stops_on_empty(self):
        http = FakeHttp({"ohlcv": {"data": {"attributes": {"ohlcv_list": []}}}})
        self.assertEqual(GeckoTerminal(http).ohlcv("solana", "P", "1h", limit=500), [])

    def test_unknown_timeframe_and_network(self):
        gt = GeckoTerminal(FakeHttp({}))
        with self.assertRaises(ApiError):
            gt.ohlcv("solana", "P", "3m", limit=10)
        with self.assertRaises(ApiError):
            network_of("несуществующая-сеть")
        self.assertEqual(network_of("polygon"), "polygon_pos")
        self.assertIn("15m", TIMEFRAMES)


class TestSafety(unittest.TestCase):
    def setUp(self):
        self.limits = SafetyLimits()

    def test_healthy_pair_passes(self):
        self.assertEqual(check_pair(parse_pair(raw_pair()), self.limits), [])

    def test_thin_liquidity_rejected(self):
        bad = check_pair(parse_pair(raw_pair(liq=5_000, vol=200_000)), self.limits)
        self.assertTrue(any("ликвидность" in r for r in bad))

    def test_fresh_pool_rejected(self):
        bad = check_pair(parse_pair(raw_pair(age_days=0.5)), self.limits)
        self.assertTrue(any("младше" in r for r in bad))

    def test_no_sellers_rejected(self):
        bad = check_pair(parse_pair(raw_pair(buys=900, sells=0)), self.limits)
        self.assertTrue(any("не выпускают" in r for r in bad))
        skewed = check_pair(parse_pair(raw_pair(buys=5000, sells=100)), self.limits)
        self.assertTrue(any("больше продаж" in r for r in skewed))

    def test_wash_trading_rejected(self):
        bad = check_pair(parse_pair(raw_pair(liq=60_000, vol=50_000_000)), self.limits)
        self.assertTrue(any("wash" in r for r in bad))

    def test_dead_pool_rejected(self):
        bad = check_pair(parse_pair(raw_pair(liq=5_000_000, vol=150_000)), self.limits)
        self.assertTrue(any("стоячий" in r for r in bad))


class TestPriceImpact(unittest.TestCase):
    def test_impact_grows_with_size_and_falls_with_liquidity(self):
        self.assertLess(price_impact(100, 1_000_000), price_impact(10_000, 1_000_000))
        self.assertGreater(price_impact(1_000, 100_000), price_impact(1_000, 10_000_000))
        self.assertEqual(price_impact(0, 1_000_000), 0.0)
        self.assertEqual(price_impact(1_000, 0), 0.0)

    def test_one_percent_of_pool_moves_price_about_two_percent(self):
        self.assertAlmostEqual(price_impact(10_000, 1_000_000), 0.0196, places=3)

    def test_effective_slippage_includes_impact(self):
        cfg = DexConfig()
        thin = effective_slippage_bps(cfg, 50_000)
        deep = effective_slippage_bps(cfg, 20_000_000)
        self.assertGreater(thin, deep)
        self.assertGreaterEqual(deep, cfg.slippage_bps)


class FakeDexMarket(DexMarket):
    """DexMarket поверх готовых свечей: без сети, с курсором как в биржевых тестах."""

    def __init__(self, cfg: DexConfig, candles: dict[str, Candles], pairs: dict[str, Pair],
                 cursor: int):
        self.cfg = cfg
        self.offline = False
        self.ex = None
        self.pairs = pairs
        self.candles = candles
        self.cursor = cursor
        self.screener = None
        self.gecko = None

    def save_pairs(self) -> None:
        pass

    def fetch_ohlcv(self, symbol, timeframe, limit, offline=False, use_cache=True) -> Candles:
        c = self.candles[symbol]
        return c.slice(max(0, self.cursor - limit), self.cursor)

    def last_price(self, symbol: str) -> float:
        return self.candles[symbol].close[self.cursor - 1]

    def refresh_pair(self, symbol: str) -> Pair | None:
        return self.pairs.get(symbol)


class TestDexMarketCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = DexConfig(cache_dir=str(Path(self.tmp.name) / "c"),
                             db_path=str(Path(self.tmp.name) / "d.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_split_key(self):
        self.assertEqual(split_key("solana:POOL"), ("solana", "POOL"))
        with self.assertRaises(ValueError):
            split_key("BTCUSDT")

    def test_bad_symbol_is_refused_early(self):
        market = DexMarket(self.cfg, offline=True)
        with self.assertRaises(ValueError) as ctx:
            market.fetch_ohlcv("BTCUSDT", "15m", 100)
        self.assertIn("chain:адрес_пула", str(ctx.exception))

    def test_cli_rejects_symbols_without_pool_address(self):
        from citadel.dex.cli import apply_overrides, build_parser

        args = build_parser().parse_args(["--symbols", "BTC/USDT", "backtest"])
        with self.assertRaises(SystemExit) as ctx:
            apply_overrides(DexConfig(), args)
        self.assertIn("chain:адрес_пула", str(ctx.exception))

    def test_offline_without_cache_explains_itself(self):
        market = DexMarket(self.cfg, offline=True)
        with self.assertRaises(SystemExit) as ctx:
            market.fetch_ohlcv("solana:POOL1", "15m", 100)
        self.assertIn("dexbot.py fetch", str(ctx.exception))

    def test_fetch_writes_cache_and_offline_reads_it(self):
        step = 900
        base = int(time.time()) - 400 * step
        rows = [[base + i * step, 1.0, 2.0, 0.5, 1.5, 100.0] for i in range(400)]
        http = FakeHttp({"ohlcv": {"data": {"attributes": {"ohlcv_list": list(reversed(rows))}}}})
        market = DexMarket(self.cfg, gecko=GeckoTerminal(http), screener=DexScreener(FakeHttp({})))
        got = market.fetch_ohlcv("solana:POOL1", "15m", 300)
        self.assertGreater(len(got), 100)
        self.assertTrue(market.cache_path("solana:POOL1", "15m").exists())
        offline = DexMarket(self.cfg, offline=True)
        self.assertEqual(len(offline.fetch_ohlcv("solana:POOL1", "15m", 300)), len(got))

    def test_pairs_survive_restart(self):
        market = DexMarket(self.cfg, offline=True)
        market.remember(parse_pair(raw_pair()))
        again = DexMarket(self.cfg, offline=True)
        self.assertEqual(again.pair("solana:POOL1").base_symbol, "WIF")
        self.assertIn("WIF/SOL", again.name("solana:POOL1"))
        saved = json.loads(Path(self.cfg.pairs_path).read_text(encoding="utf-8"))
        self.assertIn("solana:POOL1", saved)


class TestDexTrading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = DexConfig(
            chain="solana", timeframe="15m", start_balance=500.0, min_notional=1.0,
            max_positions=1, max_pool_frac=0.01,
            db_path=str(Path(self.tmp.name) / "dex.db"),
            cache_dir=str(Path(self.tmp.name) / "candles"))
        self.store = Storage(self.cfg.db_path)
        self.key = "solana:POOL1"
        self.pairs = {self.key: parse_pair(raw_pair())}
        n = 900
        self.candles = {self.key: make_candles(n, seed=55, drift=0.001, vol=0.02,
                                               start_ts=NOW_MS - n * 900_000, step_ms=900_000)}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _trader(self, cursor: int = 300, genome: Genome | None = None) -> DexTrader:
        market = FakeDexMarket(self.cfg, self.candles, self.pairs, cursor)
        broker = DexPaperBroker(self.cfg, self.store, market)
        self.cfg.symbols = (self.key,)
        trader = DexTrader(self.cfg, self.store, market, broker,
                           Notifier(enabled=False, echo=False))
        if genome:
            sid = self.store.save_strategy(self.key, "15m", genome, 1.0, {})
            self.store.activate(sid, self.key)
            trader._load_strategies()
        return trader

    def test_paper_broker_charges_impact_and_network_fee(self):
        trader = self._trader()
        fill = trader.broker.buy(self.key, 100.0, 1.0)
        self.assertGreater(fill.price, 1.0)                       # покупка дороже котировки
        self.assertGreater(fill.fee, self.cfg.priority_fee_usd)   # комиссия пула + сеть
        sell = trader.broker.sell(self.key, 100.0, 1.0)
        self.assertLess(sell.price, 1.0)                          # продажа дешевле

    def test_broker_never_spends_more_than_cash(self):
        trader = self._trader()
        fill = trader.broker.buy(self.key, 10 ** 9, 1.0)
        self.assertGreaterEqual(trader.broker.cash, 0.0)
        self.assertLessEqual(fill.cost + fill.fee, self.cfg.start_balance + 1e-6)

    def test_size_capped_by_pool_liquidity(self):
        trader = self._trader()
        cap = trader.extra_size_cap(self.key, 2.0)
        self.assertAlmostEqual(cap, 1_250_000 * 0.01 / 2.0)
        self.pairs[self.key] = parse_pair(raw_pair(liq=0.0))
        trader.market.pairs = self.pairs
        self.assertIsNone(trader.extra_size_cap(self.key, 2.0))

    def test_rug_guard_fires_on_liquidity_drop(self):
        trader = self._trader()
        self.store.upsert_position(self.key, qty=10.0, entry_price=1.0, entry_fee=0.0,
                                   stop=0.5, take=0.0, trail=0.0, peak=1.0, opened_at=1,
                                   opened_bar=1, bars=0, strategy_id=None)
        self.store.set(f"liq:{self.key}", 1_250_000.0)
        self.assertIsNone(trader.emergency_exit_reason(self.key))
        self.pairs[self.key] = parse_pair(raw_pair(liq=200_000.0))
        reason = trader.emergency_exit_reason(self.key)
        self.assertIsNotNone(reason)
        self.assertIn("ликвидность упала", reason)

    def test_rug_guard_ignores_flat_book(self):
        trader = self._trader()
        self.pairs[self.key] = parse_pair(raw_pair(liq=1.0))
        self.assertIsNone(trader.emergency_exit_reason(self.key))   # позиции нет — молчим

    def test_pair_config_uses_pool_specific_costs(self):
        trader = self._trader()
        cfg = trader.pair_config(self.key)
        self.assertGreater(cfg.slippage_bps, self.cfg.slippage_bps)
        self.assertEqual(cfg.taker_fee, self.cfg.taker_fee)

    def test_replay_trades_and_money_adds_up(self):
        g = Genome(entry=("ema9_over_ema21", "rsi14_over_50"), exit=("breakdown_dc20",),
                   stop_atr=2.0, take_atr=3.0, trail_atr=0.0, max_hold=48,
                   risk_pct=1.5, cooldown=1)
        trader = self._trader(cursor=300, genome=g)
        for cursor in range(300, 900):
            trader.market.cursor = cursor
            trader.tick()
        trades = self.store.recent_trades(500)
        buys = [t for t in trades if t["side"] == "buy"]
        sells = [t for t in trades if t["side"] == "sell"]
        self.assertGreater(len(buys), 2, "бот не открыл ни одной позиции")
        pnl = sum(t["pnl"] for t in sells)
        locked = sum(p["qty"] * p["entry_price"] + (p["entry_fee"] or 0.0)
                     for p in self.store.all_positions())
        self.assertAlmostEqual(trader.broker.cash + locked,
                               self.cfg.start_balance + pnl, places=6)
        for t in buys:                       # объём каждой сделки в пределах доли пула
            self.assertLessEqual(t["cost"], 1_250_000 * self.cfg.max_pool_frac * 1.01)

    def test_entry_blocked_when_pair_goes_bad(self):
        g = Genome(entry=("always",), exit=(), stop_atr=2.0, take_atr=0.0, trail_atr=0.0,
                   max_hold=100, risk_pct=1.0, cooldown=0)
        self.pairs[self.key] = parse_pair(raw_pair(liq=3_000, vol=5_000))   # пул сдулся
        trader = self._trader(cursor=300, genome=g)
        for cursor in range(300, 340):
            trader.market.cursor = cursor
            trader.tick()
        self.assertEqual(len(self.store.all_positions()), 0)
        self.assertEqual(len(self.store.recent_trades(10)), 0)


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = DexConfig(db_path=str(Path(self.tmp.name) / "d.db"),
                             cache_dir=str(Path(self.tmp.name) / "c"), universe_size=2)
        self.store = Storage(self.cfg.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_rank_prefers_deep_and_active_pools(self):
        big = parse_pair(raw_pair("BIG", "P1", liq=2_000_000, vol=4_000_000))
        small = parse_pair(raw_pair("SMALL", "P2", liq=60_000, vol=90_000))
        self.assertGreater(DexTrader.rank(big), DexTrader.rank(small))

    def test_discover_filters_and_limits_universe(self):
        good = [raw_pair("AAA", "P1", liq=2_000_000, vol=3_000_000),
                raw_pair("BBB", "P2", liq=900_000, vol=1_500_000),
                raw_pair("CCC", "P3", liq=700_000, vol=1_000_000)]
        scam = raw_pair("SCAM", "P4", liq=4_000, vol=900_000, buys=900, sells=2, age_days=0.2)
        http = FakeHttp({"latest/dex/search": {"pairs": good + [scam]},
                         "token-boosts": [], "tokens/v1": []})
        market = DexMarket(self.cfg, screener=DexScreener(http), gecko=GeckoTerminal(FakeHttp({})))
        n = 600
        candles = {f"solana:P{i}": make_candles(n, seed=i, start_ts=NOW_MS - n * 900_000,
                                                step_ms=900_000) for i in range(1, 5)}
        market.fetch_ohlcv = lambda key, tf, limit, **kw: candles[key]      # свечи есть у всех
        trader = DexTrader(self.cfg, self.store, market,
                           DexPaperBroker(self.cfg, self.store, market),
                           Notifier(enabled=False, echo=False))
        trader.rugcheck.check = lambda mint, limits: []
        universe = trader.discover()
        self.assertEqual(len(universe), 2)                    # universe_size
        self.assertNotIn("solana:P4", universe)               # скам отсеян
        self.assertEqual(universe[0], "solana:P1")            # самый крупный первым
        self.assertEqual(self.store.get("universe"), universe)

    def test_failed_feed_keeps_previous_universe(self):
        """Сеть молчит — это не повод терять рабочий список пар."""
        class Dead:
            def get_json(self, url, params=None):
                raise ApiError("сеть недоступна")

        self.cfg.symbols = ("solana:OLD1", "solana:OLD2")
        self.store.set("universe", ["solana:OLD1", "solana:OLD2"])
        market = DexMarket(self.cfg, screener=DexScreener(Dead()), gecko=GeckoTerminal(Dead()))
        trader = DexTrader(self.cfg, self.store, market,
                           DexPaperBroker(self.cfg, self.store, market),
                           Notifier(enabled=False, echo=False))
        universe = trader.discover()
        self.assertEqual(universe, ["solana:OLD1", "solana:OLD2"])
        self.assertEqual(self.store.get("universe"), ["solana:OLD1", "solana:OLD2"])

    def test_nothing_passes_filters_keeps_previous_universe(self):
        scam = raw_pair("SCAM", "P9", liq=1_000, vol=10, buys=5, sells=0, age_days=0.01)
        http = FakeHttp({"latest/dex/search": {"pairs": [scam]}, "token-boosts": [],
                         "tokens/v1": []})
        self.cfg.symbols = ("solana:OLD1",)
        self.store.set("universe", ["solana:OLD1"])
        market = DexMarket(self.cfg, screener=DexScreener(http), gecko=GeckoTerminal(FakeHttp({})))
        trader = DexTrader(self.cfg, self.store, market,
                           DexPaperBroker(self.cfg, self.store, market),
                           Notifier(enabled=False, echo=False))
        trader.rugcheck.check = lambda mint, limits: []
        self.assertEqual(trader.discover(), ["solana:OLD1"])
        self.assertEqual(self.store.get("universe"), ["solana:OLD1"])

    def test_discover_skips_pairs_without_history(self):
        http = FakeHttp({"latest/dex/search": {"pairs": [raw_pair("AAA", "P1")]},
                         "token-boosts": [], "tokens/v1": []})
        market = DexMarket(self.cfg, screener=DexScreener(http), gecko=GeckoTerminal(FakeHttp({})))
        market.fetch_ohlcv = lambda key, tf, limit, **kw: make_candles(
            100, start_ts=NOW_MS - 100 * 900_000, step_ms=900_000)          # слишком короткая
        trader = DexTrader(self.cfg, self.store, market,
                           DexPaperBroker(self.cfg, self.store, market),
                           Notifier(enabled=False, echo=False))
        trader.rugcheck.check = lambda mint, limits: []
        self.assertEqual(trader.discover(), [])


if __name__ == "__main__":
    unittest.main()


class TestVisibility(unittest.TestCase):
    """Сделку должно быть видно: имя пары, ссылка на график, ссылка на транзакцию."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = DexConfig(db_path=str(Path(self.tmp.name) / "d.db"),
                             cache_dir=str(Path(self.tmp.name) / "c"),
                             start_balance=500.0, min_notional=1.0)
        self.store = Storage(self.cfg.db_path)
        self.key = "solana:POOL1"
        self.cfg.symbols = (self.key,)
        n = 500
        candles = {self.key: make_candles(n, seed=7, start_ts=NOW_MS - n * 900_000,
                                          step_ms=900_000)}
        pairs = {self.key: parse_pair(raw_pair())}
        self.market = FakeDexMarket(self.cfg, candles, pairs, 300)
        self.trader = DexTrader(self.cfg, self.store, self.market,
                                DexPaperBroker(self.cfg, self.store, self.market),
                                Notifier(enabled=False, echo=False))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_label_is_human_readable(self):
        self.assertIn("WIF/SOL", self.trader.label(self.key))

    def test_note_has_chart_link(self):
        note = self.trader.trade_note(self.key)
        self.assertIn("dexscreener.com/solana/POOL1", note)
        self.assertIn("график", note)

    def test_note_has_transaction_link_for_live_swap(self):
        from citadel.broker import Fill
        fill = Fill(self.key, "buy", 1.0, 2.0, 2.0, 0.0, "5xTxSignature")
        note = self.trader.trade_note(self.key, fill)
        self.assertIn("solscan.io/tx/5xTxSignature", note)

    def test_paper_fill_has_no_transaction_link(self):
        from citadel.broker import Fill
        note = self.trader.trade_note(self.key, Fill(self.key, "buy", 1.0, 2.0, 2.0, 0.0))
        self.assertNotIn("solscan", note)

    def test_report_lists_chart_links(self):
        text = self.trader.report()
        self.assertIn("Графики:", text)
        self.assertIn("dexscreener.com/solana/POOL1", text)
