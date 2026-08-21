#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python3 не найден. Установи Python 3.11+ и запусти скрипт снова."
    exit 1
fi

if [ ! -d .venv ]; then
    echo "Готовлю окружение, подожди..."
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
fi

if ! grep -qE '^TELEGRAM_BOT_TOKEN=.+' .env || ! grep -qE '^TELEGRAM_CHAT_ID=.+' .env; then
    echo "Открой файл .env и заполни две строки:"
    echo "  TELEGRAM_BOT_TOKEN — токен от @BotFather"
    echo "  TELEGRAM_CHAT_ID   — твой chat_id, узнать у @userinfobot"
    exit 1
fi

echo "Запускаю Telegram-бота в бумажном режиме. Реальные деньги не тратятся."
echo "Напиши боту /panel. Не закрывай терминал — выход: Ctrl+C."
python tgapp.py
