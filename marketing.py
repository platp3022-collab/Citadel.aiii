#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web3 Marketing Engine — генератор контент-паков для мем-коина.

Модуль делает три вещи:
  1) собирает system prompt из «паспорта проекта» (marketing.json);
  2) гоняет инфоповод через Anthropic API и разбирает ответ на 6 блоков;
  3) отдаёт админ-панель в Telegram: превью + inline-кнопки (публикация в канал,
     публикация в X, тред в X, генерация картинки, перегенерация).

Самостоятельный запуск (без Telegram, всё в консоль):
    python marketing.py "Мы сожгли 3% сапплая"

Ключи (переменные окружения или .env рядом со скриптом):
    ANTHROPIC_API_KEY   — обязателен, генерация контента
    OPENAI_API_KEY      — опционально: картинки (gpt-image-1) и расшифровка голосовых
    X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET — автопостинг в X

Отказ от ответственности: контент маркетинговый, не финансовый совет.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("memebot.marketing")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_IMAGE_URL = "https://api.openai.com/v1/images/generations"
OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"
X_TWEETS_URL = "https://api.twitter.com/2/tweets"

TWEET_LIMIT = 280
TG_LIMIT = 3800          # с запасом до телеграмных 4096


# ════════════════════════════════════════════════════════════════════════════
#  ПАСПОРТ ПРОЕКТА
# ════════════════════════════════════════════════════════════════════════════

PASSPORT_FIELDS: list[tuple[str, str]] = [
    ("name", "Название монеты"),
    ("ticker", "Тикер"),
    ("chain", "Блокчейн"),
    ("contract", "Адрес контракта (CA)"),
    ("buy_link", "Ссылка на покупку"),
    ("chart_link", "График (DexScreener/Dextools)"),
    ("market_cap", "Текущий Market Cap"),
    ("target_market_cap", "Целевой Market Cap"),
    ("mascot", "Концепция/Маскот"),
    ("tg_channel", "TG Channel"),
    ("tg_chat", "TG Chat"),
    ("x_url", "X (Twitter)"),
]

# «[Вставьте CA]», «[Например: $DKING]», пустая строка — всё это незаполненное поле
_PLACEHOLDER = re.compile(r"^\s*$|^\[.*\]$|например|вставьте|заполни|your[_ ]|xxx+", re.I)

CHAIN_TAGS = {
    "solana": "#Solana", "ton": "#TON", "ethereum": "#Ethereum", "eth": "#Ethereum",
    "bnb": "#BNB", "bsc": "#BNB", "base": "#Base", "arbitrum": "#Arbitrum",
    "polygon": "#Polygon", "avalanche": "#Avalanche", "sui": "#SUI", "tron": "#TRON",
}
CHAIN_DEX = {
    "solana": "Raydium / Jupiter", "ton": "Ston.fi / DeDust", "ethereum": "Uniswap",
    "bnb": "PancakeSwap", "bsc": "PancakeSwap", "base": "Uniswap / Aerodrome",
}


@dataclass
class Passport:
    name: str = ""
    ticker: str = ""
    chain: str = ""
    contract: str = ""
    buy_link: str = ""
    chart_link: str = ""
    market_cap: str = ""
    target_market_cap: str = ""
    mascot: str = ""
    tg_channel: str = ""
    tg_chat: str = ""
    x_url: str = ""

    # ---------- загрузка ----------

    @classmethod
    def load(cls, path: str | Path) -> "Passport":
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            fallback = ROOT / "marketing.example.json"
            if fallback.exists():
                p = fallback
            else:
                return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Паспорт %s не прочитан: %s", p, e)
            return cls()
        known = {f for f, _ in PASSPORT_FIELDS}
        data = {k: str(v or "").strip() for k, v in raw.items() if k in known}
        return cls(**data)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        p.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")

    def as_dict(self) -> dict[str, str]:
        return {f: getattr(self, f) for f, _ in PASSPORT_FIELDS}

    # ---------- производные ----------

    @property
    def ticker_tag(self) -> str:
        t = (self.ticker or "").strip().lstrip("$").upper()
        return f"${t}" if t else "$TICKER"

    @property
    def chain_tag(self) -> str:
        return CHAIN_TAGS.get((self.chain or "").strip().lower(), f"#{(self.chain or 'Crypto').strip()}")

    @property
    def dex_hint(self) -> str:
        return CHAIN_DEX.get((self.chain or "").strip().lower(), "DEX проекта")

    @property
    def hashtags(self) -> str:
        return f"#Crypto #Memecoin {self.chain_tag} #100xGem #Altcoins"

    def missing(self) -> list[str]:
        """Поля, которые всё ещё держат плейсхолдер — публиковать с ними нельзя."""
        out = []
        for f, label in PASSPORT_FIELDS:
            if _PLACEHOLDER.match(str(getattr(self, f) or "").strip()):
                out.append(label)
        return out

    @property
    def ready(self) -> bool:
        return not self.missing()

    def as_prompt_block(self) -> str:
        lines = []
        for f, label in PASSPORT_FIELDS:
            val = str(getattr(self, f) or "").strip() or "— не задано, не выдумывай —"
            lines.append(f"* {label}: {val}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """\
# 1. ИДЕНТИЧНОСТЬ И РОЛЬ
Ты — Head of Marketing & Web3 Growth Lead мем-коина. Твой опыт: проведение TGE,
органический рост комьюнити, вирусный шиллинг, управление многотысячными чатами и
создание FOMO-эффекта уровня $PEPE, $BONK, $WIF и $DOGE.
Задача: превращать любой инфоповод (даже из 2–3 слов) в готовый к публикации контент-пак.

# 2. ПАСПОРТ ПРОЕКТА
{passport}

Служебное: тикер для постов — {ticker}; хэштеги — {hashtags}; основной DEX — {dex}.

# 3. TONE OF VOICE
* Bullish energy: никакого корпоративного языка, пиши так, будто от поста зависит памп.
* Degen-сленг: To the Moon, HODL, Diamond Hands, Paper Hands, WAGMI, LFG, Ape In, Pump,
  Gem, Bullish, Alpha, Whales, Burn, Listing, DexScreener, Raydium, Ston.fi.
* FOMO: держать монету — быть частью движения, выйти сейчас — пропустить самое интересное.
* Структура: эмодзи (🚀 💎 🔥 🌕 📈 🐸 🐕), короткие абзацы, жирный шрифт, списки.
* Русский язык в блоках 1 и 5; английский — в блоках 2, 3, 4 и 6.

# 4. ЧТО ГЕНЕРИРУЕШЬ
Всегда все шесть блоков, даже если вводные — два слова:
1) Telegram-пост: кликбейтный заголовок, суть (3–5 предложений), «почему это bullish»
   для холдеров, CTA и набор ссылок (Купить / График / Чат) из паспорта.
