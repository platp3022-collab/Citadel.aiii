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
    echo ".env не найден — копирую из .env.example, дефолтов хватит для бумажного режима."
    cp .env.example .env
fi

echo "Запускаю бота в бумажном режиме. Реальные деньги не тратятся."
echo "Не закрывай терминал — закроешь, бот остановится. Выход: Ctrl+C."
python polybot.py
