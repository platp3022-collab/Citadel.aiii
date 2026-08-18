"""Запуск Полки: бот, планировщик и веб-сервер в одном процессе."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    sys.exit("Нужен aiohttp:  pip install -r polka/requirements.txt")

from .api import build_app
from .bot import Bot
from .brain import Brain
from .config import DATA_DIR, load_config
from .db import Store
from .flow import Flow
from .telegram import Telegram
from . import relay, scheduler

log = logging.getLogger("polka")


async def amain(args: argparse.Namespace) -> int:
    cfg = load_config()

    if not cfg.bot_token and not args.dry:
        print("Нет TELEGRAM_BOT_TOKEN. Заполни polka/.env (см. .env.example)"
              " или запусти с --dry.", file=sys.stderr)
        return 2
    if not cfg.anthropic_key:
        log.warning("Нет ANTHROPIC_API_KEY: мысли будут падать во «Входящее» "
                    "без разбора моделью.")

    store = Store(DATA_DIR / "polka.sqlite3")
    stop = asyncio.Event()

    async with aiohttp.ClientSession() as session:
        tg = Telegram(session, cfg.bot_token, cfg.chat_id, dry=args.dry,
                      api_base=cfg.telegram_api)
        brain = Brain(cfg.anthropic_key, cfg.model)
        flow = Flow(cfg, store, brain, tg)

        if args.capture:
            thought = await flow.capture(args.capture, source="cli")
            print(f"{thought.get('title')}  →  полка {thought.get('shelf')}, "
                  f"важность {thought.get('importance')}/5")
            store.close()
            return 0

        app = build_app(cfg, store, flow)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, cfg.host, cfg.port)
        try:
            await site.start()
        except OSError as exc:
            # Чаще всего это вторая копия Полки, оставшаяся с прошлого запуска.
            print(f"\nПорт {cfg.port} занят: {exc}\n\n"
                  "Скорее всего, Полка уже запущена в другом окне. Закрой его\n"
                  "и запусти заново. Либо задай другой порт в polka/.env:\n"
                  f"    POLKA_PORT={cfg.port + 1}\n", file=sys.stderr)
            store.close()
            return 3
        log.info("Веб-сервер на http://%s:%d", cfg.host, cfg.port)
        tunnel = None
        if args.tunnel:
            # Туннель поднимаем в том же процессе: раньше Полка и туннель жили
            # порознь, и закрытие одного оставляло кнопку висеть в пустоту.
            tunnel = await raise_tunnel(cfg, stop)

        if cfg.public_url and not args.tunnel:
            print(f"\nВ настройках записан адрес {cfg.public_url}")
            print("Если он от прошлого туннеля, кнопка в Telegram сейчас мертва.")
            print("Запусти с ключом --tunnel, чтобы поднять свежий адрес.")
        elif cfg.public_url:
            print(f"\nМини-приложение в Telegram: {cfg.public_url}")
        # Без публичного адреса интерфейс всё равно можно открыть у себя.
        if cfg.capture_token:
            # 127.0.0.1, а не localhost: в Windows localhost уходит в IPv6,
            # а Полка слушает только IPv4.
            print(f"\nПосмотреть полки в браузере:\n"
                  f"  http://127.0.0.1:{cfg.port}/?token={cfg.capture_token}\n")
        if cfg.relay_topic:
            print("Двойное касание крышки на связи.")
        print("Полка слушает. Пиши боту в Telegram, окно не закрывай.\n")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:      # Windows
                pass

        tasks = [asyncio.create_task(scheduler.run(store, flow, stop))]
        if cfg.relay_topic:
            tasks.append(asyncio.create_task(relay.run(cfg, flow, stop)))
        if cfg.bot_token or args.dry:
            bot = Bot(cfg, store, flow, tg, session)
            tasks.append(asyncio.create_task(bot.run(stop)))

        try:
            await stop.wait()
        except KeyboardInterrupt:
            stop.set()
        log.info("Останавливаюсь")
        if tunnel and tunnel.returncode is None:
            tunnel.terminate()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()

    store.close()
    return 0


async def raise_tunnel(cfg, stop: asyncio.Event):
    """Поднять туннель и повесить кнопку боту. Ошибка тут не валит Полку:
    без адреса она работает, просто без кнопки в Telegram."""
    from . import setup as setup_module

    print("\nПоднимаю адрес для мини-приложения...")
    try:
        if not await setup_module.tunnel_service_reachable():
            raise setup_module.SetupError(setup_module.BLOCKED_MESSAGE)
        try:
            url, process = await setup_module.open_tunnel(cfg.port)
        except setup_module.SetupError:
            url, process = await setup_module.open_tunnel(
                cfg.port, extra=["--protocol", "http2"])
        print(f"  адрес: {url}")

        print("  проверяю, что через него отвечает Полка...")
        if not await setup_module.tunnel_reaches_polka(url):
            process.terminate()
            raise setup_module.SetupError(
                setup_module.UNREACHABLE_MESSAGE.format(url=url, port=cfg.port))

        print("  вешаю кнопку боту...")
        await setup_module.wire(url)
        setup_module.save_url(url)
        print("  кнопка в Telegram обновлена\n")
        return process
    except Exception as exc:  # noqa: BLE001
        # Что бы ни случилось с туннелем, Полка обязана продолжать работать:
        # бот, разбор и напоминания от адреса не зависят.
        if not isinstance(exc, setup_module.SetupError):
            log.exception("Туннель не поднялся из-за неожиданной ошибки")
        print(f"\nБез мини-приложения: {exc}\n")
        # Кнопка от прошлого запуска ведёт в никуда, лучше её убрать.
        if cfg.public_url:
            if await setup_module.clear_menu_button(cfg):
                print("Кнопку в Telegram снял, чтобы она не вела на мёртвый адрес.")
            # Адрес протух вместе с кнопкой, иначе он ещё и напечатается ниже.
            setup_module.save_url("")
            cfg = load_config()
        print("Всё остальное работает: бот, разбор, напоминания.\n")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(prog="polka", description="Полка: мысли по полкам")
    parser.add_argument("--dry", action="store_true",
                        help="ничего не слать в Telegram, писать в консоль")
    parser.add_argument("--tunnel", action="store_true",
                        help="поднять адрес для мини-приложения в этом же окне")
    parser.add_argument("--capture", metavar="ТЕКСТ",
                        help="разобрать одну мысль и выйти")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
