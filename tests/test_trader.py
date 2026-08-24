# -*- coding: utf-8 -*-
"""
Тесты торгового цикла: воспроизведение истории бар за баром на бумажном счёте,
поиск стратегии и хранилище.
"""
from __future__ import annotations

import random
import tempfile
import time
import unittest
from pathlib import Path

from citadel.bot import Trader
from citadel.broker import PaperBroker
from citadel.config import Config
from citadel.evolve import evolve, score
from citadel.features import Candles, build_features
from citadel.genome import Genome
from citadel.market import Market
from citadel.notify import Notifier
from citadel.storage import Storage

from .synth import make_candles


class ReplayMarket(Market):
    """Биржа-заглушка: отдаёт историю по одному бару, цена = закрытие текущего бара."""

    def __init__(self, cfg: Config, candles: dict[str, Candles], cursor: int):
        self.cfg = cfg
        self.ex = None
        self.offline = False
        self._markets = {}
        self.candles = candles
        self.cursor = cursor

    def fetch_ohlcv(self, symbol, timeframe, limit, offline=False, use_cache=True) -> Candles:
        c = self.candles[symbol]
        return c.slice(max(0, self.cursor - limit), self.cursor)

    def last_price(self, symbol: str) -> float:
        return self.candles[symbol].close[self.cursor - 1]

    def min_notional(self, symbol: str) -> float:
        return self.cfg.min_notional

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return amount


class TestPaperReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            symbols=("BTC/USDT",), timeframe="1h", start_balance=1000.0,
            min_notional=1.0, max_positions=1,
            db_path=str(Path(self.tmp.name) / "t.db"), cache_dir=str(Path(self.tmp.name) / "c"),
        )
        self.store = Storage(self.cfg.db_path)
        now_ms = int(time.time() * 1000)
        n = 900
        self.candles = {"BTC/USDT": make_candles(n, seed=77, drift=0.0009, vol=0.012,
                                                 start_ts=now_ms - n * 3600000)}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _trader(self, genome: Genome, cursor: int) -> tuple[Trader, ReplayMarket]:
        market = ReplayMarket(self.cfg, self.candles, cursor)
        broker = PaperBroker(self.cfg, self.store, market)
        trader = Trader(self.cfg, self.store, market, broker, Notifier(enabled=False, echo=False))
        sid = self.store.save_strategy("BTC/USDT", "1h", genome, 1.0, {})
        self.store.activate(sid, "BTC/USDT")
        trader._load_strategies()
        return trader, market

    def test_replay_opens_and_closes_positions(self):
        g = Genome(entry=("ema9_over_ema21", "rsi14_over_50"), exit=("breakdown_dc20",),
                   stop_atr=2.0, take_atr=3.0, trail_atr=0.0, max_hold=48,
                   risk_pct=1.0, cooldown=1)
        trader, market = self._trader(g, 300)
        for cursor in range(300, 900):
            market.cursor = cursor
            trader.tick()

        trades = self.store.recent_trades(500)
        buys = [t for t in trades if t["side"] == "buy"]
        sells = [t for t in trades if t["side"] == "sell"]
        self.assertGreater(len(buys), 3, "бот не открыл ни одной позиции")
        self.assertGreaterEqual(len(buys), len(sells))
        self.assertLessEqual(len(buys) - len(sells), 1)     # максимум одна открытая

        # деньги сходятся: кэш + стоимость открытой позиции = старт + сумма P&L
        pnl = sum(t["pnl"] for t in sells)
        locked = sum(p["qty"] * p["entry_price"] + (p["entry_fee"] or 0.0)
                     for p in self.store.all_positions())
        self.assertAlmostEqual(trader.broker.cash + locked, self.cfg.start_balance + pnl, places=6)
        self.assertGreaterEqual(trader.broker.cash, 0.0)

    def test_stop_loss_triggers_in_replay(self):
        g = Genome(entry=("always",), exit=(), stop_atr=0.5, take_atr=0.0, trail_atr=0.0,
                   max_hold=200, risk_pct=1.0, cooldown=0)
        trader, market = self._trader(g, 300)
        for cursor in range(300, 700):
            market.cursor = cursor
            trader.tick()
        reasons = {t["reason"] for t in self.store.recent_trades(500)}
        self.assertIn("stop", reasons)

    def test_max_positions_respected(self):
        cfg = self.cfg
        cfg.symbols = ("BTC/USDT", "ETH/USDT")
        cfg.max_positions = 1
        now_ms = int(time.time() * 1000)
        self.candles["ETH/USDT"] = make_candles(900, seed=99, drift=0.0009,
                                                start_ts=now_ms - 900 * 3600000)
        g = Genome(entry=("always",), exit=(), stop_atr=3.0, take_atr=0.0, trail_atr=0.0,
                   max_hold=500, risk_pct=1.0, cooldown=0)
        market = ReplayMarket(cfg, self.candles, 300)
        broker = PaperBroker(cfg, self.store, market)
        trader = Trader(cfg, self.store, market, broker, Notifier(enabled=False, echo=False))
        for symbol in cfg.symbols:
            sid = self.store.save_strategy(symbol, "1h", g, 1.0, {})
            self.store.activate(sid, symbol)
        trader._load_strategies()
        for cursor in range(300, 420):
            market.cursor = cursor
            trader.tick()
            self.assertLessEqual(len(self.store.all_positions()), 1)

    def test_drawdown_kill_switch(self):
        trader, market = self._trader(Genome(entry=("always",)), 300)
        self.store.set("equity_peak", 10000.0)
        allowed, reason = trader.trading_allowed(1000.0)
        self.assertFalse(allowed)
        self.assertIn("просадка", reason)

    def test_daily_loss_stop(self):
        trader, _ = self._trader(Genome(entry=("always",)), 300)
        trader.trading_allowed(1000.0)                     # фиксирует старт дня
        allowed, reason = trader.trading_allowed(1000.0 * (1 - self.cfg.daily_loss_stop - 0.01))
        self.assertFalse(allowed)
        self.assertIn("убыток", reason)

    def test_paper_broker_cannot_spend_more_than_cash(self):
        market = ReplayMarket(self.cfg, self.candles, 300)
        broker = PaperBroker(self.cfg, self.store, market)
        fill = broker.buy("BTC/USDT", 10**6, 100.0)
        self.assertGreaterEqual(broker.cash, 0.0)
        self.assertLessEqual(fill.cost + fill.fee, self.cfg.start_balance + 1e-6)


