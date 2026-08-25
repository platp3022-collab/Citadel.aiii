#!/usr/bin/env bash
# Один клик: готовит окружение, спрашивает настройки при первом запуске,
# запускает бота и сам поднимает его обратно, если тот упал.
set -u
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python3 не найден. Поставь Python 3.11+ с python.org и запусти скрипт снова."
    read -r -p "Enter — выход" _ || true
    exit 1
fi

if [ ! -d .venv ]; then
    echo "Готовлю окружение, это займёт минуту..."
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip >/dev/null 2>&1 || true
pip install -q -r requirements.txt

# ---- первый запуск: собираем .env ----
if [ ! -f .env ]; then
    echo
    echo "Первый запуск — настроим бота. Три вопроса, дальше он всё делает сам."
    echo
    read -r -p "Токен бота от @BotFather: " TG_TOKEN
    read -r -p "Твой chat_id (узнать: напиши боту /id): " TG_CHAT
    read -r -p "Ключ Anthropic (Enter — пропустить, бот будет без вердикта нейросети): " ANTH_KEY
    read -r -p "Профиль [AXIOM]: " PRESET
    PRESET=${PRESET:-AXIOM}
    cat > .env <<EOF
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHANNEL_ID=
TELEGRAM_CHAT_ID=${TG_CHAT}
ANTHROPIC_API_KEY=${ANTH_KEY}
FRESH_PRESET=${PRESET}
JUPITER_API_KEY=
EOF
    echo "Настройки сохранены в .env — больше спрашивать не буду."
    echo
fi

echo "Бот запущен. Не закрывай это окно — пока оно открыто, он сканирует рынок."
echo "Упадёт из-за сети — подниму сам через 10 секунд. Остановить: Ctrl+C."
echo

while true; do
    python memebot.py
    code=$?
    if [ $code -eq 0 ] || [ $code -eq 130 ]; then
        echo "Бот остановлен."
        break
    fi
    echo "Бот упал (код $code). Перезапуск через 10 секунд..."
    sleep 10
done
