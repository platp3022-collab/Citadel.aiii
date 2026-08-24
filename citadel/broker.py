# -*- coding: utf-8 -*-
"""
Исполнение сделок: бумажный счёт и реальные ордера через ccxt.

Бумажный брокер живёт целиком в sqlite и повторяет допущения бэктеста
(комиссия + проскальзывание), поэтому «бумага» и бэктест сопоставимы.
Живой брокер отправляет рыночные ордера и берёт фактическую цену исполнения
из ответа биржи.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .market import Market
from .storage import Storage

log = logging.getLogger("citadel.broker")


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    cost: float          # оборот в quote без комиссии
    fee: float
    order_id: str = ""


class PaperBroker:
    live = False
    name = "бумажный счёт"

    def __init__(self, cfg: Config, store: Storage, market: Market | None = None):
        self.cfg, self.store, self.market = cfg, store, market
        if self.store.get("paper_cash") is None:
            self.store.set("paper_cash", cfg.start_balance)

    @property
    def cash(self) -> float:
        return float(self.store.get("paper_cash", self.cfg.start_balance))

    @cash.setter
    def cash(self, v: float) -> None:
        self.store.set("paper_cash", round(v, 10))

    def equity(self, prices: dict[str, float]) -> float:
        total = self.cash
        for p in self.store.all_positions():
            px = prices.get(p["symbol"])
            if px:
                total += p["qty"] * px
        return total

    def buy(self, symbol: str, qty: float, price: float) -> Fill:
        fill_price = price * (1 + self.cfg.slippage_bps / 10000.0)
        cost = fill_price * qty
        fee = cost * self.cfg.taker_fee
        if cost + fee > self.cash:
            qty = max(0.0, (self.cash / (fill_price * (1 + self.cfg.taker_fee))) * 0.999)
            cost = fill_price * qty
            fee = cost * self.cfg.taker_fee
        self.cash -= cost + fee
        return Fill(symbol, "buy", qty, fill_price, cost, fee)

    def sell(self, symbol: str, qty: float, price: float) -> Fill:
        fill_price = price * (1 - self.cfg.slippage_bps / 10000.0)
        cost = fill_price * qty
        fee = cost * self.cfg.taker_fee
        self.cash += cost - fee
        return Fill(symbol, "sell", qty, fill_price, cost, fee)


class LiveBroker:
    live = True
    name = "реальный счёт"

    def __init__(self, cfg: Config, store: Storage, market: Market):
        self.cfg, self.store, self.market = cfg, store, market

    @property
    def cash(self) -> float:
        bal = self.market.fetch_balance()
        free = (bal.get("free") or {}).get(self.cfg.quote)
        return float(free or 0.0)

    def equity(self, prices: dict[str, float]) -> float:
        bal = self.market.fetch_balance()
        total = float((bal.get("total") or {}).get(self.cfg.quote) or 0.0)
        for symbol, px in prices.items():
            base = symbol.split("/")[0]
            amount = float((bal.get("total") or {}).get(base) or 0.0)
            if amount and px:
                total += amount * px
        return total

    def _order(self, symbol: str, side: str, qty: float, price_hint: float) -> Fill:
        qty = self.market.amount_to_precision(symbol, qty)
        if qty <= 0:
            raise ValueError("нулевой объём ордера")
        log.info("ордер %s %s %.8f", side, symbol, qty)
        order = self.market.ex.create_order(symbol, "market", side, qty)
        try:                                            # уточняем факт исполнения
            order = self.market.ex.fetch_order(order["id"], symbol) or order
        except Exception:                               # noqa: BLE001 — не все биржи это умеют
            pass
        filled = float(order.get("filled") or qty)
        price = float(order.get("average") or order.get("price") or price_hint)
        cost = float(order.get("cost") or filled * price)
        fee_info = order.get("fee") or {}
        fee = float(fee_info.get("cost") or cost * self.cfg.taker_fee)
        return Fill(symbol, side, filled, price, cost, fee, str(order.get("id", "")))

    def buy(self, symbol: str, qty: float, price: float) -> Fill:
        return self._order(symbol, "buy", qty, price)

    def sell(self, symbol: str, qty: float, price: float) -> Fill:
        return self._order(symbol, "sell", qty, price)


def make_broker(cfg: Config, store: Storage, market: Market):
    return LiveBroker(cfg, store, market) if cfg.live else PaperBroker(cfg, store, market)