class TestEvolve(unittest.TestCase):
    def test_finds_strategy_on_trending_market(self):
        f = build_features(make_candles(2000, seed=101, drift=0.0006, vol=0.012, cycle=0.001))
        cfg = Config(population=40, generations=6, min_notional=1.0)
        cand = evolve(f, cfg, random.Random(3))
        self.assertIsNotNone(cand, "на трендовом рынке стратегия должна найтись")
        self.assertGreater(cand.valid.n_trades, 0)
        self.assertGreater(cand.valid.net_return, 0.0)     # валидация вне обучающей выборки
        self.assertTrue(cand.genome.entry)

    def test_rejects_when_nothing_works(self):
        """На чистом шуме с огромными комиссиями годной стратегии быть не должно."""
        f = build_features(make_candles(1500, seed=5, drift=0.0, vol=0.02))
        cfg = Config(population=30, generations=4, taker_fee=0.02, slippage_bps=100.0,
                     min_notional=1.0, min_trades_valid=6)
        self.assertIsNone(evolve(f, cfg, random.Random(7)))

    def test_score_penalises_few_trades(self):
        from citadel.backtest import Result
        cfg = Config()
        many = Result(n_trades=30, net_return=0.2, max_dd=0.1, sharpe=2.0,
                      profit_factor=1.5, equity=[1.0] * 500, end_equity=1200, start_equity=1000)
        few = Result(n_trades=2, net_return=0.2, max_dd=0.1, sharpe=2.0,
                     profit_factor=1.5, equity=[1.0] * 500, end_equity=1200, start_equity=1000)
        self.assertGreater(score(many, cfg, 10), score(few, cfg, 10))

    def test_score_penalises_deep_drawdown(self):
        from citadel.backtest import Result
        cfg = Config()
        shallow = Result(n_trades=30, net_return=0.3, max_dd=0.05, sharpe=2.0,
                         profit_factor=1.5, equity=[1.0] * 500, end_equity=1300, start_equity=1000)
        deep = Result(n_trades=30, net_return=0.3, max_dd=0.5, sharpe=2.0,
                      profit_factor=1.5, equity=[1.0] * 500, end_equity=1300, start_equity=1000)
        self.assertGreater(score(shallow, cfg, 10), score(deep, cfg, 10))


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Storage(str(Path(self.tmp.name) / "s.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_only_one_active_strategy_per_symbol(self):
        g = Genome(entry=("always",))
        ids = [self.store.save_strategy("BTC/USDT", "1h", g, 1.0, {}) for _ in range(3)]
        for i in ids:
            self.store.activate(i, "BTC/USDT")
        rows = self.store.db.execute(
            "SELECT COUNT(*) c FROM strategies WHERE symbol=? AND active=1", ("BTC/USDT",)).fetchone()
        self.assertEqual(rows["c"], 1)
        self.assertEqual(self.store.active_strategy("BTC/USDT")["id"], ids[-1])

    def test_position_roundtrip(self):
        self.store.upsert_position("BTC/USDT", qty=0.5, entry_price=100.0, entry_fee=0.1,
                                   stop=95.0, take=110.0,
                                   trail=0.0, peak=101.0, opened_at=1, opened_bar=2, bars=0,
                                   strategy_id=None)
        row = self.store.get_position("BTC/USDT")
        self.assertAlmostEqual(row["qty"], 0.5)
        self.store.drop_position("BTC/USDT")
        self.assertIsNone(self.store.get_position("BTC/USDT"))

    def test_state_json_roundtrip(self):
        self.store.set("day", {"date": "2026-01-01", "start_equity": 1000.0})
        self.assertEqual(self.store.get("day")["start_equity"], 1000.0)
        self.assertIsNone(self.store.get("нет-такого"))


if __name__ == "__main__":
    unittest.main()


class TestRetrainSchedule(unittest.TestCase):
    """Поиск стратегии не должен запускаться на каждом круге, если он ничего не дал."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(symbols=("BTC/USDT",), db_path=str(Path(self.tmp.name) / "t.db"),
                          cache_dir=str(Path(self.tmp.name) / "c"), retrain_hours=24.0)
        self.store = Storage(self.cfg.db_path)
        n = 400
        self.candles = {"BTC/USDT": make_candles(n, seed=3,
                                                 start_ts=int(time.time() * 1000) - n * 3600000)}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _trader(self):
        market = ReplayMarket(self.cfg, self.candles, 300)
        return Trader(self.cfg, self.store, market, PaperBroker(self.cfg, self.store, market),
                      Notifier(enabled=False, echo=False))

    def test_failed_search_is_not_repeated_until_window(self):
        trader = self._trader()
        calls = []
        trader.train = lambda symbol, force=False: (calls.append(symbol),
                                                    setattr(trader.state[symbol], "trained_at",
                                                            time.time()), None)[2]
        trader.maybe_retrain()
        trader.maybe_retrain()
        trader.maybe_retrain()
        self.assertEqual(len(calls), 1, "поиск повторился, хотя окно ещё не истекло")

    def test_search_runs_again_after_window(self):
        trader = self._trader()
        calls = []
        trader.train = lambda symbol, force=False: (calls.append(symbol),
                                                    setattr(trader.state[symbol], "trained_at",
                                                            time.time()), None)[2]
        trader.maybe_retrain()
        trader.state["BTC/USDT"].trained_at = time.time() - 25 * 3600
        trader.maybe_retrain()
        self.assertEqual(len(calls), 2)
