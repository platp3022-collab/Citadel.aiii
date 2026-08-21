@echo off
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установи Python 3.11+ с https://python.org (при установке отметь "Add to PATH") и запусти файл снова.
    pause
    exit /b 1
)

if not exist .venv (
    echo Готовлю окружение, подожди...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

if not exist .env copy .env.example .env >nul

findstr /R "^TELEGRAM_BOT_TOKEN=..*" .env >nul
if errorlevel 1 goto needenv
findstr /R "^TELEGRAM_CHAT_ID=..*" .env >nul
if errorlevel 1 goto needenv

echo Запускаю Telegram-бота в бумажном режиме. Реальные деньги не тратятся.
echo Напиши боту /panel. Не закрывай это окно - выход: Ctrl+C.
python tgapp.py
pause
exit /b 0

:needenv
echo Открой файл .env и заполни две строки:
echo   TELEGRAM_BOT_TOKEN - токен от @BotFather
echo   TELEGRAM_CHAT_ID   - твой chat_id, узнать у @userinfobot
pause
exit /b 1
