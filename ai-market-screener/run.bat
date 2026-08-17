@echo off
REM AI Market Screener - launcher for Windows. Double-click this file.
setlocal
cd /d "%~dp0"
title AI Market Screener

where py >nul 2>&1 && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Python not found.
  echo   Install it from https://python.org/downloads
  echo   IMPORTANT: tick "Add Python to PATH" in the installer.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo   First run: setting up. This takes a couple of minutes, once.
  %PY% -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (
    echo   Dependency install failed - check your internet connection.
    pause & exit /b 1
  )
  echo   Ready.
)

".venv\Scripts\python.exe" webapp.py
echo.
echo   Stopped. You can close this window.
pause
