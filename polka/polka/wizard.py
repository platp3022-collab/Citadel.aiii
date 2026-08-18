"""Мастер первого запуска: python3 -m polka.wizard

Спрашивает три вещи, каждую сразу проверяет на живых сервисах и складывает
в polka/.env. Ничего не проверенного в файл не попадает.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

import aiohttp

from .config import ROOT, load_config

ENV = ROOT / ".env"


def ask(question: str, secret_hint: str = "") -> str:
    print(f"\n{question}")
    if secret_hint:
        print(f"  {secret_hint}")
    try:
        return input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)


def ok(text: str) -> None:
    print(f"  готово: {text}")


def bad(text: str) -> None:
    print(f"  не то: {text}")


async def check_bot(session: aiohttp.ClientSession, api: str,
                    token: str) -> str | None:
    """Проверить токен. Возвращает имя бота или None."""
    try:
        async with session.post(f"{api}/bot{token}/getMe",
                                timeout=aiohttp.ClientTimeout(total=20)) as response:
            data = await response.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        bad(f"Telegram не ответил: {exc}")
        return None
    if not data.get("ok"):
        bad(f"Telegram отклонил токен: {data.get('description')}")
        return None
    return data["result"].get("username")


async def find_chat_id(session: aiohttp.ClientSession, api: str,
                       token: str) -> str | None:
    """Вытащить chat id из последних сообщений боту."""
    try:
        async with session.post(f"{api}/bot{token}/getUpdates",
                                json={"timeout": 0, "allowed_updates": ["message"]},
                                timeout=aiohttp.ClientTimeout(total=25)) as response:
            data = await response.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        bad(f"Telegram не ответил: {exc}")
        return None
    if not data.get("ok"):
        bad(str(data.get("description")))
        return None
    for update in reversed(data.get("result") or []):
        chat = ((update.get("message") or {}).get("chat") or {})
        if chat.get("id"):
            return str(chat["id"])
    return None


async def board_reachable(url: str) -> bool:
    """Доступна ли доска, через которую приходит двойное касание."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)):
                return True
    except Exception:  # noqa: BLE001 — важен только сам факт
        return False


async def check_voice_key(key: str) -> tuple[bool, str]:
    """Проверить ключ распознавания речи. Groq и OpenAI отвечают одинаково."""
    host = "https://api.groq.com/openai/v1" if key.startswith("gsk_") \
        else "https://api.openai.com/v1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{host}/models",
                                   headers={"Authorization": f"Bearer {key}"},
                                   timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status == 200:
                    return True, "GROQ_API_KEY" if key.startswith("gsk_") else "OPENAI_API_KEY"
                if response.status in (401, 403):
                    return False, "ключ не принят"
                return False, f"сервис ответил {response.status}"
    except Exception as exc:  # noqa: BLE001
        return False, f"не достучался: {exc}"


async def check_key(key: str) -> tuple[bool, str]:
    """Проверить ключ Anthropic самым дешёвым запросом. Возвращает (годен, что не так)."""
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return False, "не установлен пакет anthropic: pip install -r requirements.txt"

    client = AsyncAnthropic(api_key=key)
    try:
        await client.models.list()
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "authentication" in text.lower() or "401" in text:
            return False, "ключ не принят, проверь что скопирован целиком"
        return False, text[:200]

    # Ключ рабочий. Отдельно проверяем, есть ли на счету деньги: без них
    # разбор будет падать на каждой мысли, а выглядеть это будет как молчание.
    try:
        await client.messages.create(
            model="claude-haiku-4-5", max_tokens=1,
            messages=[{"role": "user", "content": "."}],
        )
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "credit balance" in text.lower() or "billing" in text.lower():
            return False, ("ключ верный, но на счету нет кредитов. Пополни: "
                           "https://console.anthropic.com/settings/billing")
        return True, f"ключ принят, пробный запрос вернул: {text[:120]}"
    return True, ""


def write_env(values: dict[str, str]) -> None:
    """Дописать значения в .env, не трогая остальные строки."""
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.is_file() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in remaining.items())
    ENV.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
    for key, value in values.items():
        os.environ[key] = value


