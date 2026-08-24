# -*- coding: utf-8 -*-
"""
Конфиг бота. Правь значения прямо здесь либо переопределяй через .env / переменные окружения
(имя переменной = имя поля в верхнем регистре с префиксом CITADEL_, например CITADEL_TIMEFRAME=4h).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def load_env(path: Path = ROOT / ".env") -> None:
    """Простейший .env-лоадер: KEY=VALUE, без перезаписи уже заданных переменных."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    # ── рынок ───────────────────────────────────────────────────────────────
    exchange: str = "binance"           # любой спотовый биржевой id из ccxt
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
    timeframe: str = "1h"               # 15m / 1h / 4h / 1d
    history: int = 3000                 # сколько свечей тянуть на обучение

    # ── капитал и риск ──────────────────────────────────────────────────────
    start_balance: float = 1000.0       # стартовый баланс бумажного счёта (в quote-валюте)
    max_positions: int = 2              # сколько позиций держим одновременно
    max_position_frac: float = 0.35     # максимум доли эквити в одной позиции
    max_drawdown_stop: float = 0.25     # просадка счёта, после которой бот выключает торговлю
    daily_loss_stop: float = 0.08       # дневной убыток, после которого пауза до следующего дня

    # ── издержки (для бэктеста и бумажной торговли) ─────────────────────────
    taker_fee: float = 0.001            # 0.1% — комиссия тейкера
    slippage_bps: float = 5.0           # проскальзывание, базисные пункты (5 = 0.05%)

    # ── эволюция стратегии ──────────────────────────────────────────────────
    population: int = 80                # размер популяции генетического поиска
    generations: int = 25               # поколений на один прогон
    elite: int = 6                      # сколько лучших переносим без изменений
    train_frac: float = 0.7             # доля истории на обучение, остальное — валидация
    min_trades_train: int = 12          # меньше сделок на обучении — стратегия не считается
    min_trades_valid: int = 4           # минимум сделок на валидации
    finalists: int = 15                 # сколько лучших с обучения проверяем на валидации
    retrain_hours: float = 24.0         # как часто пересобирать стратегию
    adopt_margin: float = 0.10          # новая стратегия должна быть лучше текущей на 10%
    seed: int = 0                       # 0 = случайный сид поиска

    # ── исполнение ──────────────────────────────────────────────────────────
    live: bool = False                  # только через флаг --live, из .env НЕ включается
    min_notional: float = 10.0          # минимальный размер ордера в quote-валюте
    poll_seconds: float = 20.0          # шаг основного цикла

    # ── прочее ──────────────────────────────────────────────────────────────
    db_path: str = str(DATA_DIR / "trader.db")
    cache_dir: str = str(DATA_DIR / "candles")
    log_level: str = "INFO"

    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""

    # ────────────────────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls) -> "Config":
        load_env()
        cfg = cls()
        for f in fields(cls):
            if f.name == "live":        # реальную торговлю включает только флаг --live
                continue
            raw = os.environ.get("CITADEL_" + f.name.upper())
            if raw is None or raw == "":
                continue
            setattr(cfg, f.name, _coerce(raw, getattr(cfg, f.name)))
        cfg.api_key = os.environ.get("EXCHANGE_API_KEY", cfg.api_key)
        cfg.api_secret = os.environ.get("EXCHANGE_API_SECRET", cfg.api_secret)
        cfg.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", cfg.telegram_token)
        cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", cfg.telegram_chat_id)
        return cfg

    @property
    def quote(self) -> str:
        """Котируемая валюта (по первому символу) — в ней считаем баланс."""
        return self.symbols[0].split("/")[1] if self.symbols else "USDT"

    def ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)


def _coerce(raw: str, sample):
    if isinstance(sample, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on", "да")
    if isinstance(sample, int) and not isinstance(sample, bool):
        return int(float(raw))
    if isinstance(sample, float):
        return float(raw)
    if isinstance(sample, tuple):
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    return raw


# Сколько баров таймфрейма приходится на год — нужно для годовых метрик.
BARS_PER_YEAR = {
    "1m": 525600, "3m": 175200, "5m": 105120, "15m": 35040, "30m": 17520,
    "1h": 8760, "2h": 4380, "4h": 2190, "6h": 1460, "8h": 1095, "12h": 730,
    "1d": 365, "3d": 121, "1w": 52,
}

TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}