2) Вирусный твит: до 280 символов, {ticker} в первых двух строках, хэштеги в конце.
3) Тред из 4 твитов: крючок → детали → масштаб → призыв со ссылкой. Каждый твит ≤ 280
   символов, нумерация 1/4 … 4/4.
4) Raid-скрипты: три коротких (1–2 строки) реплики от лица живого участника комьюнити —
   в тред инфлюенсера, в шиллинг-чат Telegram, в комментарии DexScreener/Dextools.
5) Вовлечение в чат: провокация, опрос или челлендж, на который хочется ответить.
6) Промты для Midjourney/DALL·E: мем со маскотом и графический баннер, оба на английском.

# 5. ГРАНИЦЫ (соблюдай молча, не упоминай их в текстах постов)
* NFA: не обещай гарантированную прибыль и не давай прямых финансовых советов. Не рисуй
  «цена будет X» как обещание — цели по капитализации подаются как амбиция комьюнити.
* Только правда: не выдумывай листинги на биржах, партнёрства, аудиты, фонды, инвесторов
  и цифры, которых нет во вводных или паспорте. Мало фактов — качай эмоцию и комьюнити.
* Raid-скрипты пишутся для живых людей от первого лица: никаких заготовок «от имени
  фейковых аккаунтов», никаких обещаний доходности и вводящих в заблуждение утверждений.
* CA и ссылки переноси символ в символ. Если поле паспорта не задано — не подставляй
  выдуманное значение, оставь строку с пометкой «уточнить».

