"""Подключение мини-приложения к боту: python3 -m polka.setup

Делает всё, что вообще может сделать бот по своему токену:
проверяет токен, ставит кнопку меню с адресом мини-приложения, команды и описание.
Остальное (короткая ссылка t.me/бот/polka) заводится только с твоего аккаунта
в @BotFather - это ограничение Telegram, не программы.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import aiohttp

from .config import ROOT, load_config

TUNNEL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class SetupError(Exception):
    pass


async def call(session: aiohttp.ClientSession, api: str, method: str,
               payload: dict | None = None) -> dict:
    async with session.post(f"{api}/{method}", json=payload or {},
                            timeout=aiohttp.ClientTimeout(total=30)) as response:
        data = await response.json(content_type=None)
    if not data.get("ok"):
        raise SetupError(f"{method}: {data.get('description') or data}")
    return data.get("result")


async def wire(url: str, quiet: bool = False) -> str:
    """Прописать мини-приложение боту. Возвращает имя бота."""
    cfg = load_config()
    if not cfg.bot_token:
        raise SetupError("нет TELEGRAM_BOT_TOKEN в polka/.env")
    if not url.startswith("https://"):
        raise SetupError(f"Telegram открывает мини-приложения только по https, а тут {url}")

    api = f"{cfg.telegram_api}/bot{cfg.bot_token}"
    async with aiohttp.ClientSession() as session:
        me = await call(session, api, "getMe")
        name = me.get("username", "?")
        say(f"бот на связи: @{name}", quiet)

        # Кнопка меню в чате открывает мини-приложение. Это и есть Mini App
        # в рабочем виде: отдельная регистрация для него не нужна.
        menu: dict = {
            "menu_button": {
                "type": "web_app",
                "text": "Полка",
                "web_app": {"url": url},
            }
        }
        if cfg.chat_id:
            menu["chat_id"] = cfg.chat_id
        await call(session, api, "setChatMenuButton", menu)
        say(f"кнопка меню ведёт на {url}", quiet)

        await call(session, api, "setMyCommands", {"commands": [
            {"command": "polka", "description": "открыть полки"},
            {"command": "vazhnoe", "description": "что горит прямо сейчас"},
            {"command": "id", "description": "показать chat id"},
        ]})
        await call(session, api, "setMyShortDescription", {
            "short_description": "Скажи мысль. Разложу по полкам и напомню вовремя.",
        })
        await call(session, api, "setMyDescription", {
            "description": "Полка ловит мысль в любом виде, решает, что это и насколько "
                           "важно, и напоминает так, что пролистать не выйдет.",
        })
        say("команды и описание записаны", quiet)

        check = await call(session, api, "getChatMenuButton",
                           {"chat_id": cfg.chat_id} if cfg.chat_id else None)
        got = ((check or {}).get("web_app") or {}).get("url")
        if got != url:
            raise SetupError(f"кнопка меню не сохранилась: Telegram отдаёт {got!r}")
        say("проверено: Telegram отдаёт тот же адрес", quiet)
    return name


def say(text: str, quiet: bool) -> None:
    if not quiet:
        print(f"  {text}")


def save_url(url: str) -> None:
    """Записать адрес в polka/.env, чтобы бот подставлял его в кнопки."""
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.is_file() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("POLKA_PUBLIC_URL="):
            out.append(f"POLKA_PUBLIC_URL={url}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"POLKA_PUBLIC_URL={url}")
    env.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ["POLKA_PUBLIC_URL"] = url


async def open_tunnel(port: int, binary: str = "cloudflared") -> tuple[str, asyncio.subprocess.Process]:
    """Поднять временный туннель и дождаться выданного адреса."""
    try:
        process = await asyncio.create_subprocess_exec(
            binary, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        raise SetupError(
            "не найден cloudflared. Поставь его или укажи свой адрес: "
            "python3 -m polka.setup --url https://твой-адрес"
        ) from None

    assert process.stdout is not None
    try:
        while True:
            raw = await asyncio.wait_for(process.stdout.readline(), timeout=60)
            if not raw:
                raise SetupError("cloudflared закрылся, не выдав адрес")
            found = TUNNEL_PATTERN.search(raw.decode("utf-8", "replace"))
            if found:
                return found.group(0), process
    except asyncio.TimeoutError:
        process.terminate()
        raise SetupError("cloudflared не выдал адрес за минуту") from None


BOTFATHER_STEPS = """
Кнопка мини-приложения уже в чате: открой бота, нажми «Полка» слева от поля ввода.

Отдельная короткая ссылка вида t.me/{name}/polka заводится только с твоего
аккаунта, у бота нет прав её создать. Если она нужна, в @BotFather:

  /newapp  ->  выбрать @{name}
  название:   Полка
  описание:   Мысли по полкам
  картинка:   640x360
  ссылка:     {url}
  короткое имя: polka

После этого приложение открывается по t.me/{name}/polka и появляется в профиле бота.
"""


async def amain(args: argparse.Namespace) -> int:
    tunnel = None
    try:
        url = args.url
        if not url:
            cfg = load_config()
            url = cfg.public_url
        if not url and args.tunnel:
            print("  поднимаю туннель...")
            url, tunnel = await open_tunnel(args.port or load_config().port)
            print(f"  туннель: {url}")
        if not url:
            raise SetupError(
                "нет адреса. Запусти с --tunnel, чтобы поднять временный, "
                "или передай свой: --url https://..."
            )

        name = await wire(url)
        save_url(url)
        print(BOTFATHER_STEPS.format(name=name, url=url))

        if tunnel:
            print("Туннель живёт, пока открыто это окно. Закроешь - адрес пропадёт,\n"
                  "и мини-приложение перестанет открываться. Для постоянной работы\n"
                  "нужен свой домен либо сервер.\n")
            await tunnel.wait()
        return 0
    except SetupError as exc:
        print(f"\nНе вышло: {exc}\n", file=sys.stderr)
        return 1
    finally:
        if tunnel and tunnel.returncode is None:
            tunnel.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="polka.setup", description="Подключить мини-приложение к боту")
    parser.add_argument("--url", help="публичный https-адрес Полки")
    parser.add_argument("--tunnel", action="store_true",
                        help="поднять временный туннель через cloudflared")
    parser.add_argument("--port", type=int, help="порт, на котором слушает Полка")
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
