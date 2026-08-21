#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Запуск Polybot одной командой, без .bat и .sh — на них ругаются антивирусы.

    python start.py                     # спросит токен один раз и запомнит
    python start.py 123456:AA...        # можно сразу передать токен
    python start.py --terminal          # без Telegram, обычный дашборд в терминале
    python start.py --live              # боевой режим (см. README)

Что делает сам, чтобы ничего не пришлось править руками:
    1. проверяет версию Python;
    2. ставит недостающие зависимости (aiohttp);
    3. создаёт .env из .env.example, если его ещё нет;
    4. спрашивает токен бота, если его нет, и записывает в .env;
    5. запускает бота.

Токен хранится только в .env рядом со скриптом. Этот файл не попадает в git
(см. .gitignore) — так и должно быть: кто знает токен, тот управляет ботом.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE = BASE_DIR / ".env.example"
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

try:                                     # русский текст в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def say(text: str = "") -> None:
    print(text, flush=True)


def valid_token(token: str) -> bool:
    """Формат токена @BotFather: <цифры>:<буквы-цифры-дефисы>."""
    return bool(TOKEN_RE.match(token.strip()))


def check_python() -> None:
    if sys.version_info < (3, 10):
        say(f"Нужен Python 3.10 или новее, а здесь {sys.version.split()[0]}.")
        say("Скачай свежий с https://python.org и запусти снова.")
        sys.exit(1)


def ensure_deps() -> None:
    try:
        import aiohttp  # noqa: F401
        return
    except ImportError:
        pass
    say("Ставлю зависимости (это один раз, минуту)...")
    cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", str(BASE_DIR / "requirements.txt")]
    if subprocess.call(cmd) != 0:
        say("Не получилось через pip напрямую, пробую с --user...")
        if subprocess.call(cmd + ["--user"]) != 0:
            say("Установка не удалась. Выполни вручную:")
            say(f"    {sys.executable} -m pip install aiohttp")
            sys.exit(1)
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        say("aiohttp поставился, но не импортируется — перезапусти терминал и попробуй снова.")
        sys.exit(1)


def read_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        if ENV_EXAMPLE.exists():
            ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            say("Создал файл .env из .env.example.")
        else:
            ENV_FILE.write_text("", encoding="utf-8")
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def save_token(token: str) -> None:
    """Записать токен в .env, не трогая остальные строки."""
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    line = f"TELEGRAM_BOT_TOKEN={token}"
    if re.search(r"^TELEGRAM_BOT_TOKEN=.*$", text, flags=re.M):
        text = re.sub(r"^TELEGRAM_BOT_TOKEN=.*$", line, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    say("Токен сохранён в .env — больше спрашивать не буду.")


def ask_token(argv_token: str | None) -> str:
    env = read_env()
    token = (argv_token or "").strip() or env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token and valid_token(token):
        if token != env.get("TELEGRAM_BOT_TOKEN", "").strip():
            save_token(token)
        return token
    if token:
        say("Токен в .env выглядит неправильно, введи заново.")

    say()
    say("Нужен токен Telegram-бота. Где взять:")
    say("  1. открой в Telegram @BotFather;")
    say("  2. отправь /newbot и придумай имя (или /mybots → твой бот → API Token);")
    say("  3. скопируй строку вида 123456789:AAH... и вставь сюда.")
    say()
    for _ in range(3):
        try:
            entered = input("Токен: ").strip()
        except (EOFError, KeyboardInterrupt):
            say("\nОтменено.")
            sys.exit(1)
        if valid_token(entered):
            save_token(entered)
            return entered
        say("Это не похоже на токен — он выглядит как 123456789:AAH... Попробуй ещё раз.")
    say("Три раза не вышло. Запусти снова: python start.py")
    sys.exit(1)


def main() -> int:
    check_python()
    args = sys.argv[1:]
    terminal = "--terminal" in args
    argv_token = next((a for a in args if valid_token(a)), None)
    flags = [a for a in args if a not in ("--terminal",) and a != argv_token]

    say("Polybot — торговый бот Polymarket")
    ensure_deps()

    if terminal:
        say("Запускаю дашборд в терминале. Выход — Ctrl+C.")
        sys.argv = [sys.argv[0]] + flags
        import polybot
        return polybot.main()

    ask_token(argv_token)
    say()
    say("Запускаю бота. Дальше в Telegram:")
    say("  /start — бот запомнит тебя владельцем")
    say("  /pnl   — сколько сейчас в плюсе")
    say("  /panel — живая панель")
    say("Не закрывай это окно: закроешь — бот перестанет отвечать. Выход — Ctrl+C.")
    say()

    sys.argv = [sys.argv[0]] + flags
    import tgapp
    return tgapp.main()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nОстановлено.")
        sys.exit(0)