# 6. ФОРМАТ ОТВЕТА
Отвечай СТРОГО по шаблону ниже, без вступлений, без комментариев после, без ```-обёрток.

### 📢 1. TELEGRAM CHANNEL POST
[Заголовок]
[Текст поста]
[Bullish-фактор]
[CTA + Ссылки]

---

### 🐦 2. X (TWITTER) VIRAL TWEET
[Текст твита] + [Хэштеги]

---

### 🧵 3. X (TWITTER) THREAD
1/4 [Текст 1]
2/4 [Текст 2]
3/4 [Текст 3]
4/4 [Текст 4 + Ссылки]

---

### ⚔️ 4. RAID & SHILLING SCRIPTS
* **For Influencers Replying (X):** [Текст]
* **For Telegram Shill Groups:** [Текст]
* **For DexScreener Comments:** [Текст]

---

### 💬 5. COMMUNITY ENGAGEMENT (TG CHAT)
[Интерактивный вопрос/опрос/призыв для чата]

---

### 🎨 6. AI IMAGE PROMPTS (MIDJOURNEY / DALL-E)
* **Meme Prompt:** `/imagine prompt: [англоязычный промт] --ar 16:9`
* **Banner Prompt:** `/imagine prompt: [англоязычный промт] --ar 16:9`
"""

REGEN_HINT = (
    "\n\nЭто перегенерация: предыдущий вариант админа не устроил. Дай принципиально другой "
    "заход — другой заголовок, другой угол подачи, другие метафоры и другой визуал."
)


def build_system_prompt(p: Passport) -> str:
    return SYSTEM_PROMPT.format(passport=p.as_prompt_block(), ticker=p.ticker_tag,
                                hashtags=p.hashtags, dex=p.dex_hint)


# ════════════════════════════════════════════════════════════════════════════
#  КОНТЕНТ-ПАК И ЕГО РАЗБОР
# ════════════════════════════════════════════════════════════════════════════

BLOCK_TITLES = {
    "telegram": "📢 TELEGRAM CHANNEL POST",
    "tweet": "🐦 X (TWITTER) VIRAL TWEET",
    "thread": "🧵 X (TWITTER) THREAD",
    "raid": "⚔️ RAID & SHILLING SCRIPTS",
    "community": "💬 COMMUNITY ENGAGEMENT (TG CHAT)",
    "prompts": "🎨 AI IMAGE PROMPTS",
}
_BLOCK_NUM = re.compile(r"^#{1,4}\s*.*?([1-6])\s*[.)]\s*(.+)$")
_NUM_TO_KEY = {"1": "telegram", "2": "tweet", "3": "thread", "4": "raid",
               "5": "community", "6": "prompts"}
_THREAD_LINE = re.compile(r"^\s*(\d)\s*/\s*\d\s*[.)–—:]?\s*(.*)$")
_IMAGINE = re.compile(r"/imagine\s+prompt:\s*(.+?)\s*$", re.I | re.M)
_BULLET = re.compile(r"^\s*[*\-•]\s*\*{0,2}(.+?)\*{0,2}\s*:\s*(.+)$")


@dataclass
class ContentPack:
    brief: str
    raw: str
    blocks: dict[str, str] = field(default_factory=dict)
    draft_id: int = 0

    # ---------- разбор ----------

    @classmethod
    def parse(cls, brief: str, raw: str) -> "ContentPack":
        raw = re.sub(r"^```(?:markdown|md)?\s*$|^```\s*$", "", raw.strip(), flags=re.M).strip()
        blocks: dict[str, str] = {}
        current: str | None = None
        buf: list[str] = []
        for line in raw.splitlines():
            m = _BLOCK_NUM.match(line.strip()) if line.lstrip().startswith("#") else None
            if m:
                if current:
                    blocks[current] = "\n".join(buf).strip()
                current = _NUM_TO_KEY.get(m.group(1))
                buf = []
                continue
            if line.strip() in ("---", "***", "___"):
                continue
            if current:
                buf.append(line)
        if current:
            blocks[current] = "\n".join(buf).strip()
        return cls(brief=brief, raw=raw, blocks={k: v for k, v in blocks.items() if v})

    # ---------- готовые куски ----------

    @property
    def telegram_post(self) -> str:
        return self.blocks.get("telegram", "").strip()

    @property
    def tweet(self) -> str:
        text = self.blocks.get("tweet", "").strip()
        # модель иногда оставляет служебное «Tweet:» или обрамляет кавычками
        text = re.sub(r"^(tweet|твит)\s*:\s*", "", text, flags=re.I).strip()
        if len(text) > 1 and text[0] == text[-1] == '"':
            text = text[1:-1].strip()
        return text

    @property
    def thread(self) -> list[str]:
        out: list[str] = []
        cur: list[str] = []
        for line in self.blocks.get("thread", "").splitlines():
            m = _THREAD_LINE.match(line)
            if m:
                if cur:
                    out.append("\n".join(cur).strip())
                cur = [f"{m.group(1)}/4 {m.group(2)}".strip()]
            elif cur:
                cur.append(line)
        if cur:
            out.append("\n".join(cur).strip())
        return [t for t in out if t]

    @property
    def raid(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self.blocks.get("raid", "").splitlines():
            m = _BULLET.match(line)
            if not m:
                continue
            label, text = m.group(1).lower(), m.group(2).strip().strip("`")
            key = ("x" if "influenc" in label else
                   "telegram" if "telegram" in label else
                   "dex" if "dex" in label else "")
            if key:
                out[key] = text
        return out

    @property
    def image_prompts(self) -> dict[str, str]:
        found = _IMAGINE.findall(self.blocks.get("prompts", ""))
        found = [f.strip().strip("`") for f in found]
        out: dict[str, str] = {}
        if found:
            out["meme"] = found[0]
        if len(found) > 1:
            out["banner"] = found[1]
        return out

    def problems(self) -> list[str]:
        """Что не так с паком — показываем админу до публикации."""
        issues: list[str] = []
        for key in ("telegram", "tweet", "thread", "raid", "community", "prompts"):
            if key not in self.blocks:
                issues.append(f"блок «{BLOCK_TITLES[key]}» не распознан")
        if self.tweet and len(self.tweet) > TWEET_LIMIT:
            issues.append(f"твит длиннее лимита: {len(self.tweet)}/{TWEET_LIMIT}")
        long_parts = [t.split()[0] for t in self.thread if len(t) > TWEET_LIMIT]
        if long_parts:
            issues.append("твиты треда длиннее лимита: " + ", ".join(long_parts))
        return issues


# ════════════════════════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ (ANTHROPIC)
# ════════════════════════════════════════════════════════════════════════════

