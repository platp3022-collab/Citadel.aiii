#!/usr/bin/env bash
# Запуск Полки. Двойной клик или ./run.sh
cd "$(dirname "$0")" || exit 1

if [ ! -f .env ]; then
  echo "Нет файла .env. Скопируй .env.example в .env и заполни."
  read -r -p "Нажми Enter, чтобы закрыть."
  exit 1
fi

python3 -m pip install --quiet -r requirements.txt || {
  echo "Не поставились зависимости. Нужен python3."
  read -r -p "Нажми Enter, чтобы закрыть."
  exit 1
}

echo "Полка запущена. Не закрывай это окно."
python3 -m polka
