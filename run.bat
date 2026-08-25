@echo off
chcp 65001 >nul
cd /d %~dp0

rem Один клик: окружение, настройка при первом запуске, автоперезапуск при падении.

where python >nul 2>nul
if errorlevel 1 goto nopython

if exist .venv goto venvready
echo Готовлю окружение, это займёт минуту...
python -m venv .venv

:venvready
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip >nul 2>nul
pip install -q -r requirements.txt

if exist .env goto start

echo.
echo Первый запуск — настроим бота. Четыре вопроса, дальше он всё делает сам.
echo.
set /p TG_TOKEN="Токен бота от @BotFather: "
set /p TG_CHAT="Твой chat_id (узнать: напиши боту /id): "
set /p ANTH_KEY="Ключ Anthropic (Enter — пропустить): "
set /p PRESET="Профиль axiom/axiom_strict/fomo/safe/degen [AXIOM]: "
if "%PRESET%"=="" set PRESET=AXIOM
> .env echo TELEGRAM_BOT_TOKEN=%TG_TOKEN%
>> .env echo TELEGRAM_CHANNEL_ID=
>> .env echo TELEGRAM_CHAT_ID=%TG_CHAT%
>> .env echo ANTHROPIC_API_KEY=%ANTH_KEY%
>> .env echo FRESH_PRESET=%PRESET%
>> .env echo JUPITER_API_KEY=
echo Настройки сохранены в .env — больше спрашивать не буду.
echo.

:start
echo Бот запущен. Не закрывай это окно — пока оно открыто, он сканирует рынок.
echo Упадёт из-за сети — подниму сам через 10 секунд. Остановить: Ctrl+C.
echo.

:loop
python memebot.py
if errorlevel 1 goto crashed
echo Бот остановлен.
pause
exit /b 0

:crashed
echo Бот упал. Перезапуск через 10 секунд...
timeout /t 10 /nobreak >nul
goto loop

:nopython
echo Python не найден. Установи Python 3.11+ с https://python.org
echo При установке обязательно отметь галочку "Add python.exe to PATH".
pause
exit /b 1
