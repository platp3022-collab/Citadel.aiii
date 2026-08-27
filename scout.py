#!/usr/bin/env python3
"""
Поиск умных кошельков без ручного списка.

Логика простая и проверяемая: бот запоминает цену каждой монеты, которую увидел.
Через час смотрит, какие из них выстрелили. По каждой выстрелившей достаёт
крупнейших держателей — среди них те, кто зашёл рано и до сих пор держит.
Кошелёк, попавшийся в нескольких выстреливших монетах подряд, — это уже не
совпадение, и он сам добавляется в слежку.

Всё считается по обычному RPC: ни платных API, ни чужих списков «топ трейдеров»,
которым нельзя проверить.

Данные: data/wallet_scores.json — кто сколько раз попадался в выигравших.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("scout")

JUP_PRICE = "https://lite-api.jup.ag/price/v3"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sweep_minutes": 12,          # как часто подводить итоги по увиденным монетам
    "judge_after_minutes": 60,    # через сколько судить монету: выстрелила или нет
    "winner_multiple": 2.0,       # рост в N раз — считаем выстрелом
    "holders_per_coin": 20,       # сколько крупнейших держателей смотреть
    "min_wins": 3,                # столько попаданий — кошелёк идёт в слежку
    "max_tracked": 40,            # сколько кошельков держать в слежке
    "forget_days": 14,            # старые заслуги забываем
    "storage_path": "data/wallet_scores.json",
    "seen_path": "data/seen_coins.json",
}

# Служебные адреса, которые всегда в топе держателей и кошельками не являются
SYSTEM_OWNERS = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",   # Raydium authority
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",   # Pump.fun fee
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
}


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 попадание, 2 попадания, 5 попаданий."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur if cur is not None else default


@dataclass
class Seen:
    """Монета, которую бот видел, и цена на тот момент."""
    mint: str
    symbol: str
    ts: float
    price: float
    judged: bool = False


@dataclass
class WalletScore:
    """Заслуги кошелька: в скольких выстреливших монетах он оказался."""
    wins: int = 0
    coins: list[str] = field(default_factory=list)
    last_win: float = 0.0
    promoted: bool = False


class WalletScout:
    """Находит кошельки сам: по тем, кто оказался в выстреливших монетах."""

    def __init__(self, session: aiohttp.ClientSession, tracker: Any,
                 conf: dict[str, Any] | None = None):
        self.conf = {**DEFAULTS, **(conf or {})}
        self.session = session
        self.tracker = tracker              # WalletTracker: даёт RPC и список слежки
        self.seen: dict[str, Seen] = {}
        self.scores: dict[str, WalletScore] = {}
        self.last_sweep = 0.0
        self.last_error = ""
        self.winners = 0
        self.load()

    # ---------- хранение ----------

    def _path(self, key: str) -> Path:
        p = Path(self.conf.get(key))
        return p if p.is_absolute() else ROOT / p

    def load(self) -> None:
        try:
            p = self._path("storage_path")
            if p.exists():
                for addr, row in json.loads(p.read_text(encoding="utf-8")).items():
                    self.scores[addr] = WalletScore(
                        wins=int(num(row.get("wins"))), coins=row.get("coins") or [],
                        last_win=num(row.get("last_win")),
                        promoted=bool(row.get("promoted")))
            p = self._path("seen_path")
            if p.exists():
                for mint, row in json.loads(p.read_text(encoding="utf-8")).items():
                    self.seen[mint] = Seen(mint=mint, symbol=row.get("symbol") or "",
                                           ts=num(row.get("ts")), price=num(row.get("price")),
                                           judged=bool(row.get("judged")))
        except Exception as e:  # noqa: BLE001
            log.warning("память разведчика не прочиталась: %s", e)

    def save(self) -> None:
        try:
            p = self._path("storage_path")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(
                {a: {"wins": s.wins, "coins": s.coins[-20:], "last_win": s.last_win,
                     "promoted": s.promoted} for a, s in self.scores.items()},
                ensure_ascii=False, indent=1), encoding="utf-8")
            # держим только то, что ещё может пригодиться
            fresh = {m: s for m, s in self.seen.items()
                     if time.time() - s.ts < 6 * 3600}
            self._path("seen_path").write_text(json.dumps(
                {m: {"symbol": s.symbol, "ts": s.ts, "price": s.price, "judged": s.judged}
                 for m, s in fresh.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("память разведчика не записалась: %s", e)

    # ---------- наблюдение ----------

    def observe(self, mint: str, symbol: str, price: float) -> None:
        """Запоминаем монету и её цену — потом проверим, что с ней стало."""
        if not mint or price <= 0 or mint in self.seen:
            return
        self.seen[mint] = Seen(mint=mint, symbol=symbol, ts=time.time(), price=price)

    async def prices(self, mints: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in range(0, len(mints), 50):
            batch = mints[i:i + 50]
            try:
                async with self.session.get(JUP_PRICE, params={"ids": ",".join(batch)},
                                            timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json(content_type=None)
            except Exception as e:  # noqa: BLE001
                log.debug("цены: %s", e)
                continue
            if isinstance(data, dict):
                for mint, item in data.items():
                    price = num(dig(item, "usdPrice")) or num(dig(item, "price"))
                    if price:
                        out[mint] = price
        return out

    # ---------- кто держит выстрелившую монету ----------

    async def top_holders(self, mint: str) -> list[str]:
        """Крупнейшие держатели монеты — среди них те, кто зашёл рано."""
        res = await self.tracker.call("getTokenLargestAccounts", [mint])
        accounts = [row.get("address") for row in (dig(res, "value", default=[]) or [])
                    if row.get("address")][:int(num(self.conf.get("holders_per_coin"), 20))]
        if not accounts:
            return []
        # владельцы токен-аккаунтов — одним запросом, а не двадцатью
        info = await self.tracker.call("getMultipleAccounts",
                                       [accounts, {"encoding": "jsonParsed"}])
        owners = []
        for acc in (dig(info, "value", default=[]) or []):
            owner = dig(acc, "data", "parsed", "info", "owner")
            if owner and owner not in SYSTEM_OWNERS:
                owners.append(owner)
        return owners

    # ---------- подведение итогов ----------

    async def sweep(self) -> int:
        """Судим увиденные монеты и начисляем заслуги кошелькам."""
        wait = num(self.conf.get("judge_after_minutes"), 60) * 60
        due = [s for s in self.seen.values()
               if not s.judged and time.time() - s.ts >= wait]
        if not due:
            self.last_sweep = time.time()
            return 0

        now_prices = await self.prices([s.mint for s in due])
        need = num(self.conf.get("winner_multiple"), 2.0)
        found = 0

        for s in due:
            s.judged = True
            price_now = now_prices.get(s.mint, 0.0)
            if price_now <= 0 or s.price <= 0:
                continue
            multiple = price_now / s.price
            if multiple < need:
                continue

            # монета выстрелила — смотрим, кто в ней сидит
            self.winners += 1
            found += 1
            log.info("Выстрелила $%s: %.1fx — смотрю держателей", s.symbol, multiple)
            try:
                owners = await self.top_holders(s.mint)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:160]
                log.debug("держатели %s: %s", s.mint[:8], e)
                continue

            for owner in owners:
                score = self.scores.setdefault(owner, WalletScore())
                if s.mint in score.coins:
                    continue
                score.wins += 1
                score.coins.append(s.mint)
                score.last_win = time.time()
            await asyncio.sleep(0.3)

        self.forget_old()
        self.promote()
        self.save()
        self.last_sweep = time.time()
        return found

    def forget_old(self) -> None:
        """Заслуги двухнедельной давности ничего не говорят о сегодняшнем дне."""
        cutoff = time.time() - num(self.conf.get("forget_days"), 14) * 86400
        for addr in [a for a, s in self.scores.items()
                     if s.last_win and s.last_win < cutoff and not s.promoted]:
            del self.scores[addr]

    def promote(self) -> list[str]:
        """Кошельки с несколькими попаданиями сами уходят в слежку."""
        need = int(num(self.conf.get("min_wins"), 3))
        limit = int(num(self.conf.get("max_tracked"), 40))
        promoted = []
        ranked = sorted(self.scores.items(), key=lambda kv: -kv[1].wins)
        for addr, score in ranked:
            if score.promoted or score.wins < need:
                continue
            if len(self.tracker.wallets) >= limit:
                break
            if self.tracker.add(addr, f"авто · {score.wins} "
                                   f"{plural(score.wins, 'попадание', 'попадания', 'попаданий')}"):
                score.promoted = True
                promoted.append(addr)
                log.info("Кошелёк сам попал в слежку: %s (%d выстреливших монет)",
                         addr[:8], score.wins)
        return promoted

    async def loop(self, stop: asyncio.Event | None = None) -> None:
        interval = num(self.conf.get("sweep_minutes"), 12) * 60
        while not (stop and stop.is_set()):
            try:
                await self.sweep()
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:160]
                log.warning("разведка кошельков: %s", e)
            try:
                if stop:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                await asyncio.sleep(interval)
            except asyncio.TimeoutError:
                pass

    # ---------- отчёт ----------

    def status_line(self) -> str:
        promoted = len([s for s in self.scores.values() if s.promoted])
        waiting = len([s for s in self.seen.values() if not s.judged])
        ago = (time.time() - self.last_sweep) / 60 if self.last_sweep else -1
        return (f"Разведка кошельков: выстреливших монет {self.winners}, "
                f"кандидатов {len(self.scores)}, взято в слежку {promoted}, "
                f"монет под наблюдением {waiting}"
                + (f", проверка {ago:.0f} мин назад" if ago >= 0 else ""))

    def report(self) -> str:
        need = int(num(self.conf.get("min_wins"), 3))
        out = ["🕵️ <b>Разведка кошельков</b>",
               f"Монет под наблюдением: <b>{len([s for s in self.seen.values() if not s.judged])}</b>",
               f"Выстрелило за всё время: <b>{self.winners}</b>",
               f"Кошелёк идёт в слежку после <b>{need}</b> попаданий"]

        ranked = sorted(self.scores.items(), key=lambda kv: -kv[1].wins)[:10]
        if not ranked:
            out += ["", "Пока никого: нужно, чтобы монеты успели выстрелить.",
                    "Обычно первые кандидаты появляются за несколько часов."]
        else:
            out += ["", "<b>Кто чаще всех оказывался в выигравших:</b>"]
            for addr, s in ranked:
                mark = "✅ в слежке" if s.promoted else f"{need - s.wins} до слежки"
                out.append(f"  • <code>{addr[:6]}…{addr[-4:]}</code> — "
                           f"{s.wins} {plural(s.wins, 'попадание', 'попадания', 'попаданий')} · {mark}")
        return "\n".join(out)
