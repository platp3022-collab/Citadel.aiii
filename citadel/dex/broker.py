# -*- coding: utf-8 -*-
"""
Исполнение на DEX: бумажный брокер с моделью влияния на цену и реальные свопы
через Jupiter.

Главное отличие от биржи: на DEX ты торгуешь против пула, поэтому сам двигаешь
цену. Чем крупнее сделка относительно ликвидности, тем хуже исполнение —
это моделируется явно, а не прячется в «проскальзывание».
"""
from __future__ import annotations

import logging

from ..broker import Fill, PaperBroker
from ..storage import Storage
from .config import DexConfig
from .http import ApiError
from .jupiter import Jupiter, SolanaRpc, Wallet
from .market import DexMarket, split_key

log = logging.getLogger("citadel.dex.broker")


def price_impact(amount_usd: float, liquidity_usd: float) -> float:
    """
    Доля, на которую сделка сдвинет цену в пуле x*y=k.
    Ликвидность DexScreener — обе стороны пула, значит резерв ≈ L/2:
        impact = A / (L/2 + A)
    Сделка в 1% ликвидности сдвигает цену примерно на 2%.
    """
    if liquidity_usd <= 0 or amount_usd <= 0:
        return 0.0
    reserve = liquidity_usd / 2.0
    return amount_usd / (reserve + amount_usd)


def effective_slippage_bps(cfg: DexConfig, liquidity_usd: float,
                           equity: float | None = None) -> float:
    """
    Проскальзывание для бэктеста конкретной пары: базовое (сеть, задержка, MEV)
    плюс ожидаемое влияние на цену при типичном для этой стратегии объёме.
    Без этого бэктест на DEX систематически врёт в плюс.
    """
    equity = cfg.start_balance if equity is None else equity
    typical = min(equity * cfg.max_position_frac, liquidity_usd * cfg.max_pool_frac)
    return cfg.slippage_bps + price_impact(typical, liquidity_usd) * 10_000


class DexPaperBroker(PaperBroker):
    """Бумажный счёт с учётом комиссии пула, влияния на цену и комиссии сети."""

    name = "бумажный счёт (DEX)"

    def __init__(self, cfg: DexConfig, store: Storage, market: DexMarket):
        super().__init__(cfg, store, market)
        self.cfg: DexConfig = cfg
        self.market: DexMarket = market

    def _impact(self, symbol: str, amount_usd: float) -> float:
        return price_impact(amount_usd, self.market.liquidity(symbol))

    def buy(self, symbol: str, qty: float, price: float) -> Fill:
        slip = self.cfg.slippage_bps / 10_000.0 + self._impact(symbol, qty * price)
        fill_price = price * (1 + slip)
        cost = fill_price * qty
        fee = cost * self.cfg.taker_fee + self.cfg.priority_fee_usd
        if cost + fee > self.cash:                       # ужимаем до доступного кэша
            budget = max(0.0, self.cash - self.cfg.priority_fee_usd)
            qty = budget / (fill_price * (1 + self.cfg.taker_fee)) * 0.999
            cost = fill_price * qty
            fee = cost * self.cfg.taker_fee + self.cfg.priority_fee_usd
        self.cash -= cost + fee
        return Fill(symbol, "buy", qty, fill_price, cost, fee)

    def sell(self, symbol: str, qty: float, price: float) -> Fill:
        slip = self.cfg.slippage_bps / 10_000.0 + self._impact(symbol, qty * price)
        fill_price = price * (1 - slip)
        cost = fill_price * qty
        fee = cost * self.cfg.taker_fee + self.cfg.priority_fee_usd
        self.cash += cost - fee
        return Fill(symbol, "sell", qty, fill_price, cost, fee)


