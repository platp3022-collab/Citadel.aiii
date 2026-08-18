"""Хранилище Полки: SQLite без ORM.

Одно соединение, все обращения из asyncio уходят в поток через ``asyncio.to_thread``,
поэтому публичные функции — корутины.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS shelves (
    id          TEXT PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    subtitle    TEXT NOT NULL DEFAULT '',
    sort        INTEGER NOT NULL DEFAULT 100,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS thoughts (
    id          TEXT PRIMARY KEY,
    raw         TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    shelf       TEXT NOT NULL DEFAULT 'inbox',
    kind        TEXT NOT NULL DEFAULT 'note',
    importance  INTEGER NOT NULL DEFAULT 2,
    status      TEXT NOT NULL DEFAULT 'shelved',
    source      TEXT NOT NULL DEFAULT 'telegram',
    tags        TEXT NOT NULL DEFAULT '[]',
    claude_prompt TEXT NOT NULL DEFAULT '',
    claude_worthy INTEGER NOT NULL DEFAULT 0,
    pending     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_thoughts_shelf ON thoughts(shelf, status);
CREATE INDEX IF NOT EXISTS idx_thoughts_created ON thoughts(created_at DESC);

CREATE TABLE IF NOT EXISTS reminders (
    id           TEXT PRIMARY KEY,
    thought_id   TEXT NOT NULL REFERENCES thoughts(id) ON DELETE CASCADE,
    mode         TEXT NOT NULL DEFAULT 'once',       -- once | every
    interval_min INTEGER NOT NULL DEFAULT 0,
    next_fire_at REAL NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    escalation   INTEGER NOT NULL DEFAULT 0,
    fire_count   INTEGER NOT NULL DEFAULT 0,
    last_fired_at REAL,
    acked_at     REAL,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(active, next_fire_at);

CREATE TABLE IF NOT EXISTS reminder_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id TEXT NOT NULL,
    thought_id  TEXT NOT NULL,
    fired_at    REAL NOT NULL,
    escalation  INTEGER NOT NULL DEFAULT 0,
    action      TEXT NOT NULL DEFAULT ''
);
"""

