#!/usr/bin/env bash
# Поднять Полку и подключить мини-приложение к боту одной командой.
# Двойной клик или ./miniapp.sh
set -u
cd "$(dirname "$0")" || exit 1

hold() { read -r -p "Нажми Enter, чтобы закрыть." _; }

python3 -m pip install --quiet -r requirements.txt || {
  echo "Не поставились зависимости. Нужен python3."; hold; exit 1
}

# Первый запуск: спросить токен, chat id и ключ, проверить их и записать в .env.
if ! grep -qs "^TELEGRAM_BOT_TOKEN=." .env 2>/dev/null; then
  python3 -m polka.wizard || { hold; exit 1; }
fi

# Собранное мини-приложение лежит в репозитории. Пересобираем, только если его нет.
if [ ! -f static/index.html ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "Собираю мини-приложение..."
    (cd miniapp && npm install --silent && npm run build) || { echo "Сборка не прошла."; hold; exit 1; }
  else
    echo "Нет ни собранного интерфейса, ни npm. Поставь Node.js и повтори."
    hold; exit 1
  fi
fi

# cloudflared выдаёт временный https-адрес: Telegram открывает мини-приложения
# только по https, а по http не откроет даже с локальной машины.
CF=$(command -v cloudflared || echo "./cloudflared")
if [ ! -x "$CF" ] && ! command -v cloudflared >/dev/null 2>&1; then
  echo "Нужен cloudflared, чтобы выдать временный https-адрес."
  read -r -p "Скачать его сюда, примерно 40 МБ? [y/N] " answer
  case "$answer" in
    [yY]*)
      case "$(uname -s)-$(uname -m)" in
        Darwin-arm64)  ASSET=cloudflared-darwin-arm64.tgz ;;
        Darwin-x86_64) ASSET=cloudflared-darwin-amd64.tgz ;;
        Linux-aarch64) ASSET=cloudflared-linux-arm64 ;;
        Linux-x86_64)  ASSET=cloudflared-linux-amd64 ;;
        *) echo "Неизвестная система. Поставь cloudflared вручную."; hold; exit 1 ;;
      esac
      URL="https://github.com/cloudflare/cloudflared/releases/latest/download/$ASSET"
      if [ "${ASSET##*.}" = "tgz" ]; then
        curl -sSL "$URL" | tar xz cloudflared || { echo "Не скачалось."; hold; exit 1; }
      else
        curl -sSL -o cloudflared "$URL" || { echo "Не скачалось."; hold; exit 1; }
      fi
      chmod +x cloudflared
      CF=./cloudflared
      ;;
    *) echo "Тогда укажи свой адрес: python3 -m polka.setup --url https://твой-домен"; hold; exit 1 ;;
  esac
fi
export PATH="$PWD:$PATH"

echo "Запускаю Полку..."
python3 -m polka &
POLKA_PID=$!
trap 'kill $POLKA_PID 2>/dev/null' EXIT INT TERM
sleep 3

if ! kill -0 $POLKA_PID 2>/dev/null; then
  echo "Полка не поднялась. Смотри сообщения выше."; hold; exit 1
fi

# Поднимает туннель, прописывает кнопку боту, печатает шаги для BotFather
# и держит туннель, пока открыто окно.
python3 -m polka.setup --tunnel
hold
