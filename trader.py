#!/usr/bin/env python3
"""
Торговый модуль: ведёт позиции по монетам, которые нашёл axiom_scout.

Режимы:
  paper — бумажная торговля (по умолчанию). Сделки только считаются: бот пишет,
          по какой цене вошёл бы, ведёт позицию, закрывает по тейку/стопу/таймауту
          и копит статистику. Реальных транзакций нет вообще.
  live  — реальные сделки своим кошельком. Пока не подключено: сначала гоняем
          бумагу, смотрим статистику, правим настройки, и только потом деньги.

Бумажный расчёт честный: с проскальзыванием на входе и выходе, комиссией
протокола и сетевым сбором. Иначе статистика врёт в плюс и решение принимается
по выдуманным цифрам.
"""

from __future__ import annotations

import asyncio
import logging
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

    # ---- реализм бумажных сделок ----
    "entry_slippage_pct": 2.0,
    "exit_slippage_pct": 2.0,
    "fee_pct": 1.0,                  # своп + комиссия лончпада
    "network_fee_sol": 0.0005,       # сеть + приоритетка на сделку

    "poll_seconds": 30,              # как часто переоценивать позиции
    "storage_path": "data/memebot.db",
}

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_LITE = "https://lite-api.jup.ag"
DEX_API = "https://api.dexscreener.com"

EXIT_LABELS = {
    "take_profit": "🎯 тейк-профит",
    "stop_loss": "🛑 стоп-лосс",
    "trailing": "📉 трейлинг-стоп",
    "timeout": "⏳ таймаут",
    "manual": "✋ вручную",
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

    opened_ts: float = 0.0
    entry_price: float = 0.0         # цена токена в долларах на входе
    entry_sol_price: float = 0.0     # курс SOL на входе
    size_sol: float = 0.0            # сколько SOL вложили (с учётом сборов)
    tokens: float = 0.0              # сколько токенов получили

    high_price: float = 0.0          # максимум цены за время удержания
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
            self.conn.commit()

    def insert(self, p: Position) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO trades (mint, symbol, launchpad, mode, score, opened_ts,"
                " entry_price, entry_sol_price, size_sol, tokens, high_price, last_price,"
                " status, exit_price, exit_ts, exit_reason, pnl_sol, pnl_pct)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.mint, p.symbol, p.launchpad, p.mode, p.score, p.opened_ts,
                 p.entry_price, p.entry_sol_price, p.size_sol, p.tokens, p.high_price,
                 p.last_price, p.status, p.exit_price, p.exit_ts, p.exit_reason,
                 p.pnl_sol, p.pnl_pct))
            self.conn.commit()
            return int(cur.lastrowid)

    def update(self, p: Position) -> None:
        if p.row_id is None:
            return
        with self.lock:
            self.conn.execute(
                "UPDATE trades SET high_price=?, last_price=?, status=?, exit_price=?,"
                " exit_ts=?, exit_reason=?, pnl_sol=?, pnl_pct=? WHERE id=?",
                (p.high_price, p.last_price, p.status, p.exit_price, p.exit_ts,
                 p.exit_reason, p.pnl_sol, p.pnl_pct, p.row_id))
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

    async def sell(self, position: Position, price: float,
                   sol_price: float) -> tuple[float, float]:
        """Возвращает (сколько SOL получили, по какой фактической цене)."""
        fill = price * (1 - num(self.conf.get("exit_slippage_pct")) / 100.0)
        if fill <= 0 or sol_price <= 0:
            return 0.0, 0.0
        gross = position.tokens * fill / sol_price
        net = gross * (1 - num(self.conf.get("fee_pct")) / 100.0) \
                    - num(self.conf.get("network_fee_sol"))
        return max(0.0, net), fill


class LiveExecutor(PaperExecutor):
    """Реальные сделки своим кошельком — пока не подключено.

    Когда будем включать: приватный ключ кошелька читается из переменной
    SOLANA_PRIVATE_KEY в .env, транзакция собирается через Jupiter Swap API
    и подписывается локально. Ключ никуда не отправляется и в логи не пишется.
    Включать только после того, как бумажная статистика покажет смысл.
    """

    mode = "live"

    async def buy(self, mint: str, size_sol: float, price: float,
                  sol_price: float) -> tuple[float, float]:
        raise NotImplementedError(
            "Реальная торговля ещё не подключена — работаем в режиме paper")

    async def sell(self, position: Position, price: float,
                   sol_price: float) -> tuple[float, float]:
        raise NotImplementedError(
            "Реальная торговля ещё не подключена — работаем в режиме paper")


