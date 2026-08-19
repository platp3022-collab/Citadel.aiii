#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мастер первого запуска: спрашивает токен, сам находит chat_id, проверяет канал,
собирает .env и паспорт проекта — и предлагает сразу запустить бота.

    python setup.py

Ничего не требует, кроме стандартной библиотеки. Токен и ключи пишутся только
в локальный .env (он в .gitignore и в репозиторий не попадает).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows-консоль бывает в cp866/cp1251 — без этого печать эмодзи роняет мастер.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # noqa: PERF203
        pass

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
PASSPORT_PATH = ROOT / "marketing.json"
PASSPORT_EXAMPLE = ROOT / "marketing.example.json"

TOKEN_HINT = "формат: 1234567890:AA... — берётся у @BotFather"


class SetupError(Exception):
    pass


# ── общение с пользователем ────────────────────────────────────────────────

def say(text: str = "") -> None:
    print(text, flush=True)


def head(text: str) -> None:
    say("\n" + "─" * 60)
    say(text)
    say("─" * 60)


def ask(prompt: str, default: str = "", allow_empty: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            say("\nОтменено.")
            raise SystemExit(1)
        value = value or default
        if value or allow_empty:
            return value
        say("  Нужно значение.")


def confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer.startswith(("y", "д"))


# ── Telegram API ──────────────────────────────────────────────────────────

def api(token: str, method: str, **params) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode({k: v for k, v in params.items()
                                   if v is not None}).encode()
    req = urllib.request.Request(url, data=data or None)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            raise SetupError(f"Telegram ответил {e.code}") from e
        raise SetupError(body.get("description") or f"Telegram ответил {e.code}")
    except urllib.error.URLError as e:
        raise SetupError(f"нет связи с api.telegram.org: {e.reason}") from e
    if not body.get("ok"):
        raise SetupError(body.get("description") or "неизвестная ошибка Telegram")
    return body.get("result") or {}


# ── .env ──────────────────────────────────────────────────────────────────

ENV_KEYS = [
    ("TELEGRAM_BOT_TOKEN", "Токен от @BotFather"),
    ("TELEGRAM_CHANNEL_ID", "Канал для алертов и постов (бот должен быть админом)"),
    ("TELEGRAM_CHAT_ID", "Твой личный chat_id: команды и превью контента"),
    ("ANTHROPIC_API_KEY", "Генерация контент-паков и LLM-анализ монет"),
    ("MARKETING_MODEL", "Модель для маркетинга (пусто = claude-opus-5)"),
    ("OPENAI_API_KEY", "Картинки по промтам и расшифровка голосовых"),
    ("X_API_KEY", "X (Twitter) API v2, доступ Read and write"),
    ("X_API_SECRET", ""),
    ("X_ACCESS_TOKEN", ""),
    ("X_ACCESS_TOKEN_SECRET", ""),
]


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(values: dict[str, str]) -> None:
    lines = ["# Создан setup.py. Здесь живут настоящие ключи — файл не коммитится.", ""]
    for key, comment in ENV_KEYS:
        if comment:
            lines.append(f"# {comment}")
        lines.append(f"{key}={values.get(key, '')}")
    lines.append("")
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass


# ── шаги мастера ──────────────────────────────────────────────────────────

def step_token(env: dict[str, str]) -> tuple[str, str]:
    head("1/5 · Токен бота")
    current = env.get("TELEGRAM_BOT_TOKEN", "")
    if current:
        say(f"В .env уже есть токен …{current[-6:]}")
    while True:
        token = ask(f"Токен бота ({TOKEN_HINT})", current, allow_empty=False)
        try:
            me = api(token, "getMe")
        except SetupError as e:
            say(f"  ❌ {e}")
            if not confirm("  Попробовать другой токен?"):
                raise SystemExit(1)
            current = ""
            continue
        username = me.get("username", "")
        say(f"  ✅ Бот найден: @{username} ({me.get('first_name', '')})")
        return token, username


def step_chat_id(token: str, username: str, env: dict[str, str]) -> str:
    head("2/5 · Твой личный чат с ботом")
    current = env.get("TELEGRAM_CHAT_ID", "")
    if current and not confirm(f"Уже сохранён chat_id {current}. Определить заново?", False):
        return current

    say(f"Открой https://t.me/{username} и нажми Start (или напиши боту любое сообщение).")
    say("Жду до 2 минут… (Ctrl+C — пропустить)")
    deadline = time.time() + 120
    offset = 0
    try:
        while time.time() < deadline:
            try:
                updates = api(token, "getUpdates", offset=offset, timeout=20)
            except SetupError as e:
                if "conflict" in str(e).lower():
                    say("  ⚠️ Бот уже где-то запущен — останови его и запусти мастер снова.")
                    return current
                say(f"  ⚠️ {e}")
                break
            for u in updates:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                if chat.get("type") == "private" and chat.get("id"):
                    chat_id = str(chat["id"])
                    name = chat.get("username") or chat.get("first_name") or ""
                    say(f"  ✅ Поймал сообщение от {name}: chat_id = {chat_id}")
                    return chat_id
            time.sleep(1)
    except KeyboardInterrupt:
        say("\n  Пропускаю.")
    if not current:
        say("  Не дождался. Впишешь позже вручную: /id в чате с ботом → TELEGRAM_CHAT_ID в .env")
    return ask("chat_id (Enter — пропустить)", current)


def step_channel(token: str, env: dict[str, str]) -> str:
    head("3/5 · Канал проекта")
    say("Канал, куда бот шлёт алерты и публикует посты. Бота нужно добавить туда")
    say("администратором с правом публикации. Enter — пропустить.")
    current = env.get("TELEGRAM_CHANNEL_ID", "")
    while True:
        channel = ask("Канал (@username или -100...)", current)
        if not channel:
            return ""
        try:
            chat = api(token, "getChat", chat_id=channel)
        except SetupError as e:
            # канала нет, опечатка в имени или он приватный и бот туда не добавлен
            say(f"  ❌ Канал не найден: {e}")
            say("     Проверь имя (Управление каналом → Тип канала → t.me/…) "
                "и что бот добавлен в канал.")
            if confirm("  Ввести другой канал?"):
                current = ""
                continue
            return channel
        say(f"  ✅ Канал: {chat.get('title')} (id {chat.get('id')})")

        # права — проверка необязательная: канал уже принят, это лишь предупреждение
        try:
            member = api(token, "getChatMember", chat_id=channel,
                         user_id=api(token, "getMe").get("id"))
        except SetupError as e:
            say(f"  ⚠️ Права проверить не удалось ({e}).")
            say("     Обычно это значит, что бот ещё не администратор канала:")
            say("     Управление каналом → Администраторы → Добавить → выбери бота")
            say("     → включи «Публикация сообщений».")
            return channel
        status = member.get("status")
        can_post = member.get("can_post_messages", status == "creator")
        if status not in ("administrator", "creator"):
            say("  ⚠️ Бот в канале не администратор — публиковать не сможет. "
                "Выдай права в настройках канала.")
        elif not can_post:
            say("  ⚠️ Бот админ, но без права «Публикация сообщений» — включи его.")
        else:
            say("  ✅ Бот админ и может публиковать.")
        return channel


def step_keys(env: dict[str, str]) -> dict[str, str]:
    head("4/5 · Ключи (все необязательные, Enter — пропустить)")
    out = dict(env)
    say("ANTHROPIC_API_KEY — без него маркетинг-панель выключена (console.anthropic.com).")
    out["ANTHROPIC_API_KEY"] = ask("ANTHROPIC_API_KEY", env.get("ANTHROPIC_API_KEY", ""))
    out["OPENAI_API_KEY"] = ask("OPENAI_API_KEY (картинки, голосовые)",
                                env.get("OPENAI_API_KEY", ""))
    if confirm("Настроить автопостинг в X (нужны 4 ключа из developer.x.com)?", False):
        for key in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
            out[key] = ask(key, env.get(key, ""))
    return out


PASSPORT_QUESTIONS = [
    ("name", "Название монеты"),
    ("ticker", "Тикер (например $DKING)"),
    ("chain", "Блокчейн (TON / Solana / Ethereum / BNB / Base)"),
    ("contract", "Адрес контракта (CA)"),
    ("buy_link", "Ссылка на покупку"),
    ("chart_link", "Ссылка на график (DexScreener/Dextools)"),
    ("market_cap", "Текущий Market Cap (например $100k)"),
    ("target_market_cap", "Целевой Market Cap (например $10M)"),
    ("mascot", "Маскот: кто он, его история и характер"),
    ("tg_channel", "TG Channel (@username)"),
    ("tg_chat", "TG Chat (@username)"),
    ("x_url", "X (Twitter): ссылка"),
]


def step_passport(env: dict[str, str]) -> None:
    head("5/5 · Паспорт проекта (для маркетинг-панели)")
    if not env.get("ANTHROPIC_API_KEY"):
        say("Без ANTHROPIC_API_KEY панель не работает — пропускаю.")
        return
    if not confirm("Заполнить паспорт монеты сейчас?"):
        say("Ок. Позже: /passport set ticker $DKING прямо в чате с ботом.")
        return

    data: dict[str, str] = {}
    if PASSPORT_PATH.exists():
        try:
            data = {k: str(v) for k, v in
                    json.loads(PASSPORT_PATH.read_text(encoding="utf-8")).items()}
        except (OSError, json.JSONDecodeError):
            data = {}
    elif PASSPORT_EXAMPLE.exists():
        shutil.copy(PASSPORT_EXAMPLE, PASSPORT_PATH)

    say("Enter — оставить пустым (публикация заблокируется, пока есть незаполненные поля).\n")
    out: dict[str, str] = {}
    for field, question in PASSPORT_QUESTIONS:
        prev = data.get(field, "")
        prev = "" if prev.startswith("[") else prev
        out[field] = ask(question, prev)
    PASSPORT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    empty = [q for f, q in PASSPORT_QUESTIONS if not out.get(f)]
    if empty:
        say(f"\n  ⚠️ Не заполнено: {', '.join(empty)}. Пока так — кнопки публикации "
            f"заблокированы, черновики генерируются.")
    else:
        say("\n  ✅ Паспорт заполнен, публикация разрешена.")


# ── запуск ────────────────────────────────────────────────────────────────

def main() -> int:
    say("🚀 Настройка бота Citadel.aiii — сканер + маркетинг-панель")
    say("Всё, что введёшь, останется на этом компьютере: .env и marketing.json не коммитятся.")

    env = read_env()
    token, username = step_token(env)
    env["TELEGRAM_BOT_TOKEN"] = token
    env["TELEGRAM_CHAT_ID"] = step_chat_id(token, username, env)
    env["TELEGRAM_CHANNEL_ID"] = step_channel(token, env)
    env = step_keys(env)
    write_env(env)
    say(f"\n✅ Настройки записаны в {ENV_PATH}")
    step_passport(env)

    head("Готово")
    say(f"Бот: @{username}")
    say(f"Личный чат: {env.get('TELEGRAM_CHAT_ID') or 'не задан'}")
    say(f"Канал: {env.get('TELEGRAM_CHANNEL_ID') or 'не задан'}")
    say(f"Маркетинг-панель: {'вкл' if env.get('ANTHROPIC_API_KEY') else 'выкл (нет ANTHROPIC_API_KEY)'}")
    say(f"Автопостинг в X: {'вкл' if env.get('X_ACCESS_TOKEN') else 'выкл'}")
    say("\nВ чате с ботом: /help — команды, /mkt <инфоповод> — контент-пак.")

    if confirm("\nЗапустить бота прямо сейчас?"):
        say("Запускаю. Пока окно открыто — бот работает. Ctrl+C — остановить.\n")
        return subprocess.call([sys.executable, str(ROOT / "memebot.py")])
    say("Позже запусти: python memebot.py  (или run.sh / run.bat)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\nПрервано.")
        raise SystemExit(130)
