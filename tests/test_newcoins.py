# -*- coding: utf-8 -*-
"""Тесты ленты новых монет: разбор пулов, разметка рисков, сканер."""
from __future__ import annotations

import time
import unittest

from citadel.dex.dexscreener import DexScreener, Pair
from citadel.dex.geckoterminal import GeckoTerminal
from citadel.dex.newcoins import NewCoinScanner, assess, pair_from_pool

NOW = time.time()


def pool(symbol="PEPE2", address="POOL1", liquidity=125_000.0, volume=310_000.0,
         buys=1200, sells=980, age_hours=2.5, chain="solana", fdv=4_200_000.0,
         price=0.00042) -> dict:
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age_hours * 3600))
    return {
        "id": f"{chain}_{address}", "type": "pool",
        "attributes": {
            "address": address, "name": f"{symbol} / SOL",
            "base_token_price_usd": str(price), "reserve_in_usd": str(liquidity),
            "fdv_usd": str(fdv), "pool_created_at": created,
            "volume_usd": {"h24": str(volume), "h1": str(volume / 24)},
            "transactions": {"h24": {"buys": buys, "sells": sells}},
            "price_change_percentage": {"h24": "37.5"},
        },
        "relationships": {
            "base_token": {"data": {"id": f"{chain}_MINT{symbol}"}},
            "quote_token": {"data": {"id": f"{chain}_So111"}},
            "dex": {"data": {"id": "raydium"}},
        },
    }


class TestParsing(unittest.TestCase):
    def test_pool_becomes_pair(self):
        p = pair_from_pool(pool())
        self.assertEqual(p.key, "solana:POOL1")
        self.assertEqual(p.name, "PEPE2/SOL")
        self.assertEqual(p.base_address, "MINTPEPE2")
        self.assertEqual(p.dex, "raydium")
        self.assertAlmostEqual(p.liquidity_usd, 125_000.0)
        self.assertAlmostEqual(p.age_hours, 2.5, places=1)

    def test_network_names_are_translated_back(self):
        self.assertEqual(pair_from_pool(pool(chain="eth")).chain, "ethereum")
        self.assertEqual(pair_from_pool(pool(chain="polygon_pos")).chain, "polygon")
        self.assertEqual(pair_from_pool(pool(chain="base")).chain, "base")

    def test_broken_pool_is_skipped(self):
        self.assertIsNone(pair_from_pool({}))
        self.assertIsNone(pair_from_pool({"attributes": {"name": "X / Y"}}))

    def test_missing_numbers_do_not_crash(self):
        raw = pool()
        raw["attributes"]["reserve_in_usd"] = None
        raw["attributes"]["pool_created_at"] = "не дата"
        p = pair_from_pool(raw)
        self.assertEqual(p.liquidity_usd, 0.0)
        self.assertEqual(p.created_at_ms, 0)


class TestAssess(unittest.TestCase):
    def test_healthy_pool_scores_positive(self):
        coin = assess(pair_from_pool(pool(liquidity=900_000, volume=2_000_000,
                                          buys=3000, sells=2600, age_hours=40)))
        self.assertGreater(coin.score, 1.0)
        self.assertIn("покупки и продажи сбалансированы", coin.good)

    def test_honeypot_pattern_is_flagged(self):
        coin = assess(pair_from_pool(pool(buys=900, sells=0)))
        self.assertTrue(any("honeypot" in f for f in coin.flags))
        self.assertLess(coin.score, 0)

    def test_thin_liquidity_and_fresh_pool_are_flagged(self):
        coin = assess(pair_from_pool(pool(liquidity=3_000, age_hours=0.3)))
        self.assertTrue(any("ликвидность" in f for f in coin.flags))
        self.assertTrue(any("меньше часа" in f for f in coin.flags))

    def test_wash_trading_is_flagged(self):
        coin = assess(pair_from_pool(pool(liquidity=20_000, volume=5_000_000,
                                          buys=500, sells=480)))
        self.assertTrue(any("накрутку" in f for f in coin.flags))

    def test_age_text_in_description(self):
        self.assertIn("мин", assess(pair_from_pool(pool(age_hours=0.4))).describe())
        self.assertIn("ч", assess(pair_from_pool(pool(age_hours=5))).describe())
        self.assertIn("д", assess(pair_from_pool(pool(age_hours=100))).describe())


class FakeHttp:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append(url)
        if "new_pools" in url or "trending_pools" in url:
            return {"data": self.rows}
        return {}


class TestScanner(unittest.TestCase):
    def setUp(self):
        self.rows = [pool("AAA", "P1", liquidity=800_000, volume=2_000_000, age_hours=30),
                     pool("SCAM", "P2", liquidity=2_000, volume=900_000, buys=800,
                          sells=1, age_hours=0.2),
                     pool("BBB", "P3", liquidity=120_000, volume=300_000, age_hours=6)]
        self.http = FakeHttp(self.rows)

    def _scanner(self, enrich=None):
        gecko = GeckoTerminal(self.http)
        screener = DexScreener(FakeHttp([]))
        scanner = NewCoinScanner(gecko, screener)
        scanner._enrich = enrich or (lambda pairs: pairs)
        return scanner

    def test_fetch_sorts_by_score(self):
        coins = self._scanner().fetch("solana")
        self.assertEqual(len(coins), 3)
        self.assertEqual(coins[0].pair.base_symbol, "AAA")     # самый живой первым
        self.assertEqual(coins[-1].pair.base_symbol, "SCAM")   # скам последним
        self.assertGreater(coins[0].score, coins[-1].score)

    def test_trending_uses_other_feed(self):
        self._scanner().fetch("solana", trending=True)
        self.assertTrue(any("trending_pools" in c for c in self.http.calls))
        self.assertFalse(any("new_pools" in c for c in self.http.calls))

    def test_enrichment_replaces_pairs_with_screener_data(self):
        rich = Pair(chain="solana", dex="raydium", pair_address="P1", base_symbol="AAA",
                    base_address="M", quote_symbol="SOL", quote_address="S",
                    price_usd=1.0, liquidity_usd=800_000, volume_h24=2_000_000,
                    socials=["https://x.com/aaa"])
        coins = self._scanner(enrich=lambda pairs: [
            rich if p.pair_address == "P1" else p for p in pairs]).fetch("solana")
        best = next(c for c in coins if c.pair.pair_address == "P1")
        self.assertIn("есть сайт или соцсети", best.good)

    def test_unreachable_feed_returns_empty(self):
        class Dead:
            def get_json(self, url, params=None):
                from citadel.dex.http import ApiError
                raise ApiError("сеть недоступна")

        scanner = NewCoinScanner(GeckoTerminal(Dead()), DexScreener(Dead()))
        self.assertEqual(scanner.fetch("solana"), [])


if __name__ == "__main__":
    unittest.main()
