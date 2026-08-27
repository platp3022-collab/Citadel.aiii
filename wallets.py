#!/usr/bin/env python3
"""
Слежка за кошельками: что покупают трейдеры, за которыми мы следим.

Это то, чем на самом деле живут удачливые трейдеры «окопов»: не гадать по
метрикам, хорошая ли монета, а смотреть, зашли ли в неё кошельки, которые
стабильно зарабатывают. Совпали двое-трое на одной свежей монете — сигнал
совсем другого качества, чем «холдеров 80 и объём $12K».

Кошельки хранятся в data/wallets.json, добавляются командой /wallet add.
Данные берём из обычного RPC — ни платных API, ни ключей сверх SOLANA_RPC_URL.
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
log = logging.getLogger("wallets")

SOL_MINT = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "poll_seconds": 45,          # как часто обходить кошельки
    "window_minutes": 60,        # покупка «свежая», если сделана в этом окне
    "min_hits": 2,               # столько кошельков должны совпасть на монете
    "bonus_per_hit": 9,          # прибавка к скору за каждое совпадение
    "max_bonus": 22,
    "force_enter": True,         # совпало min_hits — заходим, даже если скор ниже
    "follow_within_minutes": 20, # позже — уже покупка у них на выходе, а не вход
    "min_own_score": 25,         # совсем мусорную монету не берём и за ними
    "signatures_per_wallet": 6,  # сколько последних сделок смотреть у кошелька
    "storage_path": "data/wallets.json",
}


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
class Buy:
    """Покупка токена отслеживаемым кошельком."""
    mint: str
    wallet: str
    name: str
    ts: float
    amount: float = 0.0


@dataclass
class Signal:
    """Сколько наших кошельков зашло в монету и когда."""
    hits: int = 0
    names: list[str] = field(default_factory=list)
    minutes_ago: float = 0.0

    @property
    def note(self) -> str:
        if not self.hits:
            return ""
        who = ", ".join(self.names[:3])
        return f"{self.hits} кошелька зашли ({who}), {self.minutes_ago:.0f} мин назад"


class WalletTracker:
    """Опрашивает кошельки и запоминает, что они покупали."""

    def __init__(self, session: aiohttp.ClientSession, conf: dict[str, Any] | None = None,
                 rpc_url: str = ""):
        self.conf = {**DEFAULTS, **(conf or {})}
        self.session = session
        self.rpc = rpc_url or DEFAULT_RPC
        self.wallets: dict[str, str] = {}       # адрес → имя
        self.buys: list[Buy] = []
        self.seen: set[str] = set()             # уже разобранные подписи
        self.last_poll = 0.0
        self.last_error = ""
        self._id = 0
        self.load()

    # ---------- список кошельков ----------

    @property
    def path(self) -> Path:
        p = Path(self.conf.get("storage_path", "data/wallets.json"))
        return p if p.is_absolute() else ROOT / p

    def load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.wallets = {str(k): str(v) for k, v in data.items()}
        except Exception as e:  # noqa: BLE001
            log.warning("список кошельков не прочитался: %s", e)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.wallets, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def add(self, address: str, name: str = "") -> bool:
        address = address.strip()
        if not (32 <= len(address) <= 44):
            return False
        self.wallets[address] = name.strip() or address[:4] + "…" + address[-4:]
        self.save()
        return True

    def remove(self, address: str) -> bool:
        address = address.strip()
        # разрешаем удалять и по имени — адреса наизусть никто не помнит
        if address not in self.wallets:
            for addr, name in list(self.wallets.items()):
                if name.lower() == address.lower():
                    address = addr
                    break
        if address in self.wallets:
            del self.wallets[address]
            self.save()
            return True
        return False

    # ---------- RPC ----------

    async def call(self, method: str, params: list) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        async with self.session.post(self.rpc, json=payload,
                                     timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status == 429:
                raise RuntimeError("RPC ограничил частоту запросов")
            data = await r.json(content_type=None)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"RPC {method}: {dig(data, 'error', 'message')}")
        return (data or {}).get("result")

    async def wallet_buys(self, address: str, name: str) -> list[Buy]:
        """Что этот кошелёк купил за последние сделки."""
        limit = int(num(self.conf.get("signatures_per_wallet"), 6))
        sigs = await self.call("getSignaturesForAddress", [address, {"limit": limit}])
        out: list[Buy] = []
        for item in (sigs or []):
            sig = item.get("signature")
            if not sig or sig in self.seen or item.get("err"):
                continue
            self.seen.add(sig)
            ts = num(item.get("blockTime")) or time.time()
            # старое не разбираем: интересуют только свежие покупки
            if time.time() - ts > num(self.conf.get("window_minutes"), 60) * 60:
                continue
            try:
                tx = await self.call("getTransaction", [sig, {
                    "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            except Exception as e:  # noqa: BLE001
                log.debug("транзакция %s: %s", sig[:8], e)
                continue
            out.extend(self._parse_buys(tx, address, name, ts))
        return out

    @staticmethod
    def _parse_buys(tx: Any, address: str, name: str, ts: float) -> list[Buy]:
        """Покупка — это когда у кошелька стало больше токенов, чем было."""
        meta = dig(tx, "meta", default={}) or {}
        before, after = {}, {}
        for row in (meta.get("preTokenBalances") or []):
            if row.get("owner") == address:
                before[row.get("mint")] = num(dig(row, "uiTokenAmount", "uiAmount"))
        for row in (meta.get("postTokenBalances") or []):
            if row.get("owner") == address:
                after[row.get("mint")] = num(dig(row, "uiTokenAmount", "uiAmount"))

        out = []
        for mint, amount in after.items():
            if not mint or mint == SOL_MINT or mint in STABLES:
                continue
            gained = amount - before.get(mint, 0.0)
            if gained > 0:
                out.append(Buy(mint=mint, wallet=address, name=name, ts=ts, amount=gained))
        return out

    # ---------- опрос ----------

    async def poll_once(self) -> int:
        if not self.wallets:
            return 0
        found = 0
        for address, name in list(self.wallets.items()):
            try:
                for buy in await self.wallet_buys(address, name):
                    self.buys.append(buy)
                    found += 1
                    log.info("Кошелёк %s купил %s", name, buy.mint[:8])
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:160]
                log.debug("кошелёк %s: %s", name, e)
            await asyncio.sleep(0.4)          # не долбим RPC очередью

        # чистим старое, иначе список растёт бесконечно
        cutoff = time.time() - num(self.conf.get("window_minutes"), 60) * 60 * 3
        self.buys = [b for b in self.buys if b.ts >= cutoff]
        if len(self.seen) > 4000:
            self.seen = set(list(self.seen)[-2000:])
        self.last_poll = time.time()
        return found

    async def loop(self, stop: asyncio.Event | None = None) -> None:
        interval = num(self.conf.get("poll_seconds"), 45)
        while not (stop and stop.is_set()):
            try:
                await self.poll_once()
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:160]
                log.warning("обход кошельков: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval) if stop \
                    else await asyncio.sleep(interval)
                return
            except asyncio.TimeoutError:
                pass

    # ---------- сигнал ----------

    def signal(self, mint: str) -> Signal:
        """Сколько наших кошельков зашло в эту монету за окно."""
        window = num(self.conf.get("window_minutes"), 60) * 60
        now = time.time()
        hits = {}
        for b in self.buys:
            if b.mint == mint and now - b.ts <= window:
                # один кошелёк считаем один раз, даже если докупал
                if b.wallet not in hits or b.ts < hits[b.wallet].ts:
                    hits[b.wallet] = b
        if not hits:
            return Signal()
        first = min(b.ts for b in hits.values())
        return Signal(hits=len(hits), names=[b.name for b in hits.values()],
                      minutes_ago=(now - first) / 60)

    def status_line(self) -> str:
        if not self.wallets:
            return ("Слежка за кошельками: список пуст, "
                    "бот наберёт его сам или добавь через /wallet add")
        ago = (time.time() - self.last_poll) / 60 if self.last_poll else -1
        return (f"Слежка за кошельками: {len(self.wallets)} шт., "
                f"покупок в памяти {len(self.buys)}"
                + (f", обход {ago:.0f} мин назад" if ago >= 0 else ", ещё не обходил")
                + (f", ошибка: {self.last_error}" if self.last_error else ""))

    def list_message(self) -> str:
        if not self.wallets:
            return ("👛 <b>Кошельки под слежкой</b>\n\nСписок пуст.\n\n"
                    "Добавить: <code>/wallet add АДРЕС Имя</code>\n\n"
                    "<i>Где брать адреса: топы прибыльных трейдеров на gmgn.ai, "
                    "kolscan.io, dune.com — там видно, кто стабильно в плюсе.</i>")
        out = [f"👛 <b>Кошельки под слежкой</b> ({len(self.wallets)})"]
        now = time.time()
        for addr, name in self.wallets.items():
            recent = len([b for b in self.buys if b.wallet == addr and now - b.ts < 3600])
            out.append(f"• <b>{name}</b> — покупок за час: {recent}\n"
                       f"  <code>{addr}</code>")
        out.append("\nУдалить: <code>/wallet del АДРЕС</code>")
        return "\n".join(out)
