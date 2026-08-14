# Деплой на сервер (чтобы бот работал 24/7 сам по себе)

Нужен любой дешёвый VPS с Ubuntu (Hetzner, DigitalOcean, Timeweb, netcup — от ~3-5$/мес).
Твой домашний компьютер для этого не годится: как только он выключен или закрыт терминал — бот стоит.

## Способ 1: Docker (проще всего)

На сервере:

```bash
# 1. Поставь Docker, если его ещё нет
curl -fsSL https://get.docker.com | sh

# 2. Склонируй репозиторий
git clone -b claude/telegram-bot-2ygrxd https://github.com/platp3022-collab/Citadel.aiii.git
cd Citadel.aiii

# 3. Создай .env со своими значениями
cp .env.example .env
nano .env   # впиши TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, TELEGRAM_CHAT_ID

# 4. Собери и запусти — с автоперезапуском при падении/перезагрузке сервера
docker build -t memebot .
docker run -d --name memebot --restart unless-stopped \
    --env-file .env -v "$(pwd)/data:/app/data" memebot

# Проверить логи:
docker logs -f memebot

# Остановить:
docker stop memebot

# Обновить после изменений в коде:
git pull
docker build -t memebot .
docker rm -f memebot
docker run -d --name memebot --restart unless-stopped \
    --env-file .env -v "$(pwd)/data:/app/data" memebot
```

## Способ 2: systemd (без Docker)

```bash
# 1. Python и git
sudo apt update && sudo apt install -y python3 python3-venv git

# 2. Отдельный системный пользователь для бота (без прав логина)
sudo useradd -r -s /usr/sbin/nologin memebot

# 3. Код в /opt/memebot
sudo git clone -b claude/telegram-bot-2ygrxd https://github.com/platp3022-collab/Citadel.aiii.git /opt/memebot
cd /opt/memebot
sudo cp .env.example .env
sudo nano .env   # впиши TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, TELEGRAM_CHAT_ID

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
