#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python3 не найден. Установи Python 3.10+ и запусти скрипт снова."
    exit 1
fi

# Внешних зависимостей нет — виртуальное окружение не нужно.
echo "Собираю бриф..."
python3 market_dashboard.py "$@"

echo
echo "Готово. Страница открылась в браузере и лежит в briefs/."
echo "Нажми Enter, чтобы закрыть это окно."
read -r _
