#!/usr/bin/env python3
"""
Торговый модуль: ведёт позиции по монетам, которые нашёл axiom_scout.

Режимы:
  paper — бумажная торговля (по умолчанию). Сделки только считаются: бот пишет,
          по какой цене вошёл бы, ведёт позицию, закрывает по тейку/стопу/таймауту
          и копит статистику. Реальных транзакций нет вообще.
  live  — реальные сделки своим кошельком. Своп идёт через Jupiter, транзакция
          подписывается локально ключом из SOLANA_PRIVATE_KEY и уходит в сеть
          уже подписанной. Ключ никуда не отправляется и в логи не пишется.

Бумажный расчёт честный: с проскальзыванием на входе и выходе, комиссией
протокола и сетевым сбором. Иначе статистика врёт в плюс и решение принимается
по выдуманным цифрам.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("trader")

# ════════════════════════════════════════════════════════════════════════════
#  КОНФИГ
# ════════════════════════════════════════════════════════════════════════════

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "paper",                 # paper | live

    # ---- размер и лимиты ----
    "size_sol": 0.1,                 # сколько SOL кладём в одну монету
    "daily_limit_sol": 1.0,          # больше этого за сутки не тратим
    "max_positions": 3,              # столько монет держим одновременно
    "cooldown_minutes": 120,         # не заходить в ту же монету повторно
    "min_score": 0,                  # 0 = берём всё, что бот назвал «НОРМ»

    # ---- выход ----
    "take_profit_pct": 60.0,         # фиксируем прибыль
    "stop_loss_pct": -35.0,          # режем убыток
    "trailing_stop_pct": 25.0,       # откат от максимума после выхода в плюс
    "trailing_after_pct": 30.0,      # трейлинг включается после этой прибыли
    "timeout_minutes": 30.0,         # не растёт — выходим

    # ---- правила выхода под конкретную стратегию ----
    # У сделки по чужим кошелькам («кимчи») другая математика: там ловят не
    # +60%, а иксы, и платят за это частыми мелкими минусами. Поэтому половину
    # снимаем на удвоении, остаток едет дальше со стопом в ноль — рисковать
    # там уже нечем, а верхняя граница не поставлена вовсе.
    # Внимание не держится вечно: пошёл максимум, а дальше монета встала —
    # это и есть момент, когда пора выходить, не дожидаясь стопа.
    "decay_after_pct": 40.0,         # с какого плюса следим за угасанием
    "decay_stall_minutes": 12.0,     # столько без нового максимума
    "decay_drop_pct": 15.0,          # и настолько ниже максимума

    # Потолок монеты решает, как её вести. Клон чужого нарратива отрабатывает
    # быстрый скальп: у него нет шанса на иксы, зато есть шанс успеть выйти.
    # Первый в нарративе — наоборот, его держим дольше и без потолка.
    "ceiling_rules": {
        "подражатель": {
            "take_profit_pct": 35.0,
            "stop_loss_pct": -25.0,
            "trailing_after_pct": 20.0,
            "trailing_stop_pct": 15.0,
            "timeout_minutes": 15.0,
            "decay_after_pct": 25.0,
            "decay_stall_minutes": 8.0,
        },
        "первопроходец": {
            "take_profit_pct": 0.0,        # потолка нет
            "scale_out_at_pct": 100.0,
            "scale_out_pct": 40.0,
            "breakeven_after_scale": True,
            "trailing_after_pct": 130.0,
            "trailing_stop_pct": 35.0,
            "timeout_minutes": 120.0,
            "decay_stall_minutes": 20.0,
        },
    },
    "rules": {
        "кимчи": {
            "take_profit_pct": 0.0,        # потолка нет: остаток бежит за иксами
            "stop_loss_pct": -32.0,
            "scale_out_at_pct": 100.0,     # удвоился — снимаем часть
            "scale_out_pct": 50.0,         # ровно половину
            "breakeven_after_scale": True, # после этого стоп переезжает в ноль
            "trailing_after_pct": 120.0,
            "trailing_stop_pct": 35.0,
            "timeout_minutes": 90.0,
        },
    },

    # ---- реализм бумажных сделок ----
    "entry_slippage_pct": 2.0,
    "exit_slippage_pct": 2.0,
    "fee_pct": 1.0,                  # своп + комиссия лончпада
    "network_fee_sol": 0.0005,       # сеть + приоритетка на сделку

    # ---- реальные сделки (mode: live) ----
    "slippage_bps": 1000,            # 10% — у свежих монет стакан узкий, иначе не пройдёт
    "priority_fee_lamports": 300000, # приоритетная комиссия, чтобы попасть в блок
    "max_trade_sol": 0.5,            # жёсткий потолок на одну сделку
    "min_sol_reserve": 0.02,         # неснижаемый остаток на комиссии
    "confirm_timeout": 90,           # сколько ждём подтверждения сети
    "rpc_url": "",                   # пусто = SOLANA_RPC_URL из .env или публичный

    "poll_seconds": 30,              # как часто переоценивать позиции
    "storage_path": "data/memebot.db",
}

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_LITE = "https://lite-api.jup.ag"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1"
DEX_API = "https://api.dexscreener.com"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
LAMPORTS = 1_000_000_000

EXIT_PLAIN = {"take_profit": "тейк", "stop_loss": "стоп", "trailing": "трейлинг",
              "timeout": "таймаут", "manual": "вручную", "breakeven": "в ноль",
              "decay": "внимание ушло"}

EXIT_LABELS = {
    "take_profit": "🎯 тейк-профит",
    "stop_loss": "🛑 стоп-лосс",
    "trailing": "📉 трейлинг-стоп",
    "timeout": "⏳ таймаут",
    "manual": "✋ вручную",
    "breakeven": "🛟 стоп в ноль",
    "decay": "🥱 внимание ушло",
}


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_sol(v: float) -> str:
    return f"{num(v):+.4f} SOL" if v else "0 SOL"


# ════════════════════════════════════════════════════════════════════════════
#  ПОЗИЦИЯ
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    mint: str
    symbol: str = ""
    launchpad: str = ""
    mode: str = "paper"
    score: float = 0.0
    strategy: str = "метрики"        # что привело в эту сделку
    meta: str = ""                   # мета: ИИ-агенты, политика, животные…
    ceiling: str = ""                # потолок: первопроходец / подражатель
    thesis: str = ""                 # зачем зашли — строка для журнала

    opened_ts: float = 0.0
    entry_price: float = 0.0         # цена токена в долларах на входе
    entry_sol_price: float = 0.0     # курс SOL на входе
    size_sol: float = 0.0            # сколько SOL вложили (с учётом сборов)
    tokens: float = 0.0              # сколько токенов получили

    realized_sol: float = 0.0        # уже снято частичной продажей
    sold_pct: float = 0.0            # какую долю позиции успели продать

    high_price: float = 0.0          # максимум цены за время удержания
    high_ts: float = 0.0             # когда этот максимум был
    last_price: float = 0.0
    last_check: float = 0.0

    status: str = "open"             # open | closed
    exit_price: float = 0.0
    exit_ts: float = 0.0
    exit_reason: str = ""
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    row_id: int | None = None

    @property
    def scaled_out(self) -> bool:
        """Часть позиции уже в кармане — дальше едем без риска."""
        return self.sold_pct > 0

    @property
    def age_minutes(self) -> float:
        return max(0.0, (time.time() - self.opened_ts) / 60.0) if self.opened_ts else 0.0

    def change_pct(self, price: float | None = None) -> float:
        price = self.last_price if price is None else price
        if self.entry_price <= 0 or price <= 0:
            return 0.0
        return (price / self.entry_price - 1) * 100.0

    def value_sol(self, price: float, sol_price: float, conf: dict) -> float:
        """Сколько SOL получим, если продадим прямо сейчас."""
        if sol_price <= 0 or price <= 0:
            return 0.0
        gross = self.tokens * price / sol_price
        after = gross * (1 - num(conf.get("exit_slippage_pct")) / 100.0) \
                      * (1 - num(conf.get("fee_pct")) / 100.0)
        return max(0.0, after - num(conf.get("network_fee_sol")))


# ════════════════════════════════════════════════════════════════════════════
#  ХРАНИЛИЩЕ
# ════════════════════════════════════════════════════════════════════════════

TRADE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, symbol TEXT,
    launchpad TEXT, mode TEXT, score REAL, opened_ts REAL, entry_price REAL,
    entry_sol_price REAL, size_sol REAL, tokens REAL, high_price REAL,
    last_price REAL, status TEXT, exit_price REAL, exit_ts REAL,
    exit_reason TEXT, pnl_sol REAL, pnl_pct REAL);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(status, mint);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(opened_ts);
"""


