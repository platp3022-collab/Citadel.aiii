#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Веб-панель Citadel: запусти этот файл — откроется браузер с терминалом,
кнопками, состоянием счёта и графиком.

    python webui.py                 # откроет Microsoft Edge (или браузер по умолчанию)
    python webui.py --mode dex      # сразу в режиме DEX
    python webui.py --allow-live    # разрешить кнопку реальной торговли
    python webui.py --price-interval 2   # опрашивать цену чаще
    python webui.py --no-browser    # просто поднять сервер и напечатать адрес

Сервер слушает только 127.0.0.1 и требует токен из адресной строки, поэтому
снаружи к панели не подключиться.
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import subprocess
import sys
import webbrowser


def open_in_edge(url: str) -> str:
    """Пытается открыть Microsoft Edge, иначе — браузер по умолчанию."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(f"microsoft-edge:{url}")           # noqa: S606 — штатный способ
            return "Microsoft Edge"
        if system == "Darwin":
            subprocess.run(["open", "-a", "Microsoft Edge", url], check=True,
                           capture_output=True)
            return "Microsoft Edge"
        for binary in ("microsoft-edge-stable", "microsoft-edge", "msedge"):
            try:
                subprocess.Popen([binary, url], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return "Microsoft Edge"
            except FileNotFoundError:
                continue
    except (OSError, subprocess.SubprocessError):
        pass
    return "браузер по умолчанию" if webbrowser.open(url) else ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="webui", description="Локальная панель управления ботом.")
    p.add_argument("--mode", choices=("cex", "dex"), default="cex",
                   help="с чего начать: биржа или DEX")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--allow-live", action="store_true",
                   help="разрешить кнопку реальной торговли (по умолчанию запрещена)")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    p.add_argument("--no-live-prices", action="store_true",
                   help="не опрашивать цены самому (тогда цена обновляется только ботом)")
    p.add_argument("--price-interval", type=float, default=4.0,
                   help="как часто панель спрашивает цену, секунд (по умолчанию 4)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from citadel.web.server import serve

    try:
        httpd, url = serve(args.host, args.port, args.mode, args.allow_live,
                           live_prices=not args.no_live_prices,
                           poll_interval=args.price_interval)
    except OSError as e:
        print(f"Не удалось занять порт {args.port}: {e}\nПопробуй --port 8766")
        return 1

    print("\n  Citadel · панель управления")
    print(f"  адрес:  {url}")
    print("  закрой это окно — панель остановится\n")
    if args.allow_live:
        print("  ⚠  реальная торговля из панели РАЗРЕШЕНА (--allow-live)\n")
    if not args.no_live_prices:
        print(f"  живые цены: панель сама опрашивает рынок раз в {args.price_interval:g} с\n")

    if not args.no_browser:
        where = open_in_edge(url)
        print(f"  открываю: {where}\n" if where
              else "  браузер не открылся — скопируй адрес выше вручную\n")
    panel = getattr(httpd.RequestHandlerClass, "panel", None)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        if panel is not None:
            # иначе запущенный из панели бот остался бы торговать без присмотра
            if panel.runner.running:
                print(f"останавливаю запущенное: {panel.runner.command}")
                panel.runner.stop()
            panel.stop_stream()
            if panel.poller:
                panel.poller.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
