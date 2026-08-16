#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Соц-скам-сканер: Telegram (юзер-аккаунт) + YouTube по ключевым словам →
извлечение тикеров/адресов контрактов → DexScreener + rug-pull проверки
(RugCheck/honeypot.is/GoPlus, переиспользуются из memebot.py) →
LLM-вердикт по вероятности скама → алерт в твой Telegram.

В отличие от memebot.py (который сам находит новые токены на DexScreener),
этот бот идёт от рекламы: ищет, где и как монеты пиарят в TG-каналах и на
YouTube, и проверяет, не разводка ли это (шиллинг, договорной памп,
раг-пул), сопоставляя рекламный текст с реальными ончейн-данными.

Установка:
    pip install -r requirements.txt   # добавляет telethon

Настройка (.env рядом со скриптом, см. .env.example):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, TELEGRAM_CHAT_ID  — куда слать алерты
                                                                   (тот же бот, что в memebot.py)
    TELEGRAM_API_ID, TELEGRAM_API_HASH  — с my.telegram.org, для юзер-клиента (поиск по TG)
    YOUTUBE_API_KEY                     — с console.cloud.google.com (YouTube Data API v3)
    ANTHROPIC_API_KEY                   — опционально, для LLM-вердикта

Первый запуск Telegram-клиента интерактивный: попросит номер телефона и код
подтверждения (один раз, сессия сохранится в data/socialbot_session.session).
Сделай это заранее:
    python socialbot.py --login

ВАЖНО: автоматический поиск через личный аккаунт технически нарушает разумные
лимиты Telegram и может привести к ограничениям — по возможности используй
отдельный (не основной) аккаунт для этого клиента.

Запуск:
    python socialbot.py            # боевой режим, бесконечный цикл
    python socialbot.py --once     # один проход и выход
    python socialbot.py --dry      # без отправки в Telegram, всё в консоль

Отказ от ответственности: инструмент фильтрации информации, не финансовый совет.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ImportError:
    sys.exit("Нужен aiohttp:  pip install aiohttp")

try:
    from telethon import TelegramClient
    from telethon.tl.functions.messages import SearchGlobalRequest
    from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty
except ImportError:
    TelegramClient = None  # соц-поиск по TG будет выключен

import memebot as core  # переиспользуем DexScreener, risk_check, Telegram, esc/fmt_* и т.д.

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("socialbot")

# ════════════════════════════════════════════════════════════════════════════
#  КОНФИГ
# ════════════════════════════════════════════════════════════════════════════

CONFIG: dict[str, Any] = {
    "scan": {
        "interval_seconds": 300,
        "lookback_hours": 6,          # окно поиска свежих упоминаний
        "chains": ["solana", "base", "bsc", "ethereum"],
        "min_liquidity_usd": 3000,    # низкий порог — цель найти скам, а не только «хорошие» монеты
    },
    "telegram_search": {
        "enabled": True,
        "results_per_keyword": 40,
    },
    "youtube_search": {
        "enabled": True,
        "results_per_keyword": 15,
    },
    "keywords": [
        # EN — типичные фразы скам-шиллинга
        "100x gem", "next 100x", "low cap gem", "presale live", "stealth launch",
        "fair launch", "guaranteed profit", "insider call", "private signal group",
        "ape now", "limited slots", "dev doxxed", "next pump", "gem alert",
        # RU — то же самое по-русски
        "стопроцентная прибыль", "гарантированный памп", "инсайд по монете",
        "закрытая группа сигналов", "успей купить пока не поздно", "иксы гарантированы",
        "заходи в монету сейчас", "разгон монеты",
    ],
    "alerts": {
        "cooldown_minutes": 360,      # не повторять алерт по той же монете чаще
        "max_per_scan": 8,
    },
    "risk": {"enabled": True},
    "llm": {"enabled": True, "model": "claude-sonnet-5"},
    "storage": {"path": "data/socialbot.db"},
}


def cfg(path: str, default: Any = None) -> Any:
    node: Any = CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# ════════════════════════════════════════════════════════════════════════════
