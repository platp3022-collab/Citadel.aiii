#!/usr/bin/env bash
# Разворачивает бота на чистом Ubuntu/Debian VPS одной командой.
# Использование:  sudo bash deploy/setup_vps.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/platp3022-collab/Citadel.aiii.git}"
BRANCH="${BRANCH:-claude/meme-coin-analyzer-bot-3qqsiy}"
TARGET="${TARGET:-/opt/memebot}"

echo "==> Ставлю docker и git"
if ! command -v docker >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl git
    curl -fsSL https://get.docker.com | sh
fi

echo "==> Забираю код в $TARGET"
if [ -d "$TARGET/.git" ]; then
    git -C "$TARGET" fetch origin "$BRANCH"
    git -C "$TARGET" checkout "$BRANCH"
    git -C "$TARGET" pull origin "$BRANCH"
else
    git clone -b "$BRANCH" "$REPO_URL" "$TARGET"
fi
cd "$TARGET"

if [ ! -f .env ]; then
    # скрипт часто запускают как `curl ... | bash`, поэтому спрашиваем через терминал,
    # а не через stdin — иначе read сожрёт остаток самого скрипта
    { exec 3</dev/tty; } 2>/dev/null || exec 3<&0
    echo "==> Настройка (спрошу один раз)"
    printf "Токен бота от @BotFather: "; read -r TG_TOKEN <&3
    printf "Твой chat_id: "; read -r TG_CHAT <&3
    printf "Ключ Anthropic (Enter — пропустить): "; read -r ANTH_KEY <&3
    printf "Профиль [AXIOM]: "; read -r PRESET <&3
    exec 3<&-
    PRESET=${PRESET:-AXIOM}
    if [ -z "${TG_TOKEN:-}" ] || [ -z "${TG_CHAT:-}" ]; then
        echo "Токен и chat_id обязательны — без них слать алерты некуда. Запусти скрипт ещё раз."
        rm -f .env
        exit 1
    fi
    cat > .env <<EOF
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHANNEL_ID=
TELEGRAM_CHAT_ID=${TG_CHAT}
ANTHROPIC_API_KEY=${ANTH_KEY}
FRESH_PRESET=${PRESET}
JUPITER_API_KEY=
EOF
    chmod 600 .env
fi

echo "==> Запускаю контейнер"
docker compose up -d --build

echo
echo "Готово. Бот работает и переживёт перезагрузку сервера."
echo "  логи:      docker compose -f $TARGET/docker-compose.yml logs -f"
echo "  рестарт:   docker compose -f $TARGET/docker-compose.yml restart"
echo "  обновить:  cd $TARGET && git pull && docker compose up -d --build"
