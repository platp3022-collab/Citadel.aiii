# Citadel.aiii — Мем-коин сканер

Telegram-бот: DexScreener + новостной анализ + алерты в канал/чат. Логика целиком в `memebot.py`.

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

Скопируй `.env.example` в `.env` и заполни:

```bash
cp .env.example .env
```

- `TELEGRAM_BOT_TOKEN` — токен от [@BotFather](https://t.me/BotFather)
- `TELEGRAM_CHANNEL_ID` — канал для алертов (бота нужно добавить туда админом с правом публикации)
- `TELEGRAM_CHAT_ID` — личный chat_id для команд (узнать командой `/id` в чате с ботом)
- `ANTHROPIC_API_KEY` — опционально, для LLM-анализа монет

`.env` не коммитится (см. `.gitignore`) — там живёт настоящий токен бота.

## Запуск

```bash
python memebot.py            # боевой режим
python memebot.py --once     # один скан и выход
python memebot.py --dry      # без отправки в Telegram, всё в консоль
```

## Команды бота

`/scan`, `/top [часы]`, `/stats [часы]`, `/status`, `/threshold [0-100]`, `/mute [минуты]`, `/unmute`, `/news`, `/id`, `/help`

Отказ от ответственности: инструмент фильтрации информации, не финансовый совет.
