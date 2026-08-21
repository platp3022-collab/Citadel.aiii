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
    echo .env не найден - копирую из .env.example, дефолтов хватит для бумажного режима.
    copy .env.example .env >nul
)

echo Запускаю бота в бумажном режиме. Реальные деньги не тратятся.
echo Не закрывай это окно - закроешь, бот остановится. Выход: Ctrl+C.
python polybot.py
pause
