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
from collections import deque
from pathlib import Path

import aiohttp

from .config import ROOT, load_config

TUNNEL_PATTERN = re.compile(r"https://([a-z0-9-]+)\.trycloudflare\.com")

# Служебные адреса самого cloudflared: они попадаются в его логе раньше, чем
# выданный туннель, и раньше их принимали за адрес приложения.
TUNNEL_SERVICE_HOSTS = {"api", "www", "update", "region1", "region2"}


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
        # Telegram нормализует адрес и может дописать косую черту в конце.
        if not same_url(got, url):
            raise SetupError(f"кнопка меню не сохранилась: Telegram отдаёт {got!r}")
        say("проверено: Telegram отдаёт тот же адрес", quiet)
    return name


def same_url(left: str | None, right: str | None) -> bool:
    """Сравнение адресов без учёта завершающей косой черты."""
    return (left or "").rstrip("/") == (right or "").rstrip("/")


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


async def tunnel_service_reachable(timeout: float = 12.0) -> bool:
    """Быстрая проверка: пускает ли сеть к сервису туннелей.

    Без неё cloudflared молча ждёт таймаута и падает с невнятной ошибкой.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.trycloudflare.com",
                                   timeout=aiohttp.ClientTimeout(total=timeout)):
                return True
    except Exception:  # noqa: BLE001 — интересен только сам факт недоступности
        return False


async def open_tunnel(port: int, binary: str = "cloudflared",
                      extra: list[str] | None = None
                      ) -> tuple[str, asyncio.subprocess.Process]:
    """Поднять временный туннель и дождаться выданного адреса."""
    try:
        process = await asyncio.create_subprocess_exec(
            binary, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate",
            *(extra or []),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        raise SetupError(
            "не найден cloudflared. Поставь его или укажи свой адрес: "
            "python3 -m polka.setup --url https://твой-адрес"
        ) from None

    assert process.stdout is not None
    # Последние строки держим при себе: без них "закрылся" ничего не объясняет.
    tail: deque[str] = deque(maxlen=12)
    try:
        while True:
            raw = await asyncio.wait_for(process.stdout.readline(), timeout=90)
            if not raw:
                raise SetupError(
                    "cloudflared закрылся, не выдав адрес." + explain(tail))
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                tail.append(line)
            for found in TUNNEL_PATTERN.finditer(line):
                if found.group(1) not in TUNNEL_SERVICE_HOSTS:
                    return found.group(0), process
    except asyncio.TimeoutError:
        process.terminate()
        raise SetupError("cloudflared не выдал адрес за полторы минуты." + explain(tail)) from None


# Подсказки ищем по точным формулировкам cloudflared. Раньше здесь стояла
# проверка на подстроку "quic", и она срабатывала на слове "quick Tunnel",
# уводя диагностику в сторону.
TUNNEL_HINTS: tuple[tuple[str, str], ...] = (
    ("failed to request quick tunnel",
     "Твоя сеть не пускает к api.trycloudflare.com. Это не про Полку и не про бота:\n"
     "     до сервиса туннелей просто нет доступа. Обычно помогает включить VPN\n"
     "     либо раздать интернет с телефона и повторить."),
    ("context deadline exceeded",
     "Запрос ушёл в никуда и истёк по времени: адрес закрыт провайдером,\n"
     "     брандмауэром или антивирусом."),
    ("no such host",
     "Адрес не разрешается в DNS. Попробуй сменить DNS на 1.1.1.1 или 8.8.8.8."),
    ("quic handshake",
     "Режется QUIC. Повторный заход поверх http2 должен помочь."),
    ("not in allowlist",
     "Сеть пропускает только разрешённые адреса. Нужен другой канал связи."),
)


def explain(tail: "deque[str]") -> str:
    """Собрать понятную подсказку из того, что успел напечатать cloudflared."""
    text = "\n".join(tail)
    lowered = text.lower()
    hints = [hint for marker, hint in TUNNEL_HINTS if marker in lowered]
    out = ""
    if hints:
        out += "\n" + "\n".join(f"  {hint}" for hint in hints)
    if text:
        out += "\n\nЧто сказал cloudflared:\n" + "\n".join(f"  {line}" for line in tail)
    return out


BLOCKED_MESSAGE = """твоя сеть не пускает к api.trycloudflare.com.

Это не поломка Полки: бот, разбор мыслей и напоминания работают, а вот
временный адрес для мини-приложения выдать неоткуда. Три выхода:

  1. Включить VPN и повторить. Самый быстрый способ проверить.
  2. Раздать интернет с телефона и повторить: у мобильного оператора
     этот адрес обычно открыт.
  3. Поставить Полку на сервер со своим доменом. Тогда адрес постоянный,
     туннель не нужен вообще, и всё работает без открытого окна:
         python3 -m polka.setup --url https://твой-домен

Пока адреса нет, запусти без мини-приложения: python -m polka
Бот будет ловить мысли и напоминать, только без кнопки с интерфейсом."""

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
            port = args.port or load_config().port
            print("  поднимаю туннель...")
            if not await tunnel_service_reachable():
                raise SetupError(BLOCKED_MESSAGE)
            try:
                url, tunnel = await open_tunnel(port)
            except SetupError as first:
                # QUIC часто зарезан у провайдера или брандмауэром, http2 проходит.
                print(f"  первый заход не удался, пробую поверх http2\n{first}\n")
                url, tunnel = await open_tunnel(port, extra=["--protocol", "http2"])
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
            print(f"Мини-приложение живёт по адресу: {url}\n")
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