async def generate_pack(session: aiohttp.ClientSession, api_key: str, model: str,
                        passport: Passport, brief: str, *, regenerate: bool = False,
                        timeout: float = 120) -> ContentPack:
    """Инфоповод → готовый контент-пак. Кидает RuntimeError с человеческой причиной."""
    if not api_key:
        raise RuntimeError("Не задан ANTHROPIC_API_KEY — генерировать нечем.")
    brief = brief.strip()
    if not brief:
        raise RuntimeError("Пустой инфоповод.")

    system = build_system_prompt(passport) + (REGEN_HINT if regenerate else "")
    user = f"ИНФОПОВОД:\n{brief}"
    if regenerate:
        user += f"\n\n(вариант #{secrets.randbelow(9000) + 1000})"
    payload = {"model": model, "max_tokens": 3000, "temperature": 1.0,
               "system": system, "messages": [{"role": "user", "content": user}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        async with session.post(ANTHROPIC_URL, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            body = await r.text()
            if r.status != 200:
                log.warning("Anthropic %s: %s", r.status, body[:300])
                raise RuntimeError(f"Anthropic API вернул {r.status}: {body[:180]}")
            data = json.loads(body)
    except aiohttp.ClientError as e:
        raise RuntimeError(f"Сеть до Anthropic: {e}") from e
    except asyncio.TimeoutError as e:
        raise RuntimeError("Anthropic не ответил за отведённое время.") from e

    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text").strip()
    if not text:
        raise RuntimeError("Пустой ответ модели.")
    return ContentPack.parse(brief, text)


# ════════════════════════════════════════════════════════════════════════════
#  КАРТИНКИ И РАСШИФРОВКА ГОЛОСОВЫХ (OPENAI, ОПЦИОНАЛЬНО)
# ════════════════════════════════════════════════════════════════════════════

def clean_image_prompt(prompt: str) -> str:
    """Убираем мидджорни-флаги — DALL·E/gpt-image их не понимает."""
    return re.sub(r"\s--\w+(?:\s+[\w:.]+)?", "", prompt).strip()


async def generate_image(session: aiohttp.ClientSession, api_key: str, prompt: str,
                         model: str = "gpt-image-1") -> bytes | None:
    if not api_key:
        return None
    payload = {"model": model, "prompt": clean_image_prompt(prompt)[:3800],
               "n": 1, "size": "1536x1024"}
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    try:
        async with session.post(OPENAI_IMAGE_URL, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=180)) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                log.warning("Картинка %s: %s", r.status, str(data)[:250])
                return None
    except Exception as e:  # noqa: BLE001
        log.warning("Картинка: %s", e)
        return None

    item = (data.get("data") or [{}])[0]
    if item.get("b64_json"):
        try:
            return base64.b64decode(item["b64_json"])
        except (ValueError, TypeError):
            return None
    if item.get("url"):
        try:
            async with session.get(item["url"],
                                   timeout=aiohttp.ClientTimeout(total=60)) as r:
                return await r.read() if r.status == 200 else None
        except Exception as e:  # noqa: BLE001
            log.warning("Скачивание картинки: %s", e)
    return None


async def transcribe(session: aiohttp.ClientSession, api_key: str, audio: bytes,
                     filename: str = "voice.ogg") -> str:
    """Голосовое → текст. Пусто, если ключа нет или сервис не ответил."""
    if not api_key or not audio:
        return ""
    form = aiohttp.FormData()
    form.add_field("model", "whisper-1")
    form.add_field("file", audio, filename=filename, content_type="audio/ogg")
    try:
        async with session.post(OPENAI_STT_URL, data=form,
                                headers={"Authorization": f"Bearer {api_key}"},
                                timeout=aiohttp.ClientTimeout(total=120)) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                log.warning("Расшифровка %s: %s", r.status, str(data)[:250])
                return ""
            return str(data.get("text") or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("Расшифровка: %s", e)
        return ""


# ════════════════════════════════════════════════════════════════════════════
#  X (TWITTER) API v2 — OAuth 1.0a user context, только stdlib
# ════════════════════════════════════════════════════════════════════════════

def _q(value: str) -> str:
    return urllib.parse.quote(str(value), safe="~-._")


class TwitterClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str = "", api_secret: str = "",
                 token: str = "", token_secret: str = "", dry: bool = False):
        self.session = session
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.token = token.strip()
        self.token_secret = token_secret.strip()
        self.dry = dry

    @classmethod
    def from_env(cls, session: aiohttp.ClientSession, dry: bool = False) -> "TwitterClient":
        return cls(session,
                   os.environ.get("X_API_KEY", ""), os.environ.get("X_API_SECRET", ""),
                   os.environ.get("X_ACCESS_TOKEN", ""),
                   os.environ.get("X_ACCESS_TOKEN_SECRET", ""), dry=dry)

    @property
    def configured(self) -> bool:
        return all((self.api_key, self.api_secret, self.token, self.token_secret))

    def _auth_header(self, method: str, url: str) -> str:
        params = {
            "oauth_consumer_key": self.api_key,
            "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self.token,
            "oauth_version": "1.0",
        }
        joined = "&".join(f"{_q(k)}={_q(params[k])}" for k in sorted(params))
        base = f"{method.upper()}&{_q(url)}&{_q(joined)}"
        key = f"{_q(self.api_secret)}&{_q(self.token_secret)}".encode()
        sig = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
        params["oauth_signature"] = sig
        return "OAuth " + ", ".join(f'{_q(k)}="{_q(params[k])}"' for k in sorted(params))

    async def tweet(self, text: str, reply_to: str | None = None) -> str:
        """Возвращает id твита. RuntimeError с причиной, если не вышло."""
        text = text.strip()
        if not text:
            raise RuntimeError("Пустой твит.")
        if len(text) > TWEET_LIMIT:
            raise RuntimeError(f"Твит длиннее {TWEET_LIMIT} символов ({len(text)}).")
        if self.dry or not self.configured:
            print(f"\n--- [X {'(reply → ' + reply_to + ')' if reply_to else ''}] ---\n{text}\n")
            return f"dry-{secrets.token_hex(4)}"

        payload: dict[str, Any] = {"text": text}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}
        headers = {"Authorization": self._auth_header("POST", X_TWEETS_URL),
                   "content-type": "application/json"}
        try:
            async with self.session.post(X_TWEETS_URL, json=payload, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=45)) as r:
                body = await r.text()
                if r.status not in (200, 201):
                    log.warning("X %s: %s", r.status, body[:300])
                    raise RuntimeError(f"X API вернул {r.status}: {body[:180]}")
                data = json.loads(body)
        except aiohttp.ClientError as e:
            raise RuntimeError(f"Сеть до X: {e}") from e
        tweet_id = str(((data.get("data") or {}).get("id") or "")).strip()
        if not tweet_id:
            raise RuntimeError("X не вернул id твита.")
        return tweet_id

    async def thread(self, texts: list[str]) -> list[str]:
        ids: list[str] = []
        parent: str | None = None
        for i, text in enumerate(texts):
            if i:
                await asyncio.sleep(2)     # не долбим рейт-лимит
            parent = await self.tweet(text, reply_to=parent)
            ids.append(parent)
        return ids


