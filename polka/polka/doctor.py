"""Проверка всего сразу: python3 -m polka.doctor

Печатает короткий отчёт: что настроено, куда есть доступ, что отвечает.
Отчёт можно целиком скопировать и прислать: секретов в нём нет, ключи
показаны только хвостами.
"""
from __future__ import annotations

import asyncio
import platform
import socket
import sys
from pathlib import Path

import aiohttp

from .config import ROOT, load_config

OK = "[ да ]"
NO = "[ нет]"
MEH = "[ ?  ]"


def mask(value: str) -> str:
    """Показать, что ключ есть, не показывая ключ."""
    if not value:
        return "не задан"
    return f"задан, оканчивается на {value[-4:]}"


def line(mark: str, name: str, detail: str = "") -> None:
    print(f"  {mark} {name}" + (f"  -  {detail}" if detail else ""))


async def reachable(session: aiohttp.ClientSession, url: str,
                    timeout: float = 12.0) -> tuple[str, str]:
    """Достучались ли до адреса. Возвращает пометку и пояснение.

    Код 403 или 407 обычно приходит не от самого сервиса, а от прокси или
    фильтра посередине, поэтому это не «доступен», а «непонятно».
    """
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status in (403, 407):
                return MEH, (f"ответил {r.status}: похоже, отвечает не сам сервис,"
                             " а фильтр или прокси")
            return OK, f"ответил {r.status}"
    except asyncio.TimeoutError:
        return NO, "молчит, истекло время: адрес закрыт"
    except Exception as exc:  # noqa: BLE001
        return NO, str(exc)[:70]


def port_busy(host: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1.5)
        return probe.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host, port)) == 0


async def amain() -> int:
    cfg = load_config()
    env = ROOT / ".env"

    print("\n══ ПОЛКА, ПРОВЕРКА ══════════════════════════════════════════\n")
    print(f"  система: {platform.system()} {platform.release()}")
    print(f"  python:  {sys.version.split()[0]}")
    print(f"  папка:   {ROOT}")

    print("\n── настройки ───────────────────────────────────────────────")
    line(OK if env.is_file() else NO, "файл .env", str(env) if env.is_file() else "нет файла")
    line(OK if cfg.bot_token else NO, "токен бота", mask(cfg.bot_token))
    line(OK if cfg.chat_id else NO, "chat id", cfg.chat_id or "не задан")
    line(OK if cfg.anthropic_key else NO, "ключ Anthropic", mask(cfg.anthropic_key))
    voice = "Groq" if cfg.groq_key else ("OpenAI" if cfg.openai_key else "")
    line(OK if voice else MEH, "распознавание речи",
         voice or "не задано, голосовые бот не разберёт")
    line(OK if cfg.speech else MEH, "ответ голосом",
         "включён" if cfg.speech else "выключен, вопрос придёт текстом")
    line(OK if cfg.relay_topic else NO, "канал двойного касания",
         "заведён" if cfg.relay_topic else "не заведён")
    line(OK if cfg.public_url else MEH, "адрес мини-приложения",
         cfg.public_url or "нет, кнопки в Telegram не будет")

    print("\n── связь ───────────────────────────────────────────────────")
    async with aiohttp.ClientSession() as session:
        checks = [
            ("Telegram", "https://api.telegram.org"),
            ("Anthropic", "https://api.anthropic.com"),
            ("доска для касания", cfg.relay_url),
            ("сервис туннелей", "https://api.trycloudflare.com"),
        ]
        if cfg.groq_key:
            checks.append(("Groq", "https://api.groq.com"))
        if cfg.openai_key:
            checks.append(("OpenAI", "https://api.openai.com"))

        results = await asyncio.gather(*(reachable(session, url) for _, url in checks))
        for (name, _), (mark, detail) in zip(checks, results):
            line(mark, name, detail)

        print("\n── сама Полка ──────────────────────────────────────────────")
        running = port_busy(cfg.host, cfg.port)
        line(OK if running else NO, f"порт {cfg.port}",
             "занят, похоже Полка запущена" if running else "свободен, Полка не запущена")

        if running:
            mark, detail = await reachable(session, f"http://127.0.0.1:{cfg.port}/health", 8)
            line(mark, "отвечает у себя", detail)

        if cfg.public_url:
            mark, detail = await reachable(
                session, f"{cfg.public_url.rstrip('/')}/health", 15)
            line(mark, "отвечает снаружи", detail)

        if cfg.bot_token:
            good, detail = await check_bot(session, cfg)
            line(OK if good else NO, "бот принимает токен", detail)
            if good and cfg.chat_id:
                menu, where = await check_menu(session, cfg)
                line(OK if menu else MEH, "кнопка мини-приложения", where)

    print("\n═════════════════════════════════════════════════════════════")
    print(verdict(cfg, running))
    return 0


async def check_bot(session: aiohttp.ClientSession,
                    cfg) -> tuple[bool, str]:
    try:
        async with session.post(f"{cfg.telegram_api}/bot{cfg.bot_token}/getMe",
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json(content_type=None)
    except ValueError:
        # Пришёл не json: значит отвечал не Telegram, а что-то посередине.
        return False, "ответил не Telegram, между вами фильтр или прокси"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:70]
    if not isinstance(data, dict):
        return False, "ответ не похож на ответ Telegram"
    if data.get("ok"):
        return True, "@" + data["result"].get("username", "?")
    return False, str(data.get("description"))[:70]


async def check_menu(session: aiohttp.ClientSession, cfg) -> tuple[bool, str]:
    try:
        async with session.post(
            f"{cfg.telegram_api}/bot{cfg.bot_token}/getChatMenuButton",
            json={"chat_id": cfg.chat_id},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json(content_type=None)
    except ValueError:
        return False, "ответил не Telegram, между вами фильтр или прокси"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:70]
    if not isinstance(data, dict):
        return False, "ответ не похож на ответ Telegram"
    button = (data.get("result") or {})
    url = (button.get("web_app") or {}).get("url")
    if not url:
        return False, "не поставлена"
    if cfg.public_url and url.rstrip("/") != cfg.public_url.rstrip("/"):
        return False, f"ведёт на старый адрес {url}"
    return True, url


def verdict(cfg, running: bool) -> str:
    if not cfg.bot_token:
        return "\n  Главное: нет токена бота. Запусти python -m polka.wizard\n"
    if not running:
        return ("\n  Главное: Полка не запущена. Открой окно и набери:\n"
                "      python -m polka\n")
    return ("\n  Полка на ходу. Если что-то всё равно не работает, пришли\n"
            "  этот отчёт целиком: секретов в нём нет.\n")


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