#  ИЗВЛЕЧЕНИЕ ТИКЕРОВ/АДРЕСОВ ИЗ ТЕКСТА
# ════════════════════════════════════════════════════════════════════════════

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$([A-Za-z][A-Za-z0-9]{1,9})\b")
EVM_ADDR_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# классические маркеры скам-рекламы прямо в тексте объявления
SCAM_PHRASES = {
    "en": [
        "guaranteed profit", "guaranteed x", "no risk", "risk free", "send eth get",
        "send sol get", "double your", "dm me for", "dm for private", "limited slots",
        "limited spots", "insider info", "insider call", "buy before announcement",
        "presale ending soon", "act fast", "won't last long", "private signal",
    ],
    "ru": [
        "гарантированная прибыль", "без риска", "отправь и получи", "удвой свои",
        "пиши в личку", "закрытая группа", "ограниченное количество мест",
        "успей купить", "только сегодня", "инсайдерская информация",
        "недорого купи сейчас", "точно взлетит",
    ],
}


@dataclass
class Candidate:
    kind: str          # "address" | "ticker"
    value: str
    chain_hint: str = ""


def extract_candidates(text: str) -> list[Candidate]:
    if not text:
        return []
    out: list[Candidate] = []
    for m in EVM_ADDR_RE.findall(text):
        out.append(Candidate("address", m.lower(), "evm"))
    for m in SOL_ADDR_RE.findall(text):
        # base58 без 0/O/I/l — уже гарантировано regex-ом; длина 32-44 типична для Solana mint
        out.append(Candidate("address", m, "solana"))
    for m in CASHTAG_RE.findall(text):
        sym = m.upper()
        if sym in core.STOP_TICKERS or len(sym) < 2:
            continue
        out.append(Candidate("ticker", sym))
    # убрать дубли, сохранив порядок
    seen, uniq = set(), []
    for c in out:
        key = (c.kind, c.value)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def scam_text_flags(text: str) -> list[str]:
    low = (text or "").lower()
    flags = []
    for lang, phrases in SCAM_PHRASES.items():
        for p in phrases:
            if p in low:
                flags.append(f"фраза-маркер: «{p}»")
    return flags


# ════════════════════════════════════════════════════════════════════════════
#  ИСТОЧНИКИ
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Mention:
    platform: str       # "telegram" | "youtube"
    source_name: str
    source_link: str
    text: str
    ts: float


class TelegramSource:
    """Поиск свежих сообщений по ключевым словам через юзер-клиент Telethon.

    Официальный Bot API не даёт читать чужие каналы — нужен полноценный
    аккаунт. Использует глобальный поиск Telegram (то же, чем пользуется
    встроенный поиск в приложении).
    """

    def __init__(self):
        api_id = int(os.environ.get("TELEGRAM_API_ID", "0") or 0)
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        self.enabled = bool(TelegramClient and api_id and api_hash
                            and cfg("telegram_search.enabled", True))
        self.client: "TelegramClient | None" = None
        if self.enabled:
            session_path = ROOT / "data"
            session_path.mkdir(parents=True, exist_ok=True)
            self.client = TelegramClient(str(session_path / "socialbot_session"),
                                         api_id, api_hash)
        elif TelegramClient is None:
            log.warning("telethon не установлен — поиск по Telegram выключен")
        elif not (api_id and os.environ.get("TELEGRAM_API_HASH")):
            log.warning("TELEGRAM_API_ID/TELEGRAM_API_HASH не заданы — поиск по Telegram выключен")

    async def start(self) -> None:
        if not self.enabled:
            return
        await self.client.start()
        if not await self.client.is_user_authorized():
            log.error("Telegram-клиент не авторизован. Запусти: python socialbot.py --login")
            self.enabled = False

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def search(self, keyword: str, lookback_hours: float) -> list[Mention]:
        if not self.enabled:
            return []
        cutoff = time.time() - lookback_hours * 3600
        limit = int(cfg("telegram_search.results_per_keyword", 40))
        out: list[Mention] = []
        try:
            result = await self.client(SearchGlobalRequest(
                q=keyword,
                filter=InputMessagesFilterEmpty(),
                min_date=None,
                max_date=None,
                offset_rate=0,
                offset_peer=InputPeerEmpty(),
                offset_id=0,
                limit=limit,
            ))
        except Exception as e:  # noqa: BLE001
            log.warning("TG поиск '%s': %s", keyword, e)
            return []

        chats = {c.id: c for c in getattr(result, "chats", [])}
        for msg in getattr(result, "messages", []):
            text = getattr(msg, "message", "") or ""
            if not text:
                continue
            ts = msg.date.timestamp() if getattr(msg, "date", None) else time.time()
            if ts < cutoff:
                continue
            peer = getattr(msg, "peer_id", None)
            chat_id = getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None)
            chat = chats.get(chat_id)
            username = getattr(chat, "username", None) if chat else None
            name = (getattr(chat, "title", None) or username or "Telegram") if chat else "Telegram"
            link = f"https://t.me/{username}/{msg.id}" if username else ""
            out.append(Mention("telegram", str(name), link, text, ts))
        return out


