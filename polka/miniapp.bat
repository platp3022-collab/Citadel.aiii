@echo off
rem Поднять Полку и подключить мини-приложение к боту. Двойной клик по файлу.
cd /d "%~dp0"

if not exist .env (
  echo Нет файла .env. Скопируй .env.example в .env и заполни токен и chat id.
  pause & exit /b 1
)

python -m pip install --quiet -r requirements.txt
if errorlevel 1 ( echo Не поставились зависимости. Нужен Python 3. & pause & exit /b 1 )

if not exist static\index.html (
  echo Нет собранного интерфейса. Поставь Node.js, затем в папке miniapp: npm install ^&^& npm run build
  pause & exit /b 1
)

where cloudflared >nul 2>&1
if errorlevel 1 (
  echo Нужен cloudflared: скачай cloudflared-windows-amd64.exe со страницы релизов
  echo https://github.com/cloudflare/cloudflared/releases/latest
  echo Переименуй в cloudflared.exe и положи рядом с этим файлом.
  pause & exit /b 1
)
set PATH=%CD%;%PATH%

echo Запускаю Полку...
start "Полка" /min python -m polka
timeout /t 3 >nul

python -m polka.setup --tunnel
pause
