# Деплой на сервер (чтобы бот работал 24/7 сам по себе)

Нужен любой дешёвый VPS с Ubuntu (Hetzner, DigitalOcean, Timeweb, netcup — от ~3-5$/мес).
Твой домашний компьютер для этого не годится: как только он выключен или закрыт терминал — бот стоит.

## Способ 0: одна команда (всё сделает скрипт)

На чистом Ubuntu/Debian сервере, под root:

```bash
curl -fsSL https://raw.githubusercontent.com/platp3022-collab/Citadel.aiii/claude/meme-coin-analyzer-bot-3qqsiy/deploy/setup_vps.sh | bash
```

Скрипт сам поставит Docker, заберёт код, спросит токен, chat_id, ключ и профиль,
запустит контейнер с `restart: always` — он переживёт и падение, и перезагрузку сервера.

Дальше:

```bash
cd /opt/memebot
docker compose logs -f      # смотреть логи
docker compose restart      # перезапустить
git pull && docker compose up -d --build   # обновить
```

## Способ 1: Docker (проще всего)

На сервере:

```bash
# 1. Поставь Docker, если его ещё нет
curl -fsSL https://get.docker.com | sh

# 2. Склонируй репозиторий
git clone -b claude/meme-coin-analyzer-bot-3qqsiy https://github.com/platp3022-collab/Citadel.aiii.git
cd Citadel.aiii

# 3. Создай .env со своими значениями
cp .env.example .env
nano .env   # впиши TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY, FRESH_PRESET

# 4. Собери и запусти — с автоперезапуском при падении/перезагрузке сервера
docker compose up -d --build

# Проверить логи:
docker compose logs -f

# Остановить:
docker compose down

# Обновить после изменений в коде:
git pull && docker compose up -d --build
```

## Способ 2: systemd (без Docker)

```bash
# 1. Python и git
sudo apt update && sudo apt install -y python3 python3-venv git

# 2. Отдельный системный пользователь для бота (без прав логина)
sudo useradd -r -s /usr/sbin/nologin memebot

# 3. Код в /opt/memebot
sudo git clone -b claude/meme-coin-analyzer-bot-3qqsiy https://github.com/platp3022-collab/Citadel.aiii.git /opt/memebot
cd /opt/memebot
sudo cp .env.example .env
sudo nano .env   # впиши TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY, FRESH_PRESET

# 4. Виртуальное окружение и зависимости
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

# 5. Права
sudo chown -R memebot:memebot /opt/memebot

# 6. Сервис systemd
sudo cp deploy/memebot.service /etc/systemd/system/memebot.service
sudo systemctl daemon-reload
sudo systemctl enable --now memebot

# Проверить статус и логи:
sudo systemctl status memebot
sudo journalctl -u memebot -f

# Обновить после изменений в коде:
cd /opt/memebot
sudo git pull
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart memebot
```

После любого из способов бот работает постоянно на сервере: сканирует рынок и шлёт тебе алерты в Telegram
независимо от того, где ты сам и включён ли твой компьютер/телефон — главное, чтобы у тебя было включено
приложение Telegram (или push-уведомления в нём).