# Базовые полки: ИИ может добавлять свои, но эти есть всегда.
BASE_SHELVES: tuple[tuple[str, str, str, int], ...] = (
    ("inbox",   "Входящее",  "ещё не разобрано",            0),
    ("ideas",   "Идеи",      "то, что можно построить",     10),
    ("work",    "Дела",      "надо сделать руками",         20),
    ("life",    "Жизнь",     "быт, здоровье, люди",         30),
    ("learn",   "Узнать",    "прочитать, посмотреть, изучить", 40),
    ("money",   "Деньги",    "траты, доходы, планы",        50),
)


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = asyncio.Lock()
        self._conn.executescript(SCHEMA)
        self._seed_shelves()
        self._conn.commit()

    # ── низкий уровень ────────────────────────────────────────────────────
    def _seed_shelves(self) -> None:
        for key, title, subtitle, sort in BASE_SHELVES:
            self._conn.execute(
                "INSERT OR IGNORE INTO shelves(id, key, title, subtitle, sort, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (new_id(), key, title, subtitle, sort, now()),
            )

    def _query(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cur = self._conn.execute(sql, tuple(args))
        return [dict(row) for row in cur.fetchall()]

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> None:
        self._conn.execute(sql, tuple(args))
        self._conn.commit()

    async def query(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._query, sql, args)

    async def exec(self, sql: str, args: Iterable[Any] = ()) -> None:
        async with self._lock:
            await asyncio.to_thread(self._exec, sql, args)

    def close(self) -> None:
        self._conn.close()

    # ── мысли ─────────────────────────────────────────────────────────────
    async def add_thought(self, raw: str, source: str = "telegram") -> str:
        tid = new_id()
        ts = now()
        await self.exec(
            "INSERT INTO thoughts(id, raw, created_at, updated_at, source)"
            " VALUES(?,?,?,?,?)",
            (tid, raw.strip(), ts, ts, source),
        )
        return tid

    async def update_thought(self, thought_id: str, **fields: Any) -> None:
        allowed = {
            "title", "summary", "shelf", "kind", "importance", "status",
            "tags", "claude_prompt", "claude_worthy", "pending", "raw",
        }
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "tags" and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key} = ?")
            args.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        args.extend([now(), thought_id])
        await self.exec(f"UPDATE thoughts SET {', '.join(sets)} WHERE id = ?", args)

    async def get_thought(self, thought_id: str) -> dict[str, Any] | None:
        rows = await self.query("SELECT * FROM thoughts WHERE id = ?", (thought_id,))
        return rows[0] if rows else None

    async def list_thoughts(self, limit: int = 400) -> list[dict[str, Any]]:
        return await self.query(
            "SELECT * FROM thoughts WHERE status != 'deleted'"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    async def last_thought(self) -> dict[str, Any] | None:
        rows = await self.query(
            "SELECT * FROM thoughts WHERE status != 'deleted'"
            " ORDER BY created_at DESC LIMIT 1"
        )
        return rows[0] if rows else None

    # ── полки ─────────────────────────────────────────────────────────────
    async def list_shelves(self) -> list[dict[str, Any]]:
        return await self.query("SELECT * FROM shelves ORDER BY sort, title")

    async def ensure_shelf(self, key: str, title: str = "", subtitle: str = "") -> str:
        key = (key or "inbox").strip().lower()[:32] or "inbox"
        rows = await self.query("SELECT key FROM shelves WHERE key = ?", (key,))
        if rows:
            return key
        await self.exec(
            "INSERT INTO shelves(id, key, title, subtitle, sort, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (new_id(), key, title or key.title(), subtitle, 200, now()),
        )
        return key

    # ── напоминания ───────────────────────────────────────────────────────
    async def add_reminder(self, thought_id: str, mode: str, interval_min: int,
                           first_fire_at: float) -> str:
        rid = new_id()
        await self.exec(
            "INSERT INTO reminders(id, thought_id, mode, interval_min, next_fire_at, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (rid, thought_id, mode, max(0, interval_min), first_fire_at, now()),
        )
        return rid

    async def due_reminders(self, at: float) -> list[dict[str, Any]]:
        return await self.query(
            "SELECT r.*, t.title, t.raw, t.importance, t.shelf, t.status AS thought_status"
            " FROM reminders r JOIN thoughts t ON t.id = r.thought_id"
            " WHERE r.active = 1 AND r.next_fire_at <= ?"
            " ORDER BY r.next_fire_at",
            (at,),
        )

    async def list_reminders(self) -> list[dict[str, Any]]:
        return await self.query(
            "SELECT * FROM reminders WHERE active = 1 ORDER BY next_fire_at"
        )

    async def reminders_for(self, thought_id: str) -> list[dict[str, Any]]:
        return await self.query(
            "SELECT * FROM reminders WHERE thought_id = ? AND active = 1", (thought_id,)
        )

    async def mark_fired(self, reminder_id: str, next_fire_at: float | None,
                         escalation: int) -> None:
        if next_fire_at is None:
            await self.exec(
                "UPDATE reminders SET active = 0, last_fired_at = ?,"
                " fire_count = fire_count + 1, escalation = ? WHERE id = ?",
                (now(), escalation, reminder_id),
            )
        else:
            await self.exec(
                "UPDATE reminders SET next_fire_at = ?, last_fired_at = ?,"
                " fire_count = fire_count + 1, escalation = ? WHERE id = ?",
                (next_fire_at, now(), escalation, reminder_id),
            )

    async def ack_reminder(self, reminder_id: str) -> None:
        await self.exec(
            "UPDATE reminders SET active = 0, acked_at = ?, escalation = 0 WHERE id = ?",
            (now(), reminder_id),
        )

    async def snooze_reminder(self, reminder_id: str, minutes: int) -> None:
        await self.exec(
            "UPDATE reminders SET next_fire_at = ?, escalation = 0 WHERE id = ?",
            (now() + minutes * 60, reminder_id),
        )

    async def drop_reminders(self, thought_id: str) -> None:
        await self.exec(
            "UPDATE reminders SET active = 0 WHERE thought_id = ?", (thought_id,)
        )

    async def log_fire(self, reminder_id: str, thought_id: str, escalation: int,
                       action: str = "sent") -> None:
        await self.exec(
            "INSERT INTO reminder_log(reminder_id, thought_id, fired_at, escalation, action)"
            " VALUES(?,?,?,?,?)",
            (reminder_id, thought_id, now(), escalation, action),
        )