class YouTubeSource:
    """Поиск свежих видео по ключевым словам через YouTube Data API v3."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.api_key = os.environ.get("YOUTUBE_API_KEY", "")
        self.enabled = bool(self.api_key) and cfg("youtube_search.enabled", True)
        if not self.api_key:
            log.warning("YOUTUBE_API_KEY не задан — поиск по YouTube выключен")

    async def search(self, keyword: str, lookback_hours: float) -> list[Mention]:
        if not self.enabled:
            return []
        published_after = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "part": "snippet", "q": keyword, "type": "video", "order": "date",
            "maxResults": int(cfg("youtube_search.results_per_keyword", 15)),
            "publishedAfter": published_after, "key": self.api_key,
        }
        try:
            async with self.session.get("https://www.googleapis.com/youtube/v3/search",
                                        params=params,
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    log.warning("YouTube API %s: %s", r.status, (await r.text())[:200])
                    return []
                data = await r.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            log.warning("YouTube поиск '%s': %s", keyword, e)
            return []

        out: list[Mention] = []
        for item in data.get("items") or []:
            vid = core.dig(item, "id", "videoId")
            sn = item.get("snippet") or {}
            title, desc = sn.get("title", ""), sn.get("description", "")
            channel = sn.get("channelTitle", "YouTube")
            ts = time.time()
            try:
                ts = datetime.strptime(sn.get("publishedAt", ""),
                                       "%Y-%m-%dT%H:%M:%SZ").replace(
                                           tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
            link = f"https://youtu.be/{vid}" if vid else ""
            out.append(Mention("youtube", channel, link, f"{title}\n{desc}", ts))
        return out


# ════════════════════════════════════════════════════════════════════════════
#  СОПОСТАВЛЕНИЕ С DEXSCREENER
# ════════════════════════════════════════════════════════════════════════════

async def resolve_candidate(dex: core.DexScreener, c: Candidate,
                            chains: set[str]) -> dict | None:
    pairs: list[dict] = []
    if c.kind == "address":
        pairs = await dex.pairs_for_tokens([c.value])
    else:
        pairs = await dex.search(c.value)
        pairs = [p for p in pairs
                if str(core.dig(p, "baseToken", "symbol", default="")).upper() == c.value]

    pairs = [p for p in pairs if str(p.get("chainId", "")).lower() in chains]
    if not pairs:
        return None
    return max(pairs, key=lambda p: core.num(core.dig(p, "liquidity", "usd")))


# ════════════════════════════════════════════════════════════════════════════
#  LLM-ВЕРДИКТ ПО СКАМ-ВЕРОЯТНОСТИ
# ════════════════════════════════════════════════════════════════════════════

LLM_SYSTEM = (
    "Ты аналитик по мошенничеству с крипто-токенами. Тебе дают ончейн-метрики монеты "
    "и то, как её рекламируют в Telegram/YouTube. Оцени вероятность скама/раг-пула/"
    "договорного пампа с последующим сливом на ранних покупателей. "
    "Отвечай ТОЛЬКО JSON без markdown, строго по схеме: "
    '{"scam_probability": 0-10, "verdict": "одно-два предложения по-русски", '
    '"reasons": ["до трёх коротких пунктов"]}. '
    "Будь скептичен: агрессивный шиллинг с обещаниями гарантированной прибыли — "
    "почти всегда красный флаг."
)


async def llm_scam_analyze(session: aiohttp.ClientSession, api_key: str, model: str,
                           ticker: str, pair: dict | None, mentions: list[Mention],
                           text_flags: list[str], risk_flags: list[str]) -> dict | None:
    if not api_key:
        return None
    ctx = [f"Тикер: ${ticker}"]
    if pair:
        liq = core.num(core.dig(pair, "liquidity", "usd"))
        fdv = core.num(pair.get("fdv")) or core.num(pair.get("marketCap"))
        pc = pair.get("priceChange") or {}
        ctx += [
            f"Сеть: {pair.get('chainId')} / {pair.get('dexId')}",
            f"Ликвидность: {core.fmt_usd(liq)} | FDV: {core.fmt_usd(fdv)}",
            f"Изменение цены: 1ч {core.num(pc.get('h1')):+.1f}%, 24ч {core.num(pc.get('h24')):+.1f}%",
            f"Возраст пула: {core.fmt_age(core.age_minutes(pair))}",
        ]
    else:
        ctx.append("На DexScreener пара не найдена (возможно, ещё не листингован или это фейк).")

    ctx.append(f"Рекламных сообщений найдено: {len(mentions)}")
    for m in mentions[:5]:
        ctx.append(f"- [{m.platform}/{m.source_name}] {m.text[:200]!r}")
    if text_flags:
        ctx.append("Маркеры скам-риторики в тексте: " + "; ".join(text_flags[:6]))
    if risk_flags:
        ctx.append("Флаги проверки контракта: " + "; ".join(risk_flags[:6]))

    payload = {"model": model, "max_tokens": 350, "system": LLM_SYSTEM,
               "messages": [{"role": "user", "content": "\n".join(ctx)}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        async with session.post("https://api.anthropic.com/v1/messages", json=payload,
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=45)) as r:
            if r.status != 200:
                log.warning("LLM %s: %s", r.status, (await r.text())[:200])
                return None
            data = await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM: %s", e)
        return None

    text = "".join(blk.get("text", "") for blk in data.get("content", [])
                   if isinstance(blk, dict) and blk.get("type") == "text").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        import json
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                import json
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                return None
    return None


# ════════════════════════════════════════════════════════════════════════════
#  ХРАНИЛИЩЕ (дедуп/кулдаун)
# ════════════════════════════════════════════════════════════════════════════

class Storage:
    def __init__(self, path: str | Path):
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen (candidate_key TEXT PRIMARY KEY, ts REAL)")
        self.conn.commit()

    def should_alert(self, key: str, cooldown_minutes: float) -> bool:
        row = self.conn.execute(
            "SELECT ts FROM seen WHERE candidate_key=?", (key,)).fetchone()
        if not row:
            return True
        return time.time() - row[0] >= cooldown_minutes * 60

    def record(self, key: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO seen VALUES (?,?)", (key, time.time()))
        self.conn.commit()


# ════════════════════════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ АЛЕРТА
# ════════════════════════════════════════════════════════════════════════════

def finding_message(ticker: str, pair: dict | None, mentions: list[Mention],
                    text_flags: list[str], risk: "core.RiskReport | None",
                    llm: dict | None) -> str:
    esc = core.esc
    scam_p = core.num((llm or {}).get("scam_probability"), -1)
    head = "🔴" if scam_p >= 7 or (risk and risk.danger) else "🟡" if scam_p >= 4 or text_flags else "👀"

    lines = [f"{head} <b>${esc(ticker)}</b> — реклама в соцсетях, {len(mentions)} источник(ов)"]

    if pair:
        liq = core.num(core.dig(pair, "liquidity", "usd"))
        fdv = core.num(pair.get("fdv")) or core.num(pair.get("marketCap"))
        pc = pair.get("priceChange") or {}
        chain = str(pair.get("chainId", ""))
        lines += [
            f"<i>{esc(core.dig(pair,'baseToken','name',default=''))}</i> · {chain}/{esc(pair.get('dexId'))}",
            f"💵 {core.fmt_price(core.num(pair.get('priceUsd')))} · "
            f"📈 {core.num(pc.get('h1')):+.1f}% 1ч, {core.num(pc.get('h24')):+.1f}% 24ч",
            f"💧 {core.fmt_usd(liq)}" + (f" · FDV {core.fmt_usd(fdv)}" if fdv else ""),
            f"⏱ Возраст пула: {core.fmt_age(core.age_minutes(pair))}",
        ]
    else:
        lines.append("⚠️ На DexScreener не найден — рекламируют то, чего не видно в рынке.")

    lines.append("")
    lines.append("<b>Источники:</b>")
    for m in mentions[:5]:
        snippet = esc(m.text.strip().replace("\n", " ")[:140])
        tag = {"telegram": "TG", "youtube": "YT"}.get(m.platform, m.platform)
        if m.source_link:
            lines.append(f"  • [{tag}] <a href=\"{esc(m.source_link)}\">{esc(m.source_name)}</a>: {snippet}")
        else:
            lines.append(f"  • [{tag}] {esc(m.source_name)}: {snippet}")

    if text_flags:
        lines += ["", "<b>⚠️ Маркеры скам-рекламы в тексте:</b>"]
        lines += [f"  • {esc(f)}" for f in text_flags[:6]]

    if risk and risk.ok and risk.flags:
        lines += ["", "<b>🛡 Проверка контракта:</b>"]
        lines += [f"  • {esc(f)}" for f in risk.flags[:8]]

    if llm:
        lines += ["", f"🧠 LLM: вероятность скама {scam_p:.0f}/10 — "
                      f"{esc(str(llm.get('verdict',''))[:220])}"]
        for reason in (llm.get("reasons") or [])[:3]:
            lines.append(f"  • {esc(str(reason)[:150])}")

    if pair:
        addr = esc(core.dig(pair, "baseToken", "address", default=""))
        lines += ["", "🔗 " + core._links(pair), f"<code>{addr}</code>"]

    lines += ["", "<i>Не финансовый совет. Признаки скама не гарантия, отсутствие — тоже.</i>"]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
#  БОТ
# ════════════════════════════════════════════════════════════════════════════

class Bot:
    def __init__(self, session: aiohttp.ClientSession, dry: bool = False):
        self.session = session
        self.dex = core.DexScreener(session)
        self.tg_source = TelegramSource()
        self.yt_source = YouTubeSource(session)
        self.store = Storage(cfg("storage.path", "data/socialbot.db"))
        self.tg = core.Telegram(session, os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                                os.environ.get("TELEGRAM_CHANNEL_ID", ""),
                                os.environ.get("TELEGRAM_CHAT_ID", ""), dry=dry)
        self.chains = {c.lower() for c in (cfg("scan.chains") or [])}
        self.stop_event = asyncio.Event()

    async def gather_mentions(self) -> list[Mention]:
        lookback = core.num(cfg("scan.lookback_hours"), 6)
        keywords = cfg("keywords") or []
        tasks = []
        for kw in keywords:
            tasks.append(self.tg_source.search(kw, lookback))
            tasks.append(self.yt_source.search(kw, lookback))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        mentions: list[Mention] = []
        for res in results:
            if isinstance(res, list):
                mentions.extend(res)
        return mentions

    async def scan_once(self, notify_empty: bool = False) -> int:
        mentions = await self.gather_mentions()
        log.info("Собрано упоминаний: %d", len(mentions))

        by_candidate: dict[tuple[str, str], list[Mention]] = {}
        for m in mentions:
            for c in extract_candidates(m.text):
                by_candidate.setdefault((c.kind, c.value), []).append(m)

        cooldown = core.num(cfg("alerts.cooldown_minutes"), 360)
        max_per_scan = int(core.num(cfg("alerts.max_per_scan"), 8))
        sent = 0

        for (kind, value), cand_mentions in by_candidate.items():
            if sent >= max_per_scan:
                break
            candidate = Candidate(kind, value)
            try:
                pair = await resolve_candidate(self.dex, candidate, self.chains)
            except Exception as e:  # noqa: BLE001
                log.warning("Резолв %s %s: %s", kind, value, e)
                pair = None

            if pair and core.num(core.dig(pair, "liquidity", "usd")) < core.num(
                    cfg("scan.min_liquidity_usd"), 3000):
                continue

            ticker = (str(core.dig(pair, "baseToken", "symbol", default=value))
                     if pair else value)
            dedup_key = f"{kind}:{value}"
            if not self.store.should_alert(dedup_key, cooldown):
                continue

            all_flags: list[str] = []
            for m in cand_mentions:
                all_flags += scam_text_flags(m.text)
            text_flags = list(dict.fromkeys(all_flags))

            risk = None
            if cfg("risk.enabled", True) and pair:
                chain = str(pair.get("chainId", ""))
                token = str(core.dig(pair, "baseToken", "address", default=""))
                try:
                    risk = await core.risk_check(self.session, chain, token)
                except Exception as e:  # noqa: BLE001
                    log.warning("risk_check %s: %s", ticker, e)

            llm_res = None
            if cfg("llm.enabled", True) and os.environ.get("ANTHROPIC_API_KEY"):
                llm_res = await llm_scam_analyze(
                    self.session, os.environ["ANTHROPIC_API_KEY"],
                    str(cfg("llm.model", "claude-sonnet-5")), ticker, pair,
                    cand_mentions, text_flags,
                    risk.flags if risk and risk.ok else [])

            # без реального сигнала на DexScreener и без явных красных флагов — не спамим
            if not pair and not text_flags and not llm_res:
                continue

            msg = finding_message(ticker, pair, cand_mentions, text_flags, risk, llm_res)
            ok = await self.tg.broadcast(msg)
            if ok:
                self.store.record(dedup_key)
                sent += 1
                await asyncio.sleep(1.0)

        if notify_empty and sent == 0:
            await self.tg.send(
                f"Скан завершён: {len(mentions)} упоминаний, {len(by_candidate)} кандидатов, "
                f"алертов — 0.", chat_id=self.tg.admin_chat_id or None)
        return sent

    async def run(self) -> None:
        await self.tg_source.start()
        interval = core.num(cfg("scan.interval_seconds"), 300)
        await self.tg.send(
            "🤖 Соц-скам-сканер запущен.\n"
            f"TG-поиск: {'вкл' if self.tg_source.enabled else 'выкл'} · "
            f"YouTube-поиск: {'вкл' if self.yt_source.enabled else 'выкл'}\n"
            f"Ключевых слов: {len(cfg('keywords') or [])}",
            chat_id=self.tg.admin_chat_id or None)
        try:
            while not self.stop_event.is_set():
                try:
                    await self.scan_once()
                except Exception as e:  # noqa: BLE001
                    log.exception("Сбой скана: %s", e)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.tg_source.stop()


# ════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════════════════════════════════════

async def do_login() -> None:
    """Разовая интерактивная авторизация Telegram-клиента (запросит номер и код)."""
    load = core.load_env
    load()
    src = TelegramSource()
    if not src.enabled or src.client is None:
        sys.exit("TELEGRAM_API_ID/TELEGRAM_API_HASH не заданы в .env или telethon не установлен.")
    await src.client.start()
    me = await src.client.get_me()
    print(f"Авторизован как: {getattr(me, 'first_name', '')} (@{getattr(me, 'username', '')})")
    await src.client.disconnect()


async def amain(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    core.load_env()

    if not args.dry and not os.environ.get("TELEGRAM_BOT_TOKEN"):
        log.warning("TELEGRAM_BOT_TOKEN не задан — работаю в режиме вывода в консоль")
        args.dry = True

    connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=core.UA, connector=connector) as session:
        bot = Bot(session, dry=args.dry)
        if args.once:
            await bot.tg_source.start()
            found = await bot.scan_once(notify_empty=True)
            await bot.tg_source.stop()
            log.info("Готово. Алертов: %d", found)
            return
        await bot.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="Соц-скам-сканер TG/YouTube → Telegram")
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    ap.add_argument("--dry", action="store_true", help="не слать в Telegram, печатать в консоль")
    ap.add_argument("--login", action="store_true", help="разовая авторизация Telegram-клиента")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    try:
        if args.login:
            asyncio.run(do_login())
        else:
            asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