class TradeStore:
    """Живёт в той же базе, что и остальной бот."""

    def __init__(self, storage: Any = None, path: str | Path = "data/memebot.db"):
        if storage is not None and hasattr(storage, "conn"):
            self.conn = storage.conn
            self.lock = getattr(storage, "lock", threading.Lock())
        else:
            p = Path(path)
            if not p.is_absolute():
                p = ROOT / p
            p.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(p), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(TRADE_SCHEMA)
            # у старых баз колонки нет — добавляем, чтобы история не потерялась
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)")}
            if "strategy" not in cols:
                self.conn.execute("ALTER TABLE trades ADD COLUMN strategy TEXT")
            if "realized_sol" not in cols:
                self.conn.execute("ALTER TABLE trades ADD COLUMN realized_sol REAL")
            if "sold_pct" not in cols:
                self.conn.execute("ALTER TABLE trades ADD COLUMN sold_pct REAL")
            for extra, kind in (("meta", "TEXT"), ("ceiling", "TEXT"),
                                ("thesis", "TEXT"), ("high_ts", "REAL")):
                if extra not in cols:
                    self.conn.execute(f"ALTER TABLE trades ADD COLUMN {extra} {kind}")
            self.conn.commit()

    def insert(self, p: Position) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO trades (mint, symbol, launchpad, mode, score, opened_ts,"
                " entry_price, entry_sol_price, size_sol, tokens, high_price, last_price,"
                " status, exit_price, exit_ts, exit_reason, pnl_sol, pnl_pct, strategy,"
                " realized_sol, sold_pct, meta, ceiling, thesis, high_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.mint, p.symbol, p.launchpad, p.mode, p.score, p.opened_ts,
                 p.entry_price, p.entry_sol_price, p.size_sol, p.tokens, p.high_price,
                 p.last_price, p.status, p.exit_price, p.exit_ts, p.exit_reason,
                 p.pnl_sol, p.pnl_pct, p.strategy, p.realized_sol, p.sold_pct,
                 p.meta, p.ceiling, p.thesis, p.high_ts))
            self.conn.commit()
            return int(cur.lastrowid)

    def update(self, p: Position) -> None:
        if p.row_id is None:
            return
        with self.lock:
            self.conn.execute(
                "UPDATE trades SET high_price=?, last_price=?, status=?, exit_price=?,"
                " exit_ts=?, exit_reason=?, pnl_sol=?, pnl_pct=?, tokens=?,"
                " realized_sol=?, sold_pct=?, high_ts=? WHERE id=?",
                (p.high_price, p.last_price, p.status, p.exit_price, p.exit_ts,
                 p.exit_reason, p.pnl_sol, p.pnl_pct, p.tokens,
                 p.realized_sol, p.sold_pct, p.high_ts, p.row_id))
            self.conn.commit()

    def open_positions(self) -> list[Position]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY opened_ts").fetchall()
        return [self._to_position(r) for r in rows]

    def last_trade(self, mint: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM trades WHERE mint=? ORDER BY opened_ts DESC LIMIT 1",
                (mint,)).fetchone()
        return dict(row) if row else None

    def spent_since(self, since_ts: float) -> float:
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(size_sol), 0) AS s FROM trades WHERE opened_ts>=?",
                (since_ts,)).fetchone()
        return num(row["s"]) if row else 0.0

    def recent_closed(self, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE status='closed'"
                " ORDER BY exit_ts DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def all_closed(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def totals(self) -> dict:
        """Итог за всё время работы бота."""
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(pnl_sol),0) AS pnl,"
                " COALESCE(SUM(size_sol),0) AS invested,"
                " COALESCE(SUM(CASE WHEN pnl_sol>0 THEN 1 ELSE 0 END),0) AS wins,"
                " COALESCE(AVG((exit_ts-opened_ts)/60.0),0) AS avg_minutes,"
                " MIN(opened_ts) AS first_ts"
                " FROM trades WHERE status='closed'").fetchone()
        return dict(row) if row else {}

    def closed_since(self, since_ts: float) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE status='closed' AND exit_ts>=?"
                " ORDER BY exit_ts DESC", (since_ts,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _to_position(row: sqlite3.Row) -> Position:
        d = dict(row)
        p = Position(mint=d["mint"], symbol=d.get("symbol") or "",
                     launchpad=d.get("launchpad") or "", mode=d.get("mode") or "paper",
                     score=num(d.get("score")))
        p.opened_ts = num(d.get("opened_ts"))
        p.entry_price = num(d.get("entry_price"))
        p.entry_sol_price = num(d.get("entry_sol_price"))
        p.size_sol = num(d.get("size_sol"))
        p.tokens = num(d.get("tokens"))
        p.high_price = num(d.get("high_price"))
        p.last_price = num(d.get("last_price"))
        p.status = d.get("status") or "open"
        p.exit_price = num(d.get("exit_price"))
        p.exit_ts = num(d.get("exit_ts"))
        p.exit_reason = d.get("exit_reason") or ""
        p.pnl_sol = num(d.get("pnl_sol"))
        p.pnl_pct = num(d.get("pnl_pct"))
        p.strategy = d.get("strategy") or "метрики"
        p.realized_sol = num(d.get("realized_sol"))
        p.sold_pct = num(d.get("sold_pct"))
        p.meta = d.get("meta") or ""
        p.ceiling = d.get("ceiling") or ""
        p.thesis = d.get("thesis") or ""
        p.high_ts = num(d.get("high_ts")) or p.opened_ts
        p.row_id = int(d["id"])
        return p


# ════════════════════════════════════════════════════════════════════════════
#  ЦЕНЫ
# ════════════════════════════════════════════════════════════════════════════

class PriceFeed:
    """Цены токенов и SOL. Jupiter основной, DexScreener запасной."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=15)
        self._sol = 0.0
        self._sol_ts = 0.0

    async def _get(self, url: str, params: dict | None = None) -> Any:
        try:
            async with self.session.get(url, params=params, timeout=self.timeout) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            log.debug("цены %s: %s", url, e)
            return None

    async def sol_price(self) -> float:
        if self._sol and time.time() - self._sol_ts < 300:
            return self._sol
        prices = await self.prices([SOL_MINT])
        price = prices.get(SOL_MINT, 0.0)
        if price:
            self._sol, self._sol_ts = price, time.time()
        return self._sol

    async def prices(self, mints: list[str]) -> dict[str, float]:
        mints = [m for m in dict.fromkeys(mints) if m]
        if not mints:
            return {}
        out: dict[str, float] = {}

        data = await self._get(f"{JUP_LITE}/price/v3", {"ids": ",".join(mints[:50])})
        if isinstance(data, dict):
            for mint, item in data.items():
                if isinstance(item, dict):
                    price = num(item.get("usdPrice")) or num(item.get("price"))
                    if price:
                        out[mint] = price

        missing = [m for m in mints if m not in out]
        for mint in missing:
            data = await self._get(f"{DEX_API}/latest/dex/tokens/{mint}")
            best = 0.0
            for pair in ((data or {}).get("pairs") or [])[:5]:
                if isinstance(pair, dict):
                    best = max(best, num(pair.get("priceUsd")))
            if best:
                out[mint] = best
        return out


# ════════════════════════════════════════════════════════════════════════════
#  ИСПОЛНЕНИЕ СДЕЛОК
# ════════════════════════════════════════════════════════════════════════════

class PaperExecutor:
    """Бумажное исполнение: считаем так, как если бы сделка прошла в сети."""

    mode = "paper"

    def __init__(self, conf: dict[str, Any]):
        self.conf = conf

    async def buy(self, mint: str, size_sol: float, price: float,
                  sol_price: float) -> tuple[float, float]:
        """Возвращает (сколько токенов получили, по какой фактической цене)."""
        if price <= 0 or sol_price <= 0:
            return 0.0, 0.0
        # заходим хуже котировки: проскальзывание + комиссия протокола
        fill = price * (1 + num(self.conf.get("entry_slippage_pct")) / 100.0)
        usable = max(0.0, size_sol - num(self.conf.get("network_fee_sol")))
        usable *= (1 - num(self.conf.get("fee_pct")) / 100.0)
        tokens = usable * sol_price / fill
        return tokens, fill

    async def sell(self, position: Position, price: float, sol_price: float,
                   fraction: float = 1.0) -> tuple[float, float]:
        """Возвращает (сколько SOL получили, по какой фактической цене).
        fraction < 1 — частичная фиксация, остаток позиции продолжает ехать."""
        fill = price * (1 - num(self.conf.get("exit_slippage_pct")) / 100.0)
        if fill <= 0 or sol_price <= 0:
            return 0.0, 0.0
        gross = position.tokens * max(0.0, min(1.0, fraction)) * fill / sol_price
        net = gross * (1 - num(self.conf.get("fee_pct")) / 100.0) \
                    - num(self.conf.get("network_fee_sol"))
        return max(0.0, net), fill


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur if cur is not None else default


class WalletError(Exception):
    """Проблема с кошельком или ключом."""


class Wallet:
    """Кошелёк из приватного ключа. Ключ живёт только в памяти процесса."""

    def __init__(self, secret: str):
        from solders.keypair import Keypair

        secret = (secret or "").strip()
        if not secret:
            raise WalletError("SOLANA_PRIVATE_KEY не задан в .env")
        try:
            if secret.startswith("["):          # формат solana-keygen: массив байтов
                self.keypair = Keypair.from_bytes(bytes(json.loads(secret)))
            else:                               # base58 — так экспортирует Phantom
                self.keypair = Keypair.from_base58_string(secret)
        except Exception as e:                  # noqa: BLE001
            raise WalletError("не смог прочитать приватный ключ — проверь, "
                              "что он скопирован целиком") from e

    @property
    def address(self) -> str:
        return str(self.keypair.pubkey())

    def sign(self, tx_base64: str) -> str:
        """Подписывает транзакцию, собранную Jupiter, и отдаёт готовую к отправке."""
        from solders.transaction import VersionedTransaction

        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_base64))
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        return base64.b64encode(bytes(signed)).decode()


class SolanaRPC:
    """Тонкий клиент RPC: баланс, отправка и подтверждение транзакций."""

    def __init__(self, session: aiohttp.ClientSession, url: str = ""):
        self.session = session
        self.url = url or os.environ.get("SOLANA_RPC_URL", "").strip() or DEFAULT_RPC
        self._id = 0

    async def call(self, method: str, params: list) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        async with self.session.post(self.url, json=payload,
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json(content_type=None)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"RPC {method}: {dig(data, 'error', 'message')}")
        return (data or {}).get("result")

    async def balance_sol(self, address: str) -> float:
        res = await self.call("getBalance", [address])
        return num(dig(res, "value")) / LAMPORTS

    async def token_balance(self, owner: str, mint: str) -> tuple[int, int]:
        """Сколько токенов лежит на кошельке: (сырое количество, знаков после запятой)."""
        res = await self.call("getTokenAccountsByOwner",
                              [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        total, decimals = 0, 0
        for acc in (dig(res, "value", default=[]) or []):
            info = dig(acc, "account", "data", "parsed", "info") or {}
            total += int(num(dig(info, "tokenAmount", "amount")))
            decimals = int(num(dig(info, "tokenAmount", "decimals")))
        return total, decimals

    async def send(self, signed_base64: str) -> str:
        return await self.call("sendTransaction", [signed_base64, {
            "encoding": "base64", "skipPreflight": True, "maxRetries": 3}])

    async def confirm(self, signature: str, timeout: float = 90) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = await self.call("getSignatureStatuses",
                                  [[signature], {"searchTransactionHistory": True}])
            status = (dig(res, "value", default=[None]) or [None])[0]
            if status:
                if status.get("err"):
                    raise RuntimeError(f"сеть отклонила транзакцию: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(2)
        return False


class LiveExecutor(PaperExecutor):
    """Реальные сделки: маршрут считает Jupiter, подпись ставится здесь.

    Jupiter отдаёт неподписанную транзакцию — ключ ей не нужен и не передаётся.
    Подпись происходит локально, в сеть уходит уже подписанная транзакция.
    """

    mode = "live"

    def __init__(self, conf: dict[str, Any], session: aiohttp.ClientSession):
        super().__init__(conf)
        self.session = session
        self.wallet = Wallet(os.environ.get("SOLANA_PRIVATE_KEY", ""))
        self.rpc = SolanaRPC(session, str(conf.get("rpc_url", "")))
        log.info("Кошелёк подключён: %s", self.wallet.address)

    # ---------- Jupiter ----------

    async def _quote(self, input_mint: str, output_mint: str, amount: int) -> dict:
        params = {"inputMint": input_mint, "outputMint": output_mint,
                  "amount": str(int(amount)),
                  "slippageBps": str(int(num(self.conf.get("slippage_bps"), 1000))),
                  "restrictIntermediateTokens": "true"}
        async with self.session.get(f"{JUP_SWAP}/quote", params=params,
                                    timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json(content_type=None)
        if not isinstance(data, dict) or not data.get("outAmount"):
            raise RuntimeError(f"Jupiter не нашёл маршрут: {str(data)[:160]}")
        return data

    async def _swap_tx(self, quote: dict) -> str:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": self.wallet.address,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": int(num(self.conf.get("priority_fee_lamports"), 300000)),
                "priorityLevel": "high"}},
        }
        async with self.session.post(f"{JUP_SWAP}/swap", json=payload,
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json(content_type=None)
        tx = dig(data, "swapTransaction")
        if not tx:
            raise RuntimeError(f"Jupiter не собрал транзакцию: {str(data)[:160]}")
        return tx

    async def _execute(self, quote: dict) -> str:
        signature = await self.rpc.send(self.wallet.sign(await self._swap_tx(quote)))
        if not await self.rpc.confirm(signature, num(self.conf.get("confirm_timeout"), 90)):
            raise RuntimeError("сеть не подтвердила транзакцию за отведённое время: "
                               f"https://solscan.io/tx/{signature}")
        log.info("Транзакция прошла: https://solscan.io/tx/%s", signature)
        return signature

    # ---------- сделки ----------

    async def buy(self, mint: str, size_sol: float, price: float,
                  sol_price: float) -> tuple[float, float]:
        cap = num(self.conf.get("max_trade_sol"), 0.5)
        if cap and size_sol > cap:
            raise RuntimeError(f"размер сделки {size_sol} SOL выше потолка {cap} SOL")

        reserve = num(self.conf.get("min_sol_reserve"), 0.02)
        balance = await self.rpc.balance_sol(self.wallet.address)
        if balance < size_sol + reserve:
            raise RuntimeError(f"на кошельке {balance:.4f} SOL — не хватает на сделку "
                               f"{size_sol:.3f} плюс резерв {reserve:.3f}")

        before, _ = await self.rpc.token_balance(self.wallet.address, mint)
        await self._execute(await self._quote(SOL_MINT, mint, int(size_sol * LAMPORTS)))

        # сколько токенов реально пришло — читаем с кошелька, а не верим котировке
        raw, decimals = before, 0
        for _ in range(10):
            raw, decimals = await self.rpc.token_balance(self.wallet.address, mint)
            if raw > before:
                break
            await asyncio.sleep(2)
        got = (raw - before) / (10 ** decimals or 1)
        if got <= 0:
            raise RuntimeError("транзакция прошла, но токены на кошельке не появились")
        fill = size_sol * sol_price / got
        log.info("Куплено %.4f токенов по %.10f", got, fill)
        return got, fill

    async def sell(self, position: Position, price: float, sol_price: float,
                   fraction: float = 1.0) -> tuple[float, float]:
        raw, decimals = await self.rpc.token_balance(self.wallet.address, position.mint)
        if raw <= 0:
            raise RuntimeError("токенов на кошельке нет — продавать нечего")
        # частичная фиксация: продаём долю остатка, а не всё подчистую
        raw = max(1, int(raw * max(0.0, min(1.0, fraction))))

        sol_before = await self.rpc.balance_sol(self.wallet.address)
        await self._execute(await self._quote(position.mint, SOL_MINT, raw))

        sol_after = sol_before
        for _ in range(10):
            sol_after = await self.rpc.balance_sol(self.wallet.address)
            if sol_after > sol_before:
                break
            await asyncio.sleep(2)
        got_sol = max(0.0, sol_after - sol_before)
        tokens = raw / (10 ** decimals or 1)
        fill = got_sol * sol_price / tokens if tokens else 0.0
        log.info("Продано %.4f токенов, получено %.4f SOL", tokens, got_sol)
        return got_sol, fill


# ════════════════════════════════════════════════════════════════════════════
#  СООБЩЕНИЯ
# ════════════════════════════════════════════════════════════════════════════

def price_str(v: float) -> str:
    v = num(v)
    if v <= 0:
        return "—"
    if v >= 1:
        return f"${v:,.4f}".rstrip("0").rstrip(".")
    return ("$" + f"{v:.12f}".rstrip("0")) if v < 0.0001 else f"${v:.8f}".rstrip("0")


def open_message(p: Position, conf: dict[str, Any]) -> str:
    """Коротко: зашёл в такую-то монету, столько-то, по такой цене."""
    tag = "📄" if p.mode == "paper" else "💰"
    return (f"{tag} 🟢 <b>зашёл ${esc(p.symbol)}</b>\n"
            f"{p.size_sol:.3f} SOL по {price_str(p.entry_price)}")


def close_message(p: Position) -> str:
    """Коротко: вышел, столько плюс или минус, за сколько времени."""
    tag = "📄" if p.mode == "paper" else "💰"
    emoji = "🟢" if p.pnl_sol > 0 else "🔴"
    return (f"{tag} {emoji} <b>вышел ${esc(p.symbol)}</b>\n"
            f"<b>{fmt_sol(p.pnl_sol)}</b> ({p.pnl_pct:+.0f}%) · {p.age_minutes:.0f} мин · "
            f"{EXIT_PLAIN.get(p.exit_reason, p.exit_reason)}\n"
            f"{price_str(p.entry_price)} → {price_str(p.exit_price)}")


def scale_message(p: Position, fill: float, got_sol: float, share: float) -> str:
    """Коротко: снял часть в плюс, остаток едет дальше."""
    tag = "📄" if p.mode == "paper" else "💰"
    return (f"{tag} 💰 <b>снял {share * 100:.0f}% ${esc(p.symbol)}</b>\n"
            f"+{got_sol:.4f} SOL по {price_str(fill)} ({p.change_pct(fill):+.0f}%)\n"
            f"остаток едет дальше, стоп — в ноль")


def positions_message(positions: list[Position], conf: dict[str, Any]) -> str:
    if not positions:
        return "Открытых позиций нет."
    out = [f"📊 <b>Открытые позиции</b> ({len(positions)})"]
    for p in positions:
        change = p.change_pct()
        emoji = "🟢" if change > 0 else "🔴" if change < 0 else "⚪"
        out.append(f"\n{emoji} <b>${esc(p.symbol)}</b> — {change:+.1f}% · "
                   f"{p.size_sol:.3f} SOL · {p.age_minutes:.0f} мин\n"
                   f"   вход {p.entry_price:.10f}".rstrip("0")
                   + f" · сейчас {p.last_price:.10f}".rstrip("0")
                   + f" · макс {p.change_pct(p.high_price):+.0f}%\n"
                   f"   <code>{esc(p.mint)}</code>")
    return "\n".join(out)


def _ago(ts: float) -> str:
    minutes = max(0.0, (time.time() - num(ts)) / 60.0)
    if minutes < 60:
        return f"{minutes:.0f} мин назад"
    if minutes < 1440:
        return f"{minutes/60:.0f} ч назад"
    return f"{minutes/1440:.0f} дн назад"


def history_line(r: dict) -> str:
    """Одна сделка одной строкой — чтобы в сообщение влезало много."""
    pnl = num(r.get("pnl_sol"))
    emoji = "🟢" if pnl > 0 else "🔴"
    held = (num(r.get("exit_ts")) - num(r.get("opened_ts"))) / 60.0
    when = time.strftime("%d.%m %H:%M", time.localtime(num(r.get("exit_ts"))))
    reason = EXIT_PLAIN.get(r.get("exit_reason"), "—")
    return (f"{emoji} <b>${esc(r.get('symbol') or '—')}</b> "
            f"{num(r.get('pnl_pct')):+.0f}% · {pnl:+.4f} SOL · {reason} · "
            f"{held:.0f} мин · скор {num(r.get('score')):.0f} · {when}")


def history_pages(rows: list[dict], header: str = "", chars: int = 3500) -> list[str]:
    """Разбивает историю на сообщения, влезающие в лимит Telegram."""
    if not rows:
        return ["Сделок пока не было."]
    pages: list[str] = []
    buf: list[str] = [header] if header else []
    size = len(header)
    for r in rows:
        line = history_line(r)
        if size + len(line) + 1 > chars and buf:
            pages.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        pages.append("\n".join(buf))
    return pages


def history_message(rows: list[dict], limit: int) -> str:
    """Список последних закрытых сделок."""
    if not rows:
        return "Сделок пока не было."
    out = [f"📜 <b>Последние сделки</b> (показано {len(rows)})"]
    for r in rows:
        pnl = num(r.get("pnl_sol"))
        emoji = "🟢" if pnl > 0 else "🔴"
        held = (num(r.get("exit_ts")) - num(r.get("opened_ts"))) / 60.0
        out.append(
            f"\n{emoji} <b>${esc(r.get('symbol') or '—')}</b> "
            f"{num(r.get('pnl_pct')):+.1f}% · {fmt_sol(pnl)}\n"
            f"   {EXIT_LABELS.get(r.get('exit_reason'), r.get('exit_reason') or '')} · "
            f"держал {held:.0f} мин · {_ago(r.get('exit_ts'))}\n"
            f"   скор {num(r.get('score')):.0f} · вход {num(r.get('size_sol')):.3f} SOL")
    return "\n".join(out)


def lesson(r: dict) -> str:
    """Короткий вывод по закрытой сделке — то, ради чего ведут журнал."""
    pnl = num(r.get("pnl_pct"))
    reason = r.get("exit_reason") or ""
    held = (num(r.get("exit_ts")) - num(r.get("opened_ts"))) / 60.0
    if reason == "stop_loss":
        return ("нарратив не поехал"
                + (", развалилось сразу" if held < 10 else f", {held:.0f} мин на дне"))
    if reason == "decay":
        return "максимум был давно, цена отошла — забрали, пока было что"
    if reason == "breakeven":
        return "половину сняли на удвоении, остаток закрыт по входу — итог в плюсе"
    if reason == "trailing":
        return f"дали доехать, {pnl:+.0f}% и откат от максимума"
    if reason == "take_profit":
        return f"цель отработала за {held:.0f} мин"
    if reason == "timeout":
        return "монета встала на месте, ждать было нечего"
    return f"{pnl:+.0f}% за {held:.0f} мин"


def journal_message(rows: list[dict]) -> str:
    """Журнал: что купили, почему, чем закончилось и какой вывод."""
    if not rows:
        return ("📓 <b>Журнал сделок</b>\n\nПока пусто — записи появятся "
                "после первых закрытых сделок.")
    out = [f"📓 <b>Журнал сделок</b> (последние {len(rows)})"]
    for r in rows:
        pnl = num(r.get("pnl_sol"))
        emoji = "🟢" if pnl > 0 else "🔴"
        when = time.strftime("%d.%m %H:%M", time.localtime(num(r.get("exit_ts"))))
        out.append(
            f"\n{emoji} <b>${esc(r.get('symbol') or '—')}</b> "
            f"{num(r.get('pnl_pct')):+.0f}% · {fmt_sol(pnl)} · {when}\n"
            f"   <i>тезис:</i> {esc(r.get('thesis') or 'без разбора нарратива')}\n"
            f"   <i>выход:</i> {EXIT_PLAIN.get(r.get('exit_reason'), '—')} — "
            f"{esc(lesson(r))}")
    return "\n".join(out)


def pnl_message(rows: list[dict], hours: float, mode: str) -> str:
    if not rows:
        return f"За {hours:.0f}ч закрытых сделок не было."
    wins = [r for r in rows if num(r.get("pnl_sol")) > 0]
    total = sum(num(r.get("pnl_sol")) for r in rows)
    invested = sum(num(r.get("size_sol")) for r in rows)
    best = max(rows, key=lambda r: num(r.get("pnl_pct")))
    worst = min(rows, key=lambda r: num(r.get("pnl_pct")))
    tag = "бумажная" if mode == "paper" else "реальная"
    return "\n".join([
        f"📈 <b>Статистика за {hours:.0f}ч</b> ({tag} торговля)",
        f"Сделок: {len(rows)} · в плюс: {len(wins)} ({len(wins)/len(rows)*100:.0f}%)",
        f"Вложено: {invested:.3f} SOL · итог: <b>{fmt_sol(total)}</b>"
        + (f" ({total/invested*100:+.1f}%)" if invested else ""),
        f"Лучшая: ${esc(best.get('symbol'))} {num(best.get('pnl_pct')):+.0f}%",
        f"Худшая: ${esc(worst.get('symbol'))} {num(worst.get('pnl_pct')):+.0f}%",
    ])


# ════════════════════════════════════════════════════════════════════════════
#  ТРЕЙДЕР
# ════════════════════════════════════════════════════════════════════════════

SendFn = Callable[[str], Awaitable[Any]]


class Trader:
    """Открывает позиции по находкам сканера и сам их ведёт до выхода."""

    def __init__(self, session: aiohttp.ClientSession, storage: Any = None,
                 send: SendFn | None = None, conf: dict[str, Any] | None = None):
        self.conf = {**DEFAULTS, **(conf or {})}
        self.session = session
        self.send = send
        self.prices = PriceFeed(session)
        self.store = TradeStore(storage, self.conf.get("storage_path", "data/memebot.db"))
        self.executor: PaperExecutor = self._make_executor()
        self.positions: list[Position] = self.store.open_positions()
        self.stop_event = asyncio.Event()
        self.last_error = ""
        if self.positions:
            log.info("Подхватил %d открытых позиций из базы", len(self.positions))

    def _make_executor(self) -> PaperExecutor:
        """Живой режим — только если кошелёк реально удалось открыть.

        Не смогли (нет ключа, кривой ключ, нет solders) — честно откатываемся
        на бумагу и говорим об этом, а не делаем вид, что торгуем.
        """
        if str(self.conf.get("mode", "paper")).lower() != "live":
            return PaperExecutor(self.conf)
        try:
            return LiveExecutor(self.conf, self.session)
        except WalletError as e:
            log.error("Живой режим не включился (%s) — работаю на бумаге", e)
        except ImportError:
            log.error("Для живого режима нужны solders и base58. Поставь их: "
                      "pip install -r requirements-live.txt. Работаю на бумаге")
        except Exception as e:  # noqa: BLE001
            log.error("Живой режим не включился: %s — работаю на бумаге", e)
        self.conf["mode"] = "paper"
        return PaperExecutor(self.conf)

    @property
    def mode(self) -> str:
        return self.executor.mode

    @property
    def wallet_address(self) -> str:
        wallet = getattr(self.executor, "wallet", None)
        return wallet.address if wallet else ""

    async def wallet_info(self) -> str:
        """Адрес кошелька и баланс — для команды /wallet."""
        if self.mode != "live":
            return ("Режим: <b>бумажный</b> — кошелёк не подключён, "
                    "реальные деньги не тратятся.\n"
                    "Чтобы включить: SOLANA_PRIVATE_KEY в .env и mode: live.")
        try:
            balance = await self.executor.rpc.balance_sol(self.wallet_address)
        except Exception as e:  # noqa: BLE001
            return f"Кошелёк: <code>{esc(self.wallet_address)}</code>\nБаланс не прочитался: {esc(e)}"
        return "\n".join([
            "💰 <b>Кошелёк бота</b>",
            f"<code>{esc(self.wallet_address)}</code>",
            f"Баланс: <b>{balance:.4f} SOL</b>",
            f"Размер сделки: {num(self.conf.get('size_sol')):.3f} SOL · "
            f"лимит {num(self.conf.get('daily_limit_sol')):.2f} SOL в сутки",
        ])

    # ---------- вход ----------

    def _blocked(self, mint: str, score: float) -> str:
        """Почему в эту монету заходить нельзя. Пустая строка — можно."""
        if not self.conf.get("enabled", True):
            return "торговля выключена"
        if score < num(self.conf.get("min_score")):
            return f"скор {score:.0f} ниже торгового порога"
        if any(p.mint == mint for p in self.positions):
            return "позиция по этой монете уже открыта"
        if len(self.positions) >= int(num(self.conf.get("max_positions"), 3)):
            return f"уже {len(self.positions)} открытых позиций"

        last = self.store.last_trade(mint)
        cooldown = num(self.conf.get("cooldown_minutes"), 120)
        if last and time.time() - num(last.get("opened_ts")) < cooldown * 60:
            return "недавно уже торговали эту монету"

        size = num(self.conf.get("size_sol"), 0.1)
        spent = self.store.spent_since(time.time() - 86400)
        limit = num(self.conf.get("daily_limit_sol"), 1.0)
        if limit and spent + size > limit:
            return f"дневной лимит: потрачено {spent:.2f} из {limit:.2f} SOL"
        return ""

    async def consider(self, mint: str, symbol: str = "", score: float = 0.0,
                       launchpad: str = "", price_hint: float = 0.0,
                       strategy: str = "метрики", meta: str = "",
                       ceiling: str = "", thesis: str = "",
                       size_mult: float = 1.0) -> Position | None:
        """Решает, входить ли в монету, и открывает позицию."""
        reason = self._blocked(mint, score)
        if reason:
            log.info("Пропускаю $%s: %s", symbol or mint[:8], reason)
            return None

        sol_price = await self.prices.sol_price()
        price = (await self.prices.prices([mint])).get(mint) or price_hint
        if price <= 0 or sol_price <= 0:
            log.warning("Нет цены для $%s — вход отменён", symbol or mint[:8])
            return None

        # клонам чужого нарратива даём меньше денег: потолок ниже, риск тот же
        size = num(self.conf.get("size_sol"), 0.1) * max(0.3, min(1.0, num(size_mult, 1.0)))
        size = round(size, 4)
        try:
            tokens, fill = await self.executor.buy(mint, size, price, sol_price)
        except Exception as e:  # noqa: BLE001
            # не влезли в маршрут, не хватило баланса, сеть отбила транзакцию —
            # пропускаем монету, но говорим об этом вслух, а не молча
            self.last_error = str(e)[:200]
            log.error("Вход в $%s не состоялся: %s", symbol or mint[:8], e)
            if self.send and self.mode == "live":
                await self.send(f"⚠️ Не смог купить <b>${esc(symbol)}</b>: {esc(e)}")
            return None
        if tokens <= 0:
            return None

        p = Position(mint=mint, symbol=symbol, launchpad=launchpad, mode=self.mode,
                     score=score, strategy=strategy, meta=meta, ceiling=ceiling,
                     thesis=thesis, opened_ts=time.time(), entry_price=fill,
                     entry_sol_price=sol_price, size_sol=size, tokens=tokens,
                     high_price=fill, high_ts=time.time(),
                     last_price=fill, last_check=time.time())
        p.row_id = self.store.insert(p)
        self.positions.append(p)
        log.info("Вход $%s по %.10f, %.3f SOL (%s)", symbol or mint[:8], fill, size, self.mode)
        if self.send:
            await self.send(open_message(p, self.conf))
        return p

    # ---------- выход ----------

    def rules(self, p: Position) -> dict[str, Any]:
        """Правила выхода: общие, поправки стратегии и поправки на потолок.

        Потолок идёт последним: клон чужого нарратива ведём как скальп, даже
        если зашли по кошелькам, — иксов там всё равно не будет.
        """
        by_strategy = (self.conf.get("rules") or {}).get(p.strategy) or {}
        by_ceiling = (self.conf.get("ceiling_rules") or {}).get(p.ceiling) or {}
        return {**self.conf, **by_strategy, **by_ceiling}

    def _exit_reason(self, p: Position, price: float) -> str:
        r = self.rules(p)
        change = p.change_pct(price)

        take = num(r.get("take_profit_pct"), 60)
        if take > 0 and change >= take:
            return "take_profit"

        # половина уже в кармане — дальше не рискуем ничем: вернулось к входу,
        # забираем остаток и всё равно остаёмся в плюсе
        if p.scaled_out and r.get("breakeven_after_scale") and change <= 0:
            return "breakeven"
        if change <= num(r.get("stop_loss_pct"), -35):
            return "stop_loss"

        # Хайп кончился: максимум был давно, цена от него отошла и не растёт.
        # Ждать стопа в этом случае — отдавать назад уже заработанное.
        decay_after = num(r.get("decay_after_pct"), 40)
        stall = num(r.get("decay_stall_minutes"), 12)
        drop = num(r.get("decay_drop_pct"), 15)
        if decay_after and stall and p.high_ts and p.high_price > 0 \
                and p.change_pct(p.high_price) >= decay_after \
                and (time.time() - p.high_ts) / 60.0 >= stall \
                and (price / p.high_price - 1) * 100.0 <= -drop:
            return "decay"

        trail_after = num(r.get("trailing_after_pct"), 30)
        trail = num(r.get("trailing_stop_pct"), 25)
        if trail and p.high_price > 0 and p.change_pct(p.high_price) >= trail_after:
            drop = (price / p.high_price - 1) * 100.0
            if drop <= -trail:
                return "trailing"

        timeout = num(r.get("timeout_minutes"), 30)
        if timeout and p.age_minutes >= timeout:
            return "timeout"
        return ""

    async def scale_out(self, p: Position, price: float, sol_price: float,
                        share: float) -> None:
        """Снимает часть позиции в плюс, остаток оставляет ехать дальше."""
        share = max(0.05, min(0.9, share))
        try:
            got_sol, fill = await self.executor.sell(p, price, sol_price, share)
        except Exception as e:  # noqa: BLE001
            # не получилось — позиция цела, попробуем на следующем круге
            self.last_error = str(e)[:200]
            log.error("Частичный выход из $%s не прошёл: %s", p.symbol or p.mint[:8], e)
            return
        if got_sol <= 0:
            return
        p.realized_sol += got_sol
        p.tokens = max(0.0, p.tokens * (1 - share))
        p.sold_pct = min(100.0, p.sold_pct + share * 100.0)
        self.store.update(p)
        log.info("Снял %.0f%% $%s: +%.4f SOL (%+.0f%%)",
                 share * 100, p.symbol or p.mint[:8], got_sol, p.change_pct(fill))
        if self.send:
            await self.send(scale_message(p, fill, got_sol, share))

    async def close(self, p: Position, price: float, sol_price: float,
                    reason: str) -> None:
        try:
            got_sol, fill = await self.executor.sell(p, price, sol_price)
        except Exception as e:  # noqa: BLE001
            # продажа не прошла — позиция остаётся открытой и будет
            # переоценена в следующем цикле, а не потеряется
            self.last_error = str(e)[:200]
            log.error("Выход из $%s не состоялся: %s", p.symbol or p.mint[:8], e)
            if self.send and self.mode == "live":
                await self.send(f"⚠️ Не смог продать <b>${esc(p.symbol)}</b>: {esc(e)}\n"
                                f"Позиция осталась открытой, попробую снова.")
            return
        p.status = "closed"
        p.exit_price = fill
        p.exit_ts = time.time()
        p.exit_reason = reason
        # к выручке добавляем то, что сняли раньше частичной продажей
        p.pnl_sol = p.realized_sol + got_sol - p.size_sol
        p.pnl_pct = (p.pnl_sol / p.size_sol * 100.0) if p.size_sol else 0.0
        p.last_price = price
        self.store.update(p)
        self.positions = [x for x in self.positions if x.row_id != p.row_id]
        log.info("Выход $%s (%s): %+.4f SOL (%+.1f%%)",
                 p.symbol or p.mint[:8], reason, p.pnl_sol, p.pnl_pct)
        if self.send:
            await self.send(close_message(p))

    async def refresh(self) -> None:
        """Переоценить открытые позиции и закрыть те, где сработало условие."""
        if not self.positions:
            return
        sol_price = await self.prices.sol_price()
        prices = await self.prices.prices([p.mint for p in self.positions])

        for p in list(self.positions):
            price = prices.get(p.mint, 0.0)
            if price <= 0:
                # цены нет — монета могла умереть; выходим по таймауту, не раньше
                if num(self.conf.get("timeout_minutes")) and \
                        p.age_minutes >= num(self.conf.get("timeout_minutes")) * 2:
                    await self.close(p, p.last_price, sol_price, "timeout")
                continue
            p.last_price = price
            if price > p.high_price:
                p.high_price, p.high_ts = price, time.time()
            p.last_check = time.time()

            # сначала частичная фиксация: снять половину на удвоении важнее,
            # чем ждать общего выхода — на этом и держится стратегия «кимчи»
            r = self.rules(p)
            at = num(r.get("scale_out_at_pct"))
            share = num(r.get("scale_out_pct")) / 100.0
            if not p.scaled_out and at > 0 and share > 0 and p.change_pct(price) >= at:
                await self.scale_out(p, price, sol_price, share)

            reason = self._exit_reason(p, price)
            if reason:
                await self.close(p, price, sol_price, reason)
            else:
                self.store.update(p)

    # ---------- цикл ----------

    async def loop(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or self.stop_event
        interval = num(self.conf.get("poll_seconds"), 30)
        while not stop.is_set():
            try:
                await self.refresh()
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:200]
                log.exception("сбой ведения позиций: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # ---------- отчёты ----------

    def stats(self, hours: float = 24) -> str:
        """Сводка за период + итог за всё время + плавающий результат по открытым."""
        text = pnl_message(self.store.closed_since(time.time() - hours * 3600),
                           hours, self.mode)
        t = self.store.totals()
        if num(t.get("n")):
            invested = num(t.get("invested"))
            pnl = num(t.get("pnl"))
            since = (f" (первая сделка {_ago(t.get('first_ts'))})"
                     if t.get("first_ts") else "")
            text += ("\n\n<b>За всё время</b>" + esc(since) + ":\n"
                     f"Сделок: {int(num(t.get('n')))} · в плюс: {int(num(t.get('wins')))}"
                     f" ({num(t.get('wins'))/num(t.get('n'))*100:.0f}%)\n"
                     f"Итог: <b>{fmt_sol(pnl)}</b>"
                     + (f" ({pnl/invested*100:+.1f}% от вложенного)" if invested else "")
                     + f"\nСредняя сделка длилась {num(t.get('avg_minutes')):.0f} мин")

        if self.positions:
            floating = sum(p.change_pct() for p in self.positions) / len(self.positions)
            text += (f"\n\nСейчас открыто: {len(self.positions)} "
                     f"(в среднем {floating:+.1f}%)")
        return text

    def by_strategy(self, hours: float = 24 * 7) -> str:
        """Какая стратегия сколько принесла — по этому видно, что работает."""
        rows = self.store.closed_since(time.time() - hours * 3600)
        if not rows:
            return "Закрытых сделок пока нет — сравнивать нечего."
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r.get("strategy") or "метрики", []).append(r)

        out = [f"🎯 <b>Что приносит результат</b> (за {hours / 24:.0f} дн)"]
        for name, items in sorted(groups.items(),
                                  key=lambda kv: -sum(num(r.get("pnl_sol")) for r in kv[1])):
            pnl = sum(num(r.get("pnl_sol")) for r in items)
            wins = len([r for r in items if num(r.get("pnl_sol")) > 0])
            invested = sum(num(r.get("size_sol")) for r in items)
            out.append(f"\n<b>{esc(name)}</b> — {fmt_sol(pnl)}"
                       + (f" ({pnl / invested * 100:+.0f}%)" if invested else "")
                       + f"\n  сделок {len(items)}, в плюс {wins} "
                         f"({wins / len(items) * 100:.0f}%)")
        return "\n".join(out)

    def journal(self, limit: int = 8) -> str:
        return journal_message(self.store.recent_closed(max(1, min(limit, 20))))

    def by_meta(self, hours: float = 24 * 7) -> str:
        """Какая мета приносит: то же самое, что по стратегиям, но по смыслу."""
        rows = [r for r in self.store.closed_since(time.time() - hours * 3600)
                if (r.get("meta") or "").strip()]
        if not rows:
            return "Сделок с распознанной метой пока нет."
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["meta"], []).append(r)
        out = [f"🌊 <b>Какая мета кормит</b> (за {hours / 24:.0f} дн)"]
        for name, items in sorted(groups.items(),
                                  key=lambda kv: -sum(num(r.get("pnl_sol")) for r in kv[1])):
            pnl = sum(num(r.get("pnl_sol")) for r in items)
            wins = len([r for r in items if num(r.get("pnl_sol")) > 0])
            out.append(f"\n<b>{esc(name)}</b> — {fmt_sol(pnl)}\n"
                       f"  сделок {len(items)}, в плюс {wins}")
        return "\n".join(out)

    def history(self, limit: int = 10) -> str:
        return history_message(self.store.recent_closed(max(1, min(limit, 30))), limit)

    def history_all(self) -> list[str]:
        """Вся история: закрытые сделки плюс то, что держим прямо сейчас."""
        rows = self.store.all_closed()
        if not rows and not self.positions:
            return ["Сделок пока не было."]
        wins = len([r for r in rows if num(r.get("pnl_sol")) > 0])
        total = sum(num(r.get("pnl_sol")) for r in rows)
        header = (f"📜 <b>Все сделки: {len(rows)}</b>"
                  + (f" · в плюс {wins} ({wins/len(rows)*100:.0f}%)" if rows else "")
                  + f" · итог <b>{fmt_sol(total)}</b>\n")
        pages = []
        if self.positions:
            open_lines = ["<b>Сейчас в позиции:</b>"]
            open_lines += [
                f"⏳ <b>${esc(p.symbol or p.mint[:6])}</b> {p.change_pct():+.0f}% · "
                f"{p.size_sol:.3f} SOL · {p.age_minutes:.0f} мин · скор {p.score:.0f}"
                for p in self.positions]
            pages.append(header + "\n".join(open_lines))
            header = ""
        return pages + history_pages(rows, header) if rows else pages

    def export_csv(self, path: str | Path | None = None) -> Path | None:
        """Выгрузка всех сделок таблицей — открывается в Excel."""
        rows = self.store.all_closed() + [
            {**p.__dict__, "status": "open"} for p in self.positions]
        if not rows:
            return None
        out = Path(path) if path else Path(
            self.conf.get("storage_path", "data/memebot.db"))
        if not path:
            if not out.is_absolute():
                out = ROOT / out
            out = out.parent / "trades.csv"
        out.parent.mkdir(parents=True, exist_ok=True)

        def when(ts: Any) -> str:
            ts = num(ts)
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""

        with open(out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["Вход", "Выход", "Тикер", "Лончпад", "Режим", "Скор",
                        "Размер SOL", "Цена входа", "Цена выхода", "Причина выхода",
                        "Минут в позиции", "Итог SOL", "Итог %", "Статус",
                        "Стратегия", "Мета", "Потолок", "Тезис", "Вывод", "Минт"])
            for r in rows:
                held = ((num(r.get("exit_ts")) - num(r.get("opened_ts"))) / 60.0
                        if num(r.get("exit_ts")) else
                        (time.time() - num(r.get("opened_ts"))) / 60.0)
                w.writerow([
                    when(r.get("opened_ts")), when(r.get("exit_ts")),
                    r.get("symbol") or "", r.get("launchpad") or "",
                    r.get("mode") or "", f"{num(r.get('score')):.0f}",
                    f"{num(r.get('size_sol')):.4f}".replace(".", ","),
                    f"{num(r.get('entry_price')):.12f}".replace(".", ","),
                    f"{num(r.get('exit_price')):.12f}".replace(".", ","),
                    EXIT_PLAIN.get(r.get("exit_reason"), r.get("exit_reason") or ""),
                    f"{held:.0f}",
                    f"{num(r.get('pnl_sol')):.4f}".replace(".", ","),
                    f"{num(r.get('pnl_pct')):.1f}".replace(".", ","),
                    "закрыта" if r.get("status") == "closed" else "открыта",
                    r.get("strategy") or "", r.get("meta") or "",
                    r.get("ceiling") or "", r.get("thesis") or "",
                    lesson(r) if r.get("status") == "closed" else "",
                    r.get("mint") or "",
                ])
        return out

    def status_line(self) -> str:
        spent = self.store.spent_since(time.time() - 86400)
        limit = num(self.conf.get("daily_limit_sol"), 1.0)
        state = "вкл" if self.conf.get("enabled", True) else "выкл"
        return (f"Торговля [{self.mode}]: {state}, позиций {len(self.positions)}"
                f"/{int(num(self.conf.get('max_positions'), 3))}, "
                f"за сутки {spent:.2f}/{limit:.2f} SOL, "
                f"размер сделки {num(self.conf.get('size_sol')):.3f} SOL"
                + (f", ошибка: {esc(self.last_error)}" if self.last_error else ""))


TRADE_HELP = (
    "/positions — открытые позиции\n"
    "/pnl [часы] — результат торговли\n"
    "/history [N] — последние сделки · /history all — вообще все\n"
    "/export — все сделки таблицей в файл\n"
    "/trade [on|off] — включить или остановить входы\n"
    "/close &lt;mint&gt; — закрыть позицию вручную\n"
    "/size [SOL] — размер одной сделки"
)
