@echo off
chcp 65001 >nul
title Citadel - панель управления
cd /d "%~dp0"

echo.
echo   CITADEL - панель управления
echo   ---------------------------
echo.

rem Сначала py -3: на Windows команда python часто ведёт в Microsoft Store и ничего не запускает
set "PY="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python --version >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto :nopython

if not exist ".venv\Scripts\python.exe" (
    echo   Готовлю окружение, это займёт минуту...
    %PY% -m venv .venv
    if errorlevel 1 goto :venvfail
)

set "VPY=%~dp0.venv\Scripts\python.exe"
echo   Проверяю зависимости...
"%VPY%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pipfail

echo   Запускаю. Сейчас откроется браузер.
echo   Это окно не закрывай - пока оно открыто, панель работает.
echo.
"%VPY%" webui.py
goto :done

:nopython
echo   Python не найден.
echo.
echo   1. Открой https://www.python.org/downloads/
echo   2. Скачай Python 3.11 или новее
echo   3. При установке ОБЯЗАТЕЛЬНО поставь галочку "Add python.exe to PATH"
echo   4. После установки запусти этот файл снова
goto :fail

:venvfail
echo   Не удалось создать окружение (.venv).
echo   Попробуй запустить файл от имени администратора или проверь место на диске.
goto :fail

:pipfail
echo   Не удалось поставить зависимости.
echo   Проверь интернет и запусти файл снова.
goto :fail

:fail
echo.
pause
exit /b 1

:done
echo.
echo   Панель остановлена.
pause
