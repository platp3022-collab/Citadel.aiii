#!/usr/bin/env python3
"""
Нарратив и внимание: за что вообще платят на мем-коинах.

Мем-коин — это тикер, к которому привязан объём внимания. Растёт внимание —
растёт цена; ушло внимание — падает всё, какими бы чистыми ни были метрики.
Поэтому кроме ончейна монету нужно разобрать по трём вопросам:

1. В какой мете она сидит — ИИ-агенты, политика, знаменитости, животные из
   тиктока, лончпады и чейны. Меты сменяются, и деньги перетекают между ними.
2. Она первая в своём нарративе или двадцатый клон. У подражателей потолок
   низкий: они отрабатывают быстрый скальп, а не иксы.
3. Платит ли эта мета прямо сейчас. Это бот считает по своим же закрытым
   сделкам: что приносило за последние дни, то и получает прибавку к скору.

Ничего платного и никаких ключей: слова берём из названия и тикера, деньги —
из собственной базы сделок, свежесть нарратива — из того, что бот уже видел.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("narrative")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "storage_path": "data/memebot.db",
    "seen_path": "data/narratives.json",

    "heat_days": 3,              # за какой срок считаем, что мета кормит
    "heat_min_trades": 3,        # меньше сделок — судить рано
    "heat_bonus": 8,             # максимум прибавки за горячую мету
    "heat_penalty": -7,          # максимум штрафа за мету, которая только сливала

    "first_bonus": 7,            # первопроходец нарратива
    "copycat_penalty": -8,       # подражатель: потолок низкий
    "copycat_after": 3,          # столько монет с тем же корнем = волна клонов
    "wave_hours": 48,            # за какое время считаем волну
    "forget_days": 10,           # корни старше — забываем
}

# ── меты 2026-го. Слова намеренно короткие: их ищем как подстроки в тикере
#    и названии, а не как отдельные слова — тикеры любят слипаться.
METAS: dict[str, tuple[str, ...]] = {
    "ИИ-агенты": (
        "ai", "agent", "gpt", "llm", "neural", "brain", "mind", "agi", "swarm",
        "claude", "grok", "gemini", "deepseek", "openai", "terminal", "bot",
        "model", "prompt", "token izer", "inference",
    ),
    "Политика": (
        "trump", "maga", "biden", "kamala", "potus", "election", "vote", "senate",
        "putin", "war", "peace", "tariff", "gov", "polit", "president", "usa",
    ),
    "Знаменитости": (
        "elon", "musk", "kanye", "ye", "taylor", "drake", "ronaldo", "messi",
        "vitalik", "saylor", "cz", "kardashian", "mrbeast", "rogan", "streamer",
    ),
    "Животные и тикток": (
        "dog", "doge", "shib", "inu", "cat", "kitty", "frog", "pepe", "hippo",
        "deng", "penguin", "panda", "capy", "sloth", "otter", "duck", "goose",
        "monkey", "ape", "bear", "bull", "wif", "hat", "chill", "moo",
    ),
    "Чейны и лончпады": (
        "sol", "base", "bonk", "pump", "hood", "robinhood", "sui", "hype",
        "chain", "launch", "dex", "swap", "jup", "ray",
    ),
    "Деньги и рынок": (
        "etf", "fed", "rate", "bank", "gold", "treasury", "bond", "yield",
        "usd", "dollar", "inflation", "rich", "money", "million", "billion",
    ),
}

# приписки, по которым видно подражателя: «то же самое, но второе»
COPYCAT_MARKS = (
    "2.0", "3.0", "2 0", "v2", "x2", "baby", "mini", "micro", "little", "junior",
    "jr", "son", "sonof", "reborn", "return", "returns", "classic", "og", "next",
    "new", "real", "true", "official", "cto", "reloaded", "again",
)

CEILING_FIRST = "первопроходец"
CEILING_COPY = "подражатель"
CEILING_PLAIN = "обычный"


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def words(text: str) -> list[str]:
    """Слова из тикера и названия в нижнем регистре, без мусора."""
    return [w for w in re.split(r"[^a-zа-я0-9]+", (text or "").lower()) if w]


def root_of(symbol: str, name: str) -> str:
    """Корень нарратива: по нему видно, что монета — из той же волны.

    «BABYPENGU», «PENGU 2.0» и «PENGU» дают один корень, поэтому двадцатый
    клон уже не выглядит как новый нарратив.
    """
    text = f"{symbol} {name}".lower()
    for mark in COPYCAT_MARKS:
        text = text.replace(mark, " ")
    # цифры на хвосте — та же приписка: MOODENG2 это тот же MOODENG
    parts = [w.rstrip("0123456789") for w in words(text)]
    parts = [w for w in parts if len(w) >= 3]
    if not parts:
        return (symbol or name or "").lower()[:12]
    # самое длинное слово обычно и есть суть: «pengu» из «baby pengu coin»
    return max(parts, key=len)[:16]


@dataclass
class Narrative:
    """Что бот понял про смысл монеты, а не про её цифры."""
    meta: str = ""                 # в какой мете сидит
    ceiling: str = CEILING_PLAIN   # потолок: первопроходец / подражатель
    points: float = 0.0            # прибавка к скору
    seen_before: int = 0           # сколько монет с тем же корнем уже видели
    heat: float = 0.0              # как эта мета платила в последние дни
    notes: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        return " · ".join(self.notes) if self.notes else "нарратив не распознан"

    @property
    def size_multiplier(self) -> float:
        """Сколько денег класть: клонам меньше, первым — полный размер."""
        if self.ceiling == CEILING_COPY:
            return 0.6
        if self.ceiling == CEILING_FIRST:
            return 1.0
        return 0.85

    def thesis(self, symbol: str) -> str:
        """Строка для журнала: почему вообще зашли."""
        parts = [f"${symbol}"]
        if self.meta:
            parts.append(f"мета «{self.meta}»")
        parts.append({CEILING_FIRST: "первый в нарративе",
                      CEILING_COPY: (f"клон, таких уже {self.seen_before}"
                                     if self.seen_before else "клон по названию"),
                      CEILING_PLAIN: "нарратив нейтральный"}[self.ceiling])
        if self.heat > 0:
            parts.append("мета платит прямо сейчас")
        elif self.heat < 0:
            parts.append("мета в последние дни только сливала")
        return ", ".join(parts)


class MetaHeat:
    """Какая мета кормит прямо сейчас — по собственным закрытым сделкам.

    Это и есть ротация ликвидности с точки зрения бота: не гадать, куда
    перетекли деньги, а посмотреть, на чём он сам заработал за последние дни.
    """

    def __init__(self, conf: dict[str, Any] | None = None):
        self.conf = {**DEFAULTS, **(conf or {})}
        self.cache: dict[str, float] = {}
        self.stats: dict[str, dict[str, float]] = {}
        self.updated = 0.0

    @property
    def db_path(self) -> Path:
        p = Path(self.conf.get("storage_path", "data/memebot.db"))
        return p if p.is_absolute() else ROOT / p

    def refresh(self, force: bool = False) -> None:
        """Раз в пару минут пересчитываем: чаще незачем, сделки редки."""
        if not force and time.time() - self.updated < 120:
            return
        self.updated = time.time()
        since = time.time() - num(self.conf.get("heat_days"), 3) * 86400
        rows: list[dict] = []
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
            if "meta" in cols:
                rows = [dict(r) for r in conn.execute(
                    "SELECT meta, pnl_sol, pnl_pct FROM trades"
                    " WHERE status='closed' AND exit_ts>=?", (since,))]
            conn.close()
        except Exception as e:  # noqa: BLE001
            log.debug("тепло мет не прочиталось: %s", e)
            return

        by_meta: dict[str, list[dict]] = {}
        for r in rows:
            by_meta.setdefault(str(r.get("meta") or "").strip() or "—", []).append(r)

        cache, stats = {}, {}
        need = int(num(self.conf.get("heat_min_trades"), 3))
        for meta, items in by_meta.items():
            if meta == "—":
                continue
            pnl = sum(num(r.get("pnl_sol")) for r in items)
            wins = len([r for r in items if num(r.get("pnl_sol")) > 0])
            stats[meta] = {"trades": len(items), "pnl": pnl, "wins": wins}
            if len(items) < need:
                continue          # на двух сделках выводов не делают
            # −1..+1: не «сколько заработали», а «стабильно ли платит»
            share = wins / len(items)
            cache[meta] = max(-1.0, min(1.0, (share - 0.45) * 2 + (1 if pnl > 0 else -1) * 0.3))
        self.cache, self.stats = cache, stats

    def heat(self, meta: str) -> float:
        self.refresh()
        return self.cache.get(meta, 0.0)

    def report(self) -> str:
        self.refresh(force=True)
        if not self.stats:
            return ("📊 <b>Меты</b>\n\nЗакрытых сделок с нарративом пока нет — "
                    "бот наберёт статистику сам, за несколько часов работы.")
        out = [f"📊 <b>Что платит за {num(self.conf.get('heat_days'), 3):.0f} дня</b>", ""]
        for meta, s in sorted(self.stats.items(), key=lambda kv: -kv[1]["pnl"]):
            heat = self.cache.get(meta)
            mark = "🔥" if (heat or 0) > 0.2 else "❄️" if (heat or 0) < -0.2 else "•"
            out.append(f"{mark} <b>{meta}</b> — {s['pnl']:+.3f} SOL, "
                       f"сделок {s['trades']:.0f}, в плюс {s['wins']:.0f}"
                       + ("" if heat is not None else " <i>(мало данных)</i>"))
        out.append("")
        out.append("<i>Горячая мета добавляет к скору, холодная — отнимает.</i>")
        return "\n".join(out)


class NarrativeReader:
    """Разбирает монету по смыслу: мета, первопроходец или клон, платит ли мета."""

    def __init__(self, conf: dict[str, Any] | None = None):
        self.conf = {**DEFAULTS, **(conf or {})}
        self.heat = MetaHeat(self.conf)
        self.seen: dict[str, list[float]] = {}     # корень → когда встречали
        self.done: dict[str, Narrative] = {}        # монета → уже разобранная
        self.load()

    # ---------- память о том, что уже видели ----------

    @property
    def path(self) -> Path:
        p = Path(self.conf.get("seen_path", "data/narratives.json"))
        return p if p.is_absolute() else ROOT / p

    def load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.seen = {str(k): [float(t) for t in v][-40:]
                                 for k, v in data.items() if isinstance(v, list)}
        except Exception as e:  # noqa: BLE001
            log.warning("память нарративов не прочиталась: %s", e)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.seen, ensure_ascii=False),
                                 encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.debug("память нарративов не записалась: %s", e)

    def forget_old(self) -> None:
        cutoff = time.time() - num(self.conf.get("forget_days"), 10) * 86400
        self.seen = {root: [t for t in stamps if t >= cutoff]
                     for root, stamps in self.seen.items()}
        self.seen = {k: v for k, v in self.seen.items() if v}

    def remember(self, root: str) -> int:
        """Запоминает корень и говорит, сколько таких уже было в волне."""
        window = time.time() - num(self.conf.get("wave_hours"), 48) * 3600
        stamps = self.seen.setdefault(root, [])
        before = len([t for t in stamps if t >= window])
        stamps.append(time.time())
        del stamps[:-40]
        return before

    # ---------- разбор ----------

    def meta_of(self, symbol: str, name: str) -> str:
        text = f"{symbol} {name}".lower()
        best, hits = "", 0
        for meta, keys in METAS.items():
            found = sum(1 for k in keys if k in text)
            if found > hits:
                best, hits = meta, found
        return best

    def read(self, symbol: str, name: str = "", news_hit: bool = False,
             mint: str = "") -> Narrative:
        """Главное: мета, потолок и прибавка к скору.

        Одну монету разбираем один раз: сканер видит её каждые полминуты, и
        без этого она сама себе накрутила бы «волну клонов».
        """
        n = Narrative()
        if not self.conf.get("enabled", True):
            return n
        key = mint or f"{symbol}|{name}"
        if key in self.done:
            return self.done[key]

        n.meta = self.meta_of(symbol, name)
        root = root_of(symbol, name)
        n.seen_before = self.remember(root)

        text = f"{symbol} {name}".lower()
        marked = any(m in text for m in COPYCAT_MARKS)
        wave = int(num(self.conf.get("copycat_after"), 3))

        if marked or n.seen_before >= wave:
            n.ceiling = CEILING_COPY
            n.points += num(self.conf.get("copycat_penalty"), -8)
            n.notes.append(f"клон нарратива «{root}»"
                           + (f", таких за двое суток {n.seen_before}"
                              if n.seen_before else ""))
        elif n.seen_before == 0 and (news_hit or n.meta):
            n.ceiling = CEILING_FIRST
            n.points += num(self.conf.get("first_bonus"), 7)
            n.notes.append("первый с таким нарративом")

        if n.meta:
            n.heat = self.heat.heat(n.meta)
            if n.heat > 0:
                n.points += n.heat * num(self.conf.get("heat_bonus"), 8)
                n.notes.append(f"мета «{n.meta}» платит")
            elif n.heat < 0:
                n.points += abs(n.heat) * num(self.conf.get("heat_penalty"), -7)
                n.notes.append(f"мета «{n.meta}» в минусе")
            else:
                n.notes.append(f"мета «{n.meta}»")

        if news_hit:
            n.notes.append("тикер в свежих заголовках")

        if len(self.done) > 3000:
            self.done.clear()
        self.done[key] = n
        self.forget_old()
        self.save()
        return n
