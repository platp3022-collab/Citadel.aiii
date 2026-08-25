# -*- coding: utf-8 -*-
"""Тесты экспорта стратегии в Pine Script."""
from __future__ import annotations

import os
import random
import re
import unittest

from citadel.features import ENTRY_POOL, EXIT_POOL
from citadel.genome import Genome, random_genome
from citadel.pine import (DECLS, SIGNALS, UnsupportedSignal, to_pine, trades_overlay,
                          tv_symbol)

BUILTINS = {
    "close", "open", "high", "low", "volume", "time", "time_close", "bar_index", "na",
    "true", "false", "syminfo", "strategy", "color", "shape", "location", "size",
    "plot", "label", "array", "math", "str", "format", "input", "alert", "indicator",
    "ta", "nz", "bgcolor", "plotshape", "barstate",
    "and", "or", "not", "if", "else", "for", "while", "var",     # ключевые слова Pine
}


def declared_names(src: str) -> set[str]:
    """Имена, объявленные в скрипте: `x = ...`, `[a, b] = ...`, `var float y = ...`."""
    names: set[str] = set()
    for line in src.splitlines():
        line = line.strip()
        m = re.match(r"^\[([^\]]+)\]\s*=", line)
        if m:
            names.update(x.strip() for x in m.group(1).split(","))
            continue
        m = re.match(r"^(?:var\s+\w+\s+)?([A-Za-z_]\w*)\s*:?=", line)
        if m:
            names.add(m.group(1))
    return names


def used_names(expr: str) -> set[str]:
    """Идентификаторы в выражении, без обращений к полям вида ta.ema."""
    out = set()
    for m in re.finditer(r"(?<![.\w])([A-Za-z_]\w*)", expr):
        name = m.group(1)
        if name not in BUILTINS:
            out.add(name)
    return out