# ════════════════════════════════════════════════════════════════════════════
#  ХРАНИЛИЩЕ ЧЕРНОВИКОВ
# ════════════════════════════════════════════════════════════════════════════

DRAFT_SCHEMA = """
CREATE TABLE IF NOT EXISTS mkt_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    chat_id TEXT,
    brief TEXT,
    raw TEXT,
    published_tg REAL,
    published_x TEXT);
CREATE INDEX IF NOT EXISTS idx_mkt_ts ON mkt_drafts(ts);
"""


class DraftStore:
    def __init__(self, path: str | Path):
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(DRAFT_SCHEMA)
            self.conn.commit()

    def add(self, chat_id: str, pack: ContentPack) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO mkt_drafts (ts, chat_id, brief, raw) VALUES (?,?,?,?)",
                (time.time(), str(chat_id), pack.brief, pack.raw))
            self.conn.commit()
            return int(cur.lastrowid)

    def get(self, draft_id: int) -> ContentPack | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM mkt_drafts WHERE id=?",
                                    (draft_id,)).fetchone()
        if not row:
            return None
        pack = ContentPack.parse(row["brief"] or "", row["raw"] or "")
        pack.draft_id = int(row["id"])
        return pack

    def mark_tg(self, draft_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE mkt_drafts SET published_tg=? WHERE id=?",
                              (time.time(), draft_id))
            self.conn.commit()

    def mark_x(self, draft_id: int, tweet_ids: list[str]) -> None:
        with self.lock:
            self.conn.execute("UPDATE mkt_drafts SET published_x=? WHERE id=?",
                              (",".join(tweet_ids), draft_id))
            self.conn.commit()

    def status(self, draft_id: int) -> dict:
        with self.lock:
            row = self.conn.execute(
                "SELECT published_tg, published_x FROM mkt_drafts WHERE id=?",
                (draft_id,)).fetchone()
        return dict(row) if row else {}

    def recent(self, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, brief, published_tg, published_x FROM mkt_drafts"
                " ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ ДЛЯ TELEGRAM
# ════════════════════════════════════════════════════════════════════════════

def md_to_html(text: str) -> str:
    """Markdown из ответа модели → HTML, который понимает Telegram."""
    out = html.escape(text, quote=False)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', out)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", out)
    out = re.sub(r"(?<![_\w])__([^_\n]+)__(?!\w)", r"<b>\1</b>", out)
    out = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", out, flags=re.M)
    return out


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режем длинный текст по абзацам, чтобы влезал в одно сообщение."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            parts.append(buf)
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            parts.append(para[:cut])
            para = para[cut:].lstrip()
        buf = para
    if buf:
        parts.append(buf)
    return parts


def preview_text(pack: ContentPack, passport: Passport) -> str:
    """Человекочитаемое превью пака для админа."""
    lines = [f"🎯 <b>Контент-пак {passport.ticker_tag}</b>",
             f"<i>Инфоповод:</i> {html.escape(pack.brief[:300])}", ""]
    for key in ("telegram", "tweet", "thread", "raid", "community", "prompts"):
        body = pack.blocks.get(key)
        if not body:
            continue
        lines.append(f"━━━━━━━━━━━━━━━\n<b>{BLOCK_TITLES[key]}</b>\n")
        lines.append(md_to_html(body))
        if key == "tweet" and pack.tweet:
            lines.append(f"<i>({len(pack.tweet)}/{TWEET_LIMIT} символов)</i>")
        lines.append("")
    return "\n".join(lines).strip()


def keyboard(draft_id: int, *, has_x: bool, has_images: bool) -> dict:
    rows: list[list[dict]] = [[
        {"text": "🚀 Опубликовать в TG", "callback_data": f"mkt:tg:{draft_id}"},
    ]]
    if has_x:
        rows[0].append({"text": "🐦 Твит в X", "callback_data": f"mkt:x:{draft_id}"})
        rows.append([{"text": "🧵 Тред в X", "callback_data": f"mkt:thread:{draft_id}"}])
    if has_images:
        rows.append([{"text": "🎨 Мем", "callback_data": f"mkt:img:{draft_id}:meme"},
                     {"text": "🖼 Баннер", "callback_data": f"mkt:img:{draft_id}:banner"}])
    rows.append([{"text": "🔄 Перегенерировать", "callback_data": f"mkt:regen:{draft_id}"}])
    return {"inline_keyboard": rows}


# ════════════════════════════════════════════════════════════════════════════
#  АДМИН-ПАНЕЛЬ
# ════════════════════════════════════════════════════════════════════════════

MKT_HELP = (
    "🎯 <b>Web3 Marketing Engine</b>\n\n"
    "Пришли инфоповод текстом, голосовым или файлом — соберу контент-пак "
    "(TG-пост, твит, тред, raid-скрипты, вовлечение, промты для картинок) "
    "и покажу превью с кнопками публикации.\n\n"
    "/mkt [текст] — собрать пак по инфоповоду\n"
    "/mkt_on · /mkt_off — ловить любой текст в этом чате как инфоповод\n"
    "/passport — паспорт проекта и чего в нём не хватает\n"
    "/passport set поле значение — заполнить поле\n"
    "/drafts — последние черновики\n\n"
    "Публикация идёт только по нажатию кнопки и только в ресурсы проекта."
)


class MarketingPanel:
    """Склейка генерации, черновиков и кнопок. Telegram-клиент передаётся снаружи."""

    def __init__(self, session: aiohttp.ClientSession, tg: Any, *,
                 db_path: str | Path = "data/memebot.db",
                 passport_path: str | Path = "marketing.json",
                 model: str = "claude-opus-5",
                 auto_brief: bool = True, dry: bool = False):
        self.session = session
        self.tg = tg
        # MARKETING_PASSPORT удобен в докере: положив паспорт в data/, его не теряешь
        # при пересборке образа (этот каталог и так примонтирован томом).
        self.passport_path = os.environ.get("MARKETING_PASSPORT", "").strip() or passport_path
        self.passport = Passport.load(self.passport_path)
        self.store = DraftStore(db_path)
        self.model = os.environ.get("MARKETING_MODEL", "").strip() or model
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.x = TwitterClient.from_env(session, dry=dry)
        self.auto_brief = auto_brief
        self.dry = dry
        self.busy = asyncio.Lock()

    # ---------- служебное ----------

    @property
    def enabled(self) -> bool:
        return bool(self.anthropic_key)

    def _passport_warning(self) -> str:
        missing = self.passport.missing()
        if not missing:
            return ""
        return ("⚠️ <b>Паспорт проекта не заполнен</b>: " + ", ".join(missing) +
                ".\nПубликация заблокирована — в постах остались бы плейсхолдеры вместо "
                "CA и ссылок. Заполни <code>marketing.json</code> или командой "
                "<code>/passport set ticker $DKING</code>.")

    async def _say(self, chat_id: str, text: str, markup: dict | None = None) -> None:
        parts = split_message(text)
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            await self.tg.send(part, chat_id=chat_id,
                               reply_markup=markup if last else None)
            if not last:
                await asyncio.sleep(0.3)

    # ---------- генерация и превью ----------

    async def make_pack(self, chat_id: str, brief: str, *, regenerate: bool = False) -> None:
        if self.busy.locked():
            await self.tg.send("⏳ Уже собираю предыдущий пак, подожди пару секунд.",
                               chat_id=chat_id)
            return
        async with self.busy:
            await self.tg.send("⚙️ Собираю контент-пак…", chat_id=chat_id)
            try:
                pack = await generate_pack(self.session, self.anthropic_key, self.model,
                                           self.passport, brief, regenerate=regenerate)
            except RuntimeError as e:
                await self.tg.send(f"❌ Не сгенерировал: {html.escape(str(e))}",
                                   chat_id=chat_id)
                return
            pack.draft_id = self.store.add(chat_id, pack)
            await self._send_preview(chat_id, pack)

    async def _send_preview(self, chat_id: str, pack: ContentPack) -> None:
        header = []
        warning = self._passport_warning()
        if warning:
            header.append(warning)
        problems = pack.problems()
        if problems:
            header.append("⚠️ Проверь: " + "; ".join(problems) + ".")
        if header:
            await self.tg.send("\n\n".join(header), chat_id=chat_id)

        markup = keyboard(pack.draft_id,
                          has_x=self.x.configured or self.dry,
                          has_images=bool(self.openai_key and pack.image_prompts))
        await self._say(chat_id, preview_text(pack, self.passport), markup)

    # ---------- кнопки ----------

    async def handle_callback(self, cb: dict) -> bool:
        """True — колбэк наш и обработан."""
        data = str(cb.get("data") or "")
        if not data.startswith("mkt:"):
            return False
        cb_id = str(cb.get("id") or "")
        msg = cb.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        draft_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        arg = parts[3] if len(parts) > 3 else ""

        pack = self.store.get(draft_id)
        if not pack:
            await self.tg.answer_callback(cb_id, "Черновик не найден.", alert=True)
            return True

        if action in ("tg", "x", "thread") and not self.passport.ready:
            await self.tg.answer_callback(
                cb_id, "Сначала заполни паспорт проекта (/passport) — "
                       "иначе уйдут плейсхолдеры вместо CA и ссылок.", alert=True)
            return True

        try:
            if action == "tg":
                await self._publish_telegram(cb_id, chat_id, pack)
            elif action == "x":
                await self._publish_tweet(cb_id, chat_id, pack)
            elif action == "thread":
                await self._publish_thread(cb_id, chat_id, pack)
            elif action == "img":
                await self._publish_image(cb_id, chat_id, pack, arg or "meme")
            elif action == "regen":
                await self.tg.answer_callback(cb_id, "Перегенерирую…")
                await self.make_pack(chat_id, pack.brief, regenerate=True)
            else:
                await self.tg.answer_callback(cb_id, "Неизвестная кнопка.")
        except Exception as e:  # noqa: BLE001
            log.exception("Кнопка %s: %s", data, e)
            await self.tg.answer_callback(cb_id, "Ошибка, смотри чат.", alert=True)
            await self.tg.send(f"❌ {html.escape(str(e)[:400])}", chat_id=chat_id)
        return True

    async def _publish_telegram(self, cb_id: str, chat_id: str, pack: ContentPack) -> None:
        target = self.passport.tg_channel or getattr(self.tg, "channel_id", "")
        if not target:
            await self.tg.answer_callback(cb_id, "Не задан канал проекта.", alert=True)
            return
        if self.store.status(pack.draft_id).get("published_tg"):
            await self.tg.answer_callback(cb_id, "Этот пак уже уходил в канал.", alert=True)
            return
        if not pack.telegram_post:
            await self.tg.answer_callback(cb_id, "Блок TG-поста пустой.", alert=True)
            return
        await self.tg.answer_callback(cb_id, "Публикую…")
        ok = True
        for part in split_message(md_to_html(pack.telegram_post)):
            ok = await self.tg.send(part, chat_id=target, preview=True) and ok
            await asyncio.sleep(0.3)
        if ok:
            self.store.mark_tg(pack.draft_id)
            await self.tg.send(f"✅ Пост #{pack.draft_id} ушёл в {html.escape(str(target))}.",
                               chat_id=chat_id)
        else:
            await self.tg.send("❌ Телеграм не принял пост — проверь, что бот админ канала.",
                               chat_id=chat_id)

    async def _publish_tweet(self, cb_id: str, chat_id: str, pack: ContentPack) -> None:
        if not pack.tweet:
            await self.tg.answer_callback(cb_id, "Блок твита пустой.", alert=True)
            return
        if not self.x.configured and not self.dry:
            await self.tg.answer_callback(cb_id, "Не заданы ключи X API.", alert=True)
            return
        await self.tg.answer_callback(cb_id, "Публикую в X…")
        tweet_id = await self.x.tweet(pack.tweet)
        self.store.mark_x(pack.draft_id, [tweet_id])
        await self.tg.send(f"✅ Твит опубликован: <code>{html.escape(tweet_id)}</code>",
                           chat_id=chat_id)

    async def _publish_thread(self, cb_id: str, chat_id: str, pack: ContentPack) -> None:
        tweets = pack.thread
        if not tweets:
            await self.tg.answer_callback(cb_id, "Тред не распознан.", alert=True)
            return
        if not self.x.configured and not self.dry:
            await self.tg.answer_callback(cb_id, "Не заданы ключи X API.", alert=True)
            return
        await self.tg.answer_callback(cb_id, f"Публикую тред из {len(tweets)} твитов…")
        ids = await self.x.thread(tweets)
        self.store.mark_x(pack.draft_id, ids)
        await self.tg.send(f"✅ Тред опубликован, {len(ids)} твитов. "
                           f"Первый: <code>{html.escape(ids[0])}</code>", chat_id=chat_id)

    async def _publish_image(self, cb_id: str, chat_id: str, pack: ContentPack,
                             kind: str) -> None:
        prompt = pack.image_prompts.get(kind)
        if not prompt:
            await self.tg.answer_callback(cb_id, "Такого промта нет в паке.", alert=True)
            return
        if not self.openai_key:
            await self.tg.answer_callback(cb_id, "Не задан OPENAI_API_KEY.", alert=True)
            return
        await self.tg.answer_callback(cb_id, "Рисую, это до минуты…")
        image = await generate_image(self.session, self.openai_key, prompt)
        if not image:
            await self.tg.send("❌ Картинка не сгенерировалась. Промт для ручного "
                               f"прогона в Midjourney:\n<code>{html.escape(prompt)}</code>",
                               chat_id=chat_id)
            return
        caption = f"🎨 {kind} · черновик #{pack.draft_id}"
        if not await self.tg.send_photo(image, caption=caption, chat_id=chat_id):
            await self.tg.send("❌ Не отправил картинку в чат.", chat_id=chat_id)

    # ---------- команды и входящие сообщения ----------

    async def handle_command(self, cmd: str, arg: str, chat_id: str) -> bool:
        """True — команда наша."""
        if cmd == "/mkt":
            if not arg:
                await self.tg.send(MKT_HELP, chat_id=chat_id)
            else:
                await self.make_pack(chat_id, arg)
        elif cmd in ("/mkt_on", "/mkt_off"):
            self.auto_brief = cmd == "/mkt_on"
            await self.tg.send(
                "✅ Ловлю любой текст в этом чате как инфоповод." if self.auto_brief
                else "✅ Реагирую только на /mkt.", chat_id=chat_id)
        elif cmd == "/passport":
            await self._passport_command(arg, chat_id)
        elif cmd == "/drafts":
            await self._drafts_command(chat_id)
        else:
            return False
        return True

    async def _passport_command(self, arg: str, chat_id: str) -> None:
        parts = arg.split(maxsplit=2) if arg else []
        if parts and parts[0].lower() == "set":
            if len(parts) < 3:
                await self.tg.send("Формат: <code>/passport set ticker $DKING</code>",
                                   chat_id=chat_id)
                return
            field_name, value = parts[1].strip().lower(), parts[2].strip()
            if field_name not in {f for f, _ in PASSPORT_FIELDS}:
                await self.tg.send("Неизвестное поле. Доступны: " +
                                   ", ".join(f"<code>{f}</code>" for f, _ in PASSPORT_FIELDS),
                                   chat_id=chat_id)
                return
            setattr(self.passport, field_name, value)
            try:
                self.passport.save(self.passport_path)
            except OSError as e:
                await self.tg.send(f"⚠️ В памяти обновил, на диск не записал: {e}",
                                   chat_id=chat_id)
            else:
                await self.tg.send(f"✅ <code>{field_name}</code> = "
                                   f"{html.escape(value)}", chat_id=chat_id)
            return
        if parts and parts[0].lower() == "reload":
            self.passport = Passport.load(self.passport_path)
            await self.tg.send("♻️ Паспорт перечитан с диска.", chat_id=chat_id)
            return

        lines = ["📋 <b>Паспорт проекта</b>", ""]
        for f, label in PASSPORT_FIELDS:
            val = str(getattr(self.passport, f) or "")
            mark = "❌" if _PLACEHOLDER.match(val.strip()) else "✅"
            shown = html.escape(val) if val else "<i>пусто</i>"
            lines.append(f"{mark} <b>{label}</b> (<code>{f}</code>): {shown}")
        lines.append("")
        lines.append("Изменить: <code>/passport set contract EQC...</code> · "
                     "перечитать файл: <code>/passport reload</code>")
        warning = self._passport_warning()
        if warning:
            lines += ["", warning]
        await self._say(chat_id, "\n".join(lines))

    async def _drafts_command(self, chat_id: str) -> None:
        rows = self.store.recent()
        if not rows:
            await self.tg.send("Черновиков пока нет.", chat_id=chat_id)
            return
        lines = ["🗂 <b>Последние черновики</b>", ""]
        for r in rows:
            marks = ("📢" if r.get("published_tg") else "") + ("🐦" if r.get("published_x") else "")
            when = time.strftime("%d.%m %H:%M", time.localtime(r["ts"]))
            lines.append(f"#{r['id']} · {when} {marks}\n<i>{html.escape((r['brief'] or '')[:110])}</i>")
        await self._say(chat_id, "\n".join(lines))

    async def handle_message(self, msg: dict, chat_id: str) -> bool:
        """Свободный текст / голосовое / файл из админ-чата → инфоповод."""
        if not self.auto_brief:
            return False
        text = (msg.get("text") or msg.get("caption") or "").strip()

        voice = msg.get("voice") or msg.get("audio")
        if voice and not text:
            if not self.openai_key:
                await self.tg.send("🎤 Голосовое пришло, но расшифровывать нечем — "
                                   "задай OPENAI_API_KEY или пришли текстом.",
                                   chat_id=chat_id)
                return True
            await self.tg.send("🎤 Расшифровываю…", chat_id=chat_id)
            audio = await self.tg.download(str(voice.get("file_id")))
            text = await transcribe(self.session, self.openai_key, audio or b"")
            if not text:
                await self.tg.send("❌ Не разобрал голосовое.", chat_id=chat_id)
                return True
            await self.tg.send(f"📝 Расшифровка: <i>{html.escape(text[:500])}</i>",
                               chat_id=chat_id)

        doc = msg.get("document")
        if doc and not text:
            name = str(doc.get("file_name") or "")
            if not name.lower().endswith((".txt", ".md", ".json")):
                await self.tg.send("📎 Понимаю только .txt/.md/.json — или пришли текстом.",
                                   chat_id=chat_id)
                return True
            blob = await self.tg.download(str(doc.get("file_id")))
            text = (blob or b"").decode("utf-8", "replace").strip()[:6000]

        if not text:
            return False
        await self.make_pack(chat_id, text)
        return True


# ════════════════════════════════════════════════════════════════════════════
#  КОНСОЛЬНЫЙ ЗАПУСК
# ════════════════════════════════════════════════════════════════════════════

def _load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def _amain(brief: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    _load_env()
    passport = Passport.load(os.environ.get("MARKETING_PASSPORT", "marketing.json"))
    missing = passport.missing()
    if missing:
        print("⚠️  Паспорт не заполнен: " + ", ".join(missing))
        print("    Заполни marketing.json — иначе в тексте будут пометки «уточнить».\n")
    async with aiohttp.ClientSession() as session:
        try:
            pack = await generate_pack(session, os.environ.get("ANTHROPIC_API_KEY", ""),
                                       os.environ.get("MARKETING_MODEL") or "claude-opus-5",
                                       passport, brief)
        except RuntimeError as e:
            print(f"❌ {e}")
            return 1
    print(pack.raw)
    problems = pack.problems()
    if problems:
        print("\n⚠️  " + "; ".join(problems))
    return 0


def main() -> None:
    import sys
    if len(sys.argv) < 2:
        print('Использование: python marketing.py "Мы сожгли 3% сапплая"')
        raise SystemExit(2)
    raise SystemExit(asyncio.run(_amain(" ".join(sys.argv[1:]))))


if __name__ == "__main__":
    main()
