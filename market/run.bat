@echo off
chcp 65001 >nul
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установи Python 3.10+ с https://python.org (при установке отметь "Add to PATH") и запусти файл снова.
    pause
    exit /b 1
)

rem Внешних зависимостей нет — виртуальное окружение не нужно.
echo Собираю бриф...
python market_dashboard.py %*

echo.
echo Готово. Бриф сохранён в briefs\.
pause
