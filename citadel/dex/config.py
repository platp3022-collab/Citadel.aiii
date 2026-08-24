# -*- coding: utf-8 -*-
"""Конфиг DEX-бота: то же, что у биржевого, плюс параметры пулов и безопасности."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR, Config
from .safety import SafetyLimits


@dataclass
class DexConfig(Config):
    # ── что торгуем ─────────────────────────────────────────────────────────
    chain: str = "solana"                # сеть DexScreener: solana, base, bsc, ethereum…
    symbols: tuple[str, ...] = ()        # заполняется автоматически: "chain:адрес_пула"
    timeframe: str = "15m"               # на DEX история короткая, час — уже роскошь
    history: int = 1500
    start_balance: float = 500.0
    max_positions: int = 2

    # ── издержки свопа ──────────────────────────────────────────────────────
    taker_fee: float = 0.0025            # комиссия пула (Raydium 0.25%, Uniswap 0.3%)
    slippage_bps: float = 100.0          # допуск проскальзывания, 100 = 1%
    priority_fee_usd: float = 0.05       # приоритетная комиссия сети за своп
    max_pool_frac: float = 0.01          # не больше 1% ликвидности пула в одной сделке
    min_notional: float = 5.0

    # ── подбор пар ──────────────────────────────────────────────────────────
    universe_size: int = 6               # сколько пар держим в работе
    rediscover_hours: float = 12.0       # как часто пересматривать список пар
    discover_queries: tuple[str, ...] = ("SOL", "USDC")   # с чего начинать поиск
    rug_liquidity_drop: float = 0.5      # ликвидность упала вдвое → аварийный выход

    # ── фильтры безопасности (разворачиваются в SafetyLimits) ───────────────
    min_liquidity_usd: float = 50_000.0
    min_volume_h24_usd: float = 100_000.0
    min_age_hours: float = 72.0
    min_txns_h24: int = 200
    require_socials: bool = False
    rugcheck: bool = True

    # ── исполнение свопов (Solana / Jupiter) ────────────────────────────────
    quote_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"   # USDC
    quote_symbol: str = "USDC"
    jupiter_url: str = "https://lite-api.jup.ag/swap/v1"
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    wallet_key: str = field(default="", repr=False)      # base58 приватный ключ

    db_path: str = str(DATA_DIR / "dex.db")
    cache_dir: str = str(DATA_DIR / "dex_candles")

    @classmethod
    def from_env(cls) -> "DexConfig":
        cfg = super().from_env()
        cfg.wallet_key = os.environ.get("SOLANA_PRIVATE_KEY", cfg.wallet_key)
        cfg.rpc_url = os.environ.get("SOLANA_RPC_URL", cfg.rpc_url)
        return cfg

    @property
    def quote(self) -> str:
        """На DEX всё считаем в долларах: свечи GeckoTerminal приходят в USD."""
        return "USD"

    def safety(self) -> SafetyLimits:
        return SafetyLimits(
            min_liquidity_usd=self.min_liquidity_usd,
            min_volume_h24_usd=self.min_volume_h24_usd,
            min_age_hours=self.min_age_hours,
            min_txns_h24=self.min_txns_h24,
            require_socials=self.require_socials,
            rugcheck=self.rugcheck,
        )

    def ensure_dirs(self) -> None:
        super().ensure_dirs()
        Path(self.pairs_path).parent.mkdir(parents=True, exist_ok=True)

    @property
    def pairs_path(self) -> str:
        return str(Path(self.cache_dir).parent / "dex_pairs.json")
