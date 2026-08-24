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

echo "1) качаю историю..."
python tradebot.py fetch
echo "2) ищу стратегию (это займёт минуту)..."
python tradebot.py evolve
echo "3) торгую на бумажном счёте. Не закрывай терминал."
python tradebot.py trade
