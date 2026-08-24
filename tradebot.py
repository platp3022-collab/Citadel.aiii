#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Citadel Trader — спотовый крипто-бот, который сам выводит себе стратегию.

Установка:
    pip install -r requirements.txt

Запуск:
    python tradebot.py evolve       # найти стратегию по истории
    python tradebot.py backtest     # проверить её на истории
    python tradebot.py trade        # торговать на бумажном счёте
    python tradebot.py trade --live # реальные ордера (нужны ключи в .env)

Не финансовый совет. Торговля криптой связана с риском полной потери средств.
"""
import sys

from citadel.cli import main

if __name__ == "__main__":
    sys.exit(main())