# ════════════════════════════════════════════════════════════════════════════
#  СООБЩЕНИЯ
# ════════════════════════════════════════════════════════════════════════════

def open_message(p: Position, conf: dict[str, Any]) -> str:
    tag = "📄 БУМАЖНАЯ СДЕЛКА" if p.mode == "paper" else "💰 СДЕЛКА"
    return "\n".join([
        f"{tag} · вход <b>${esc(p.symbol)}</b>",
        f"Скор монеты: {p.score:.0f}/100 · {esc(p.launchpad or 'solana')}",
        f"Вход по {p.entry_price:.10f}".rstrip("0") + f" · размер {p.size_sol:.3f} SOL",
        f"Тейк {num(conf.get('take_profit_pct')):+.0f}% · "
        f"стоп {num(conf.get('stop_loss_pct')):+.0f}% · "
        f"таймаут {num(conf.get('timeout_minutes')):.0f} мин",
        f"<code>{esc(p.mint)}</code>",
    ])


def close_message(p: Position) -> str:
    tag = "📄" if p.mode == "paper" else "💰"
    emoji = "🟢" if p.pnl_sol > 0 else "🔴"
    return "\n".join([
        f"{tag} {emoji} выход <b>${esc(p.symbol)}</b> — {EXIT_LABELS.get(p.exit_reason, p.exit_reason)}",
        f"Итог: <b>{fmt_sol(p.pnl_sol)}</b> ({p.pnl_pct:+.1f}%) за {p.age_minutes:.0f} мин",
        f"Вход {p.entry_price:.10f}".rstrip("0") + f" → выход {p.exit_price:.10f}".rstrip("0"),
        f"<code>{esc(p.mint)}</code>",
    ])


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
        self.executor: PaperExecutor = (LiveExecutor(self.conf)
                                        if self.conf.get("mode") == "live"
                                        else PaperExecutor(self.conf))
        self.positions: list[Position] = self.store.open_positions()
        self.stop_event = asyncio.Event()
        self.last_error = ""
        if self.positions:
            log.info("Подхватил %d открытых позиций из базы", len(self.positions))

    @property
    def mode(self) -> str:
        return self.executor.mode

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
                       launchpad: str = "", price_hint: float = 0.0) -> Position | None:
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

        size = num(self.conf.get("size_sol"), 0.1)
        try:
            tokens, fill = await self.executor.buy(mint, size, price, sol_price)
        except NotImplementedError as e:
            log.error("%s", e)
            return None
        if tokens <= 0:
            return None

        p = Position(mint=mint, symbol=symbol, launchpad=launchpad, mode=self.mode,
                     score=score, opened_ts=time.time(), entry_price=fill,
                     entry_sol_price=sol_price, size_sol=size, tokens=tokens,
                     high_price=fill, last_price=fill, last_check=time.time())
        p.row_id = self.store.insert(p)
        self.positions.append(p)
        log.info("Вход $%s по %.10f, %.3f SOL (%s)", symbol or mint[:8], fill, size, self.mode)
        if self.send:
            await self.send(open_message(p, self.conf))
        return p

    # ---------- выход ----------

    def _exit_reason(self, p: Position, price: float) -> str:
        change = p.change_pct(price)
        if change >= num(self.conf.get("take_profit_pct"), 60):
            return "take_profit"
        if change <= num(self.conf.get("stop_loss_pct"), -35):
            return "stop_loss"

        trail_after = num(self.conf.get("trailing_after_pct"), 30)
        trail = num(self.conf.get("trailing_stop_pct"), 25)
        if trail and p.high_price > 0 and p.change_pct(p.high_price) >= trail_after:
            drop = (price / p.high_price - 1) * 100.0
            if drop <= -trail:
                return "trailing"

        timeout = num(self.conf.get("timeout_minutes"), 30)
        if timeout and p.age_minutes >= timeout:
            return "timeout"
        return ""

    async def close(self, p: Position, price: float, sol_price: float,
                    reason: str) -> None:
        got_sol, fill = await self.executor.sell(p, price, sol_price)
        p.status = "closed"
        p.exit_price = fill
        p.exit_ts = time.time()
        p.exit_reason = reason
        p.pnl_sol = got_sol - p.size_sol
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
            p.high_price = max(p.high_price, price)
            p.last_check = time.time()

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
        return pnl_message(self.store.closed_since(time.time() - hours * 3600),
                           hours, self.mode)

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
    "/trade [on|off] — включить или остановить входы\n"
    "/close &lt;mint&gt; — закрыть позицию вручную\n"
    "/size [SOL] — размер одной сделки"
)
