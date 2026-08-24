@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установи Python 3.11+ с python.org и поставь галочку "Add to PATH".
    pause
    exit /b 1
)

if not exist .venv (
    echo Готовлю окружение, подожди...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

echo 1) качаю историю...
python tradebot.py fetch
echo 2) ищу стратегию (это займёт минуту)...
python tradebot.py evolve
echo 3) торгую на бумажном счёте. Не закрывай это окно.
python tradebot.py trade
pause