async def amain() -> int:
    print("\nПолка. Настройка занимает пару минут.")
    print("Всё, что ты введёшь, ляжет в polka/.env на этом компьютере и больше никуда.")

    cfg = load_config()
    api = cfg.telegram_api
    collected: dict[str, str] = {}

    async with aiohttp.ClientSession() as session:
        # ── токен бота ────────────────────────────────────────────────────
        token = cfg.bot_token
        name = await check_bot(session, api, token) if token else None
        while not name:
            token = ask("Токен бота от @BotFather:",
                        "команда /newbot, если бота ещё нет. Строка вида 8123456789:AAH...")
            if not token:
                return 1
            name = await check_bot(session, api, token)
        ok(f"бот @{name}")
        collected["TELEGRAM_BOT_TOKEN"] = token

        # ── chat id ───────────────────────────────────────────────────────
        chat_id = cfg.chat_id
        if not chat_id:
            print(f"\nТеперь напиши что угодно боту @{name} в Telegram.")
            input("Написал? Нажми Enter > ")
            chat_id = await find_chat_id(session, api, token)
            if chat_id:
                ok(f"твой chat id {chat_id}")
            else:
                chat_id = ask("Не увидел сообщения. Введи chat id вручную:",
                              "узнать можно у бота @userinfobot")
        else:
            ok(f"chat id {chat_id} уже записан")
        if not chat_id:
            return 1
        collected["POLKA_CHAT_ID"] = chat_id

    # ── ключ Anthropic ────────────────────────────────────────────────────
    key = cfg.anthropic_key
    good, trouble = await check_key(key) if key else (False, "")
    while not good:
        if trouble:
            bad(trouble)
        key = ask("Ключ Anthropic:",
                  "https://console.anthropic.com/settings/keys, строка вида sk-ant-...\n"
                  "  пустая строка - пропустить, тогда мысли не будут разбираться")
        if not key:
            print("  ладно, без разбора. Вписать ключ можно потом в polka/.env")
            break
        good, trouble = await check_key(key)
    if good:
        ok("ключ рабочий, кредиты на месте" if not trouble else trouble)
        collected["ANTHROPIC_API_KEY"] = key

    # ── голосовые в Telegram ──────────────────────────────────────────────
    if not (cfg.groq_key or cfg.openai_key
            or os.environ.get("POLKA_VOICE_SKIPPED")):
        print("\nГолосовые сообщения боту требуют распознавания речи.")
        print("У Groq оно бесплатное: console.groq.com/keys, ключ вида gsk_...")
        voice_key = ask("Ключ распознавания речи:",
                        "пустая строка - пропустить, голосом можно будет"
                        " диктовать через двойное касание крышки")
        while voice_key:
            good_voice, where = await check_voice_key(voice_key)
            if good_voice:
                ok("голосовые будут расшифровываться")
                collected[where] = voice_key
                break
            bad(where)
            voice_key = ask("Ключ распознавания речи:", "пустая строка - пропустить")
        if not voice_key:
            print("  ладно, без голосовых в чате")
            # Помечаем отказ, иначе вопрос повторялся бы при каждом обновлении.
            collected["POLKA_VOICE_SKIPPED"] = "1"
    elif cfg.groq_key or cfg.openai_key:
        ok("ключ распознавания речи уже записан")

    if not os.environ.get("POLKA_CAPTURE_TOKEN") and not cfg.capture_token:
        collected["POLKA_CAPTURE_TOKEN"] = secrets.token_urlsafe(24)

    # Доска для двойного касания. Имя случайное и длинное: оно же и пароль.
    if not cfg.relay_topic:
        collected["POLKA_RELAY_TOPIC"] = "polka-" + secrets.token_hex(12)
        if await board_reachable(cfg.relay_url):
            ok("завёл канал для двойного касания крышки")
        else:
            bad(f"канал для двойного касания завёл, но {cfg.relay_url} сейчас"
                " недоступен.\n     Двойное касание заработает, когда появится доступ."
                " Всё остальное работает.")

    write_env(collected)
    print(f"\nЗаписано в {ENV}")
    print("\nДальше: ./miniapp.sh поднимет Полку и повесит кнопку в Telegram.")
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
