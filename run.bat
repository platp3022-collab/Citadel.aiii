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

if not exist .env (
    echo Первый запуск — открываю мастер настройки.
    python setup.py
    pause
    exit /b 0
)

echo Запускаю бота. Не закрывай это окно — пока оно открыто, бот сканирует и шлёт алерты.
python memebot.py
pause