class TestSignalCoverage(unittest.TestCase):
    def test_every_bot_signal_translates(self):
        missing = sorted(s for s in set(ENTRY_POOL) | set(EXIT_POOL) if s not in SIGNALS)
        self.assertEqual(missing, [], f"нет перевода в Pine: {missing}")

    def test_unknown_signal_raises(self):
        with self.assertRaises(UnsupportedSignal):
            to_pine(Genome(entry=("никакого_такого_сигнала",)), "BTC/USDT", "1h")

    def test_cli_explains_unknown_signal_instead_of_crashing(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path as _Path

        from citadel.storage import Storage

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Storage(str(_Path(tmp.name) / "t.db"))
        sid = store.save_strategy("BTC/USDT", "1h", Genome(entry=("выдуманный_сигнал",)), 1.0, {})
        store.activate(sid, "BTC/USDT")
        store.close()
        env = {**os.environ, "CITADEL_DB_PATH": str(_Path(tmp.name) / "t.db"),
               "CITADEL_SYMBOLS": "BTC/USDT", "CITADEL_CACHE_DIR": tmp.name}
        res = subprocess.run([sys.executable, "tradebot.py", "--offline", "--dry", "pine",
                              "--out", tmp.name], capture_output=True, text=True, env=env)
        self.assertNotIn("Traceback", res.stdout + res.stderr)
        self.assertIn("перезапусти", res.stdout)

    def test_signal_deps_exist_in_decls(self):
        for name, (_, needs) in SIGNALS.items():
            for dep in needs:
                self.assertIn(dep, DECLS, f"{name} требует необъявленный {dep}")


class TestStrategyScript(unittest.TestCase):
    def test_structure(self):
        g = Genome(entry=("ema21_over_ema50", "adx_over_25"), exit=("breakdown_dc20",),
                   stop_atr=2.0, take_atr=3.0, trail_atr=1.0, max_hold=48,
                   risk_pct=1.0, cooldown=2)
        src = to_pine(g, "BTC/USDT", "1h", strategy_id=7, score=1.4, exchange="binance")
        self.assertTrue(src.startswith("//@version=6"))
        self.assertIn('strategy("Citadel · BTC/USDT #7"', src)
        self.assertIn("entryCond = (ema21 > ema50) and (adxVal > 25)", src)
        self.assertIn("exitCond  = (close < dcLow20)", src)
        self.assertIn("strategy.entry(\"long\", strategy.long", src)
        self.assertIn("BINANCE:BTCUSDT", src)
        self.assertNotIn("{", src)                       # незакрытых подстановок нет

    def test_only_needed_indicators_are_declared(self):
        g = Genome(entry=("rsi14_over_50",), exit=())
        src = to_pine(g, "BTC/USDT", "1h")
        self.assertIn("rsi14 = ta.rsi(close, 14)", src)
        self.assertNotIn("ta.dmi", src)
        self.assertNotIn("ta.bb(", src)
        self.assertIn("atr14 = ta.atr(14)", src)         # ATR нужен всегда — для стопа

    def test_no_undeclared_identifiers_for_random_genomes(self):
        rnd = random.Random(5)
        for _ in range(120):
            g = random_genome(rnd)
            src = to_pine(g, "ETH/USDT", "4h")
            names = declared_names(src)
            for line in src.splitlines():
                if line.startswith(("entryCond", "exitCond")):
                    for used in used_names(line.split("=", 1)[1]):
                        self.assertIn(used, names, f"{used} не объявлен для {g.entry}/{g.exit}")

    def test_blocks_are_indented(self):
        """После строки, открывающей блок, обязана идти строка с большим отступом."""
        src = to_pine(random_genome(random.Random(9)), "BTC/USDT", "1h")
        lines = [l for l in src.splitlines() if l.strip() and not l.strip().startswith("//")]
        for i, line in enumerate(lines[:-1]):
            stripped = line.strip()
            if re.match(r"^(if|else if|else|for|while)\b", stripped) and stripped.endswith(
                    tuple("abcdefghijklmnopqrstuvwxyz0123456789)_")):
                indent = len(line) - len(line.lstrip())
                nxt = lines[i + 1]
                self.assertGreater(len(nxt) - len(nxt.lstrip()), indent,
                                   f"блок без тела: {stripped}")

    def test_balanced_brackets(self):
        src = to_pine(random_genome(random.Random(11)), "BTC/USDT", "1h")
        body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))
        for opener, closer in (("(", ")"), ("[", "]")):
            self.assertEqual(body.count(opener), body.count(closer), opener)

    def test_risk_params_land_in_inputs(self):
        g = Genome(entry=("always",), stop_atr=3.5, take_atr=0.0, trail_atr=2.5,
                   max_hold=72, risk_pct=0.75, cooldown=4)
        src = to_pine(g, "SOL/USDT", "15m")
        self.assertIn("input.float(3.5,", src)
        self.assertIn("input.float(0,", src)
        self.assertIn("input.int(72,", src)
        self.assertIn("input.float(0.75,", src)
        self.assertIn("input.int(4,", src)


class TestTradesOverlay(unittest.TestCase):
    def test_arrays_and_types(self):
        buys = [(1712345678000, 100.0), (1712349278000, 105.5)]
        sells = [(1712352878000, 110.0, 9.5), (1712356478000, 99.0, -3.0)]
        src = trades_overlay("BTC/USDT", "1h", buys, sells)
        self.assertIn("buyTs = array.from(1712345678000, 1712349278000)", src)
        self.assertIn("buyPx = array.from(100.0, 105.5)", src)     # с точкой → массив float
        self.assertIn("sellPnl = array.from(9.5, -3.0)", src)
        self.assertIn("barMs = 3600000", src)
        self.assertIn("indicator(", src)

    def test_empty_history_is_valid(self):
        src = trades_overlay("BTC/USDT", "1h", [], [])
        self.assertIn("array.new<int>()", src)
        self.assertIn("array.new<float>()", src)

    def test_limit_keeps_last_trades(self):
        buys = [(1000 + i, float(i)) for i in range(400)]
        src = trades_overlay("BTC/USDT", "1h", buys, [], limit=10)
        self.assertIn("(10 шт.)", src)
        self.assertIn("1399", src)
        self.assertNotIn("1000,", src)

    def test_tv_symbol(self):
        self.assertEqual(tv_symbol("BTC/USDT", "binance"), "BINANCE:BTCUSDT")
        self.assertEqual(tv_symbol("ETH/USDT"), "ETHUSDT")


if __name__ == "__main__":
    unittest.main()
