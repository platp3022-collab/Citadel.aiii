# -*- coding: utf-8 -*-
"""SQLite-хранилище: стратегии, позиции, сделки, кривая эквити, состояние бота."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .genome import Genome

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    genome TEXT NOT NULL,
    score REAL,
    metrics TEXT,
    created_at INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL DEFAULT 0,
    stop REAL, take REAL, trail REAL, peak REAL,
    opened_at INTEGER, opened_bar INTEGER, bars INTEGER DEFAULT 0,
    strategy_id INTEGER
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL, price REAL, cost REAL, fee REAL, pnl REAL,
    reason TEXT, live INTEGER DEFAULT 0, ts INTEGER NOT NULL,
    order_id TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts INTEGER PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_strat_symbol ON strategies(symbol, active);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
"""


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Дописывает колонки, появившиеся в новых версиях, в уже созданную базу."""
        for table, column, ddl in (("positions", "entry_fee", "REAL DEFAULT 0"),):
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.db.close()

    # ── состояние ───────────────────────────────────────────────────────────
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    # ── стратегии ───────────────────────────────────────────────────────────
    def save_strategy(self, symbol: str, timeframe: str, g: Genome,
                      score: float, metrics: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO strategies(symbol,timeframe,genome,score,metrics,created_at,active)"
            " VALUES(?,?,?,?,?,?,0)",
            (symbol, timeframe, g.to_json(), score,
             json.dumps(metrics, ensure_ascii=False), int(time.time())))
        self.db.commit()
        return int(cur.lastrowid)

    def activate(self, strategy_id: int, symbol: str) -> None:
        self.db.execute("UPDATE strategies SET active=0 WHERE symbol=?", (symbol,))
        self.db.execute("UPDATE strategies SET active=1 WHERE id=?", (strategy_id,))
        self.db.commit()

    def active_strategy(self, symbol: str):
        return self.db.execute(
            "SELECT * FROM strategies WHERE symbol=? AND active=1 ORDER BY id DESC LIMIT 1",
            (symbol,)).fetchone()

    def strategy_history(self, symbol: str, limit: int = 10):
        return self.db.execute(
            "SELECT * FROM strategies WHERE symbol=? ORDER BY id DESC LIMIT ?",
            (symbol, limit)).fetchall()

    # ── позиции ─────────────────────────────────────────────────────────────
    def upsert_position(self, symbol: str, **kw) -> None:
        cols = ("qty", "entry_price", "entry_fee", "stop", "take", "trail", "peak",
                "opened_at", "opened_bar", "bars", "strategy_id")
        vals = [kw.get(c) for c in cols]
        self.db.execute(
            f"INSERT INTO positions(symbol,{','.join(cols)}) VALUES(?,{','.join('?' * len(cols))}) "
            f"ON CONFLICT(symbol) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols)}",
            [symbol] + vals)
        self.db.commit()

    def get_position(self, symbol: str):
        return self.db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()

    def all_positions(self):
        return self.db.execute("SELECT * FROM positions WHERE qty>0").fetchall()

    def drop_position(self, symbol: str) -> None:
        self.db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self.db.commit()

    # ── сделки и эквити ─────────────────────────────────────────────────────
    def log_trade(self, symbol: str, side: str, qty: float, price: float, cost: float,
                  fee: float, pnl: float, reason: str, live: bool, order_id: str = "") -> None:
        self.db.execute(
            "INSERT INTO trades(symbol,side,qty,price,cost,fee,pnl,reason,live,ts,order_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, side, qty, price, cost, fee, pnl, reason, int(live), int(time.time()), order_id))
        self.db.commit()

    def trades_after(self, symbol: str, last_id: int, limit: int = 50):
        """Сделки, появившиеся после указанного id — панель дорисовывает их сразу."""
        return self.db.execute(
            "SELECT * FROM trades WHERE symbol=? AND id>? ORDER BY id LIMIT ?",
            (symbol, int(last_id), limit)).fetchall()

    def trade_counts(self) -> dict[str, int]:
        """Сколько сделок по каждому инструменту — панель показывает это на вкладках."""
        rows = self.db.execute(
            "SELECT symbol, COUNT(*) n FROM trades GROUP BY symbol").fetchall()
        return {r["symbol"]: int(r["n"]) for r in rows}

    def last_trade_id(self) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(id),0) m FROM trades").fetchone()
        return int(row["m"] or 0)

    def recent_trades(self, limit: int = 20):
        return self.db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def pnl_since(self, ts: int) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(pnl),0) s FROM trades WHERE ts>=? AND side='sell'",
                              (ts,)).fetchone()
        return float(row["s"] or 0.0)

    def log_equity(self, equity: float, cash: float) -> None:
        self.db.execute("INSERT INTO equity(ts,equity,cash) VALUES(?,?,?)"
                        " ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity, cash=excluded.cash",
                        (int(time.time()), equity, cash))
        self.db.commit()

    def equity_curve(self, limit: int = 500):
        rows = self.db.execute("SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return list(reversed(rows))
