@echo off
rem Запуск Полки. Двойной клик по файлу.
cd /d "%~dp0"

if not exist .env (
  echo Нет файла .env. Скопируй .env.example в .env и заполни.
  pause
  exit /b 1
)

python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo Не поставились зависимости. Нужен Python 3.
  pause
  exit /b 1
)

echo Полка запущена. Не закрывай это окно.
python -m polka
pause
