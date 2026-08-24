#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Citadel DEX — тот же бот, что и tradebot.py, но на децентрализованных биржах.

Данные:     DexScreener (пары, ликвидность, объёмы) + GeckoTerminal (свечи)
Исполнение: бумажный счёт или свопы через Jupiter (Solana)

    python dexbot.py discover     # подобрать пары
    python dexbot.py evolve       # найти стратегию под каждую
    python dexbot.py trade        # торговать на бумаге
    python dexbot.py trade --live # реальные свопы (нужен кошелёк в .env)

Не финансовый совет. На DEX риск потерять всё выше, чем где-либо ещё.
"""
import sys

from citadel.dex.cli import main

if __name__ == "__main__":
    sys.exit(main())