class JupiterBroker:
    """
    Реальные свопы на Solana. Покупка: USDC → токен, продажа: токен → USDC.

    Количество после свопа берётся не из котировки, а из фактического баланса
    кошелька: токены с налогом на перевод доставляют меньше, чем обещали.
    """

    live = True
    name = "реальный кошелёк (Solana/Jupiter)"

    def __init__(self, cfg: DexConfig, store: Storage, market: DexMarket):
        self.cfg, self.store, self.market = cfg, store, market
        self.wallet = Wallet(cfg.wallet_key)
        self.rpc = SolanaRpc(cfg.rpc_url)
        self.jup = Jupiter(cfg.jupiter_url)
        self._decimals: dict[str, int] = {}
        log.info("кошелёк %s", self.wallet.pubkey)

    # ── справки ─────────────────────────────────────────────────────────────
    def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            self._decimals[mint] = self.rpc.decimals(mint)
        return self._decimals[mint]

    def _mint(self, symbol: str) -> str:
        pair = self.market.pair(symbol)
        if not pair or not pair.base_address:
            raise ApiError(f"неизвестен адрес токена для {symbol}")
        chain, _ = split_key(symbol)
        if chain != "solana":
            raise ApiError(f"свопы через Jupiter возможны только в Solana, а тут {chain}")
        return pair.base_address

    @property
    def cash(self) -> float:
        return self.rpc.token_balance(self.wallet.pubkey, self.cfg.quote_mint)

    def equity(self, prices: dict[str, float]) -> float:
        total = self.cash
        for symbol, px in prices.items():
            try:
                qty = self.rpc.token_balance(self.wallet.pubkey, self._mint(symbol))
            except ApiError:
                continue
            total += qty * px
        return total

    # ── свопы ───────────────────────────────────────────────────────────────
    def _swap(self, input_mint: str, output_mint: str, amount_atomic: int) -> tuple[str, dict]:
        quote = self.jup.quote(input_mint, output_mint, amount_atomic,
                               int(self.cfg.slippage_bps))
        tx = self.jup.swap_transaction(
            quote, self.wallet.pubkey,
            priority_lamports=int(self.cfg.priority_fee_usd / 150 * 1e9))  # ~$150 за SOL
        signature = self.rpc.send_raw(self.wallet.sign(tx))
        log.info("своп отправлен: %s", signature)
        self.rpc.confirm(signature)
        return signature, quote

    def buy(self, symbol: str, qty: float, price: float) -> Fill:
        mint = self._mint(symbol)
        spend_usd = qty * price
        quote_dec = self.decimals(self.cfg.quote_mint)
        before = self.rpc.token_balance(self.wallet.pubkey, mint)
        signature, _ = self._swap(self.cfg.quote_mint, mint,
                                  int(spend_usd * 10 ** quote_dec))
        after = self.rpc.token_balance(self.wallet.pubkey, mint)
        got = max(0.0, after - before)
        if got <= 0:
            raise ApiError(f"своп {signature} прошёл, но токенов на кошельке не прибавилось")
        fill_price = spend_usd / got
        return Fill(symbol, "buy", got, fill_price, spend_usd, 0.0, signature)

    def sell(self, symbol: str, qty: float, price: float) -> Fill:
        mint = self._mint(symbol)
        held = self.rpc.token_balance(self.wallet.pubkey, mint)
        qty = min(qty, held)                              # продаём только то, что есть
        if qty <= 0:
            raise ApiError(f"нечего продавать: на кошельке нет {symbol}")
        dec = self.decimals(mint)
        before_usdc = self.cash
        signature, _ = self._swap(mint, self.cfg.quote_mint, int(qty * 10 ** dec))
        got_usd = max(0.0, self.cash - before_usdc)
        fill_price = got_usd / qty if qty else price
        return Fill(symbol, "sell", qty, fill_price, got_usd, 0.0, signature)


def make_dex_broker(cfg: DexConfig, store: Storage, market: DexMarket):
    return JupiterBroker(cfg, store, market) if cfg.live else DexPaperBroker(cfg, store, market)
