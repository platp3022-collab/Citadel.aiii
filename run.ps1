#Requires -Version 5.1
<#
    Запуск мем-коин сканера из PowerShell.

        powershell -ExecutionPolicy Bypass -File .\run.ps1

    Скрипт сам готовит окружение, при первом запуске спрашивает настройки
    и держит бота живым: упал из-за сети — поднимет обратно.
#>

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-EnvFile([string]$Path, [string[]]$Lines) {
    # без BOM — иначе первая переменная приедет с мусорным префиксом
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Lines -join "`r`n") + "`r`n", $utf8NoBom)
}

# ---- Python ----
$python = $null
foreach ($name in @("python", "python3", "py")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) {
    Write-Host "Python не найден." -ForegroundColor Red
    Write-Host "Поставь Python 3.11+ с https://python.org и отметь галочку 'Add python.exe to PATH'."
    Read-Host "Enter — выход" | Out-Null
    exit 1
}

# ---- окружение ----
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Готовлю окружение, это займёт минуту..." -ForegroundColor Cyan
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "Не удалось создать venv." -ForegroundColor Red; exit 1 }
}
& $venvPython -m pip install --quiet --upgrade pip 2>$null
& $venvPython -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "Не удалось поставить зависимости. Проверь интернет." -ForegroundColor Red
    Read-Host "Enter — выход" | Out-Null
    exit 1
}

# ---- настройки ----
if (-not (Test-Path ".env")) {
    Write-Host ""
    Write-Host "Первый запуск — настроим бота. Четыре вопроса, дальше он всё делает сам." -ForegroundColor Cyan
    Write-Host ""
    $tgToken = Read-Host "Токен бота от @BotFather"
    $tgChat  = Read-Host "Твой chat_id (узнать: напиши боту /id)"
    $anthKey = Read-Host "Ключ Anthropic (Enter — пропустить)"
    $preset  = Read-Host "Профиль axiom/axiom_strict/fomo/safe/degen [AXIOM]"
    if ([string]::IsNullOrWhiteSpace($preset)) { $preset = "AXIOM" }
    if ([string]::IsNullOrWhiteSpace($tgToken) -or [string]::IsNullOrWhiteSpace($tgChat)) {
        Write-Host "Токен и chat_id обязательны — без них слать алерты некуда." -ForegroundColor Red
        Read-Host "Enter — выход" | Out-Null
        exit 1
    }
    Write-EnvFile (Join-Path $PSScriptRoot ".env") @(
        "TELEGRAM_BOT_TOKEN=$tgToken",
        "TELEGRAM_CHANNEL_ID=",
        "TELEGRAM_CHAT_ID=$tgChat",
        "ANTHROPIC_API_KEY=$anthKey",
        "FRESH_PRESET=$preset",
        "JUPITER_API_KEY="
    )
    Write-Host "Настройки сохранены в .env — больше спрашивать не буду." -ForegroundColor Green
    Write-Host ""
}

# ---- запуск с автоперезапуском ----
Write-Host "Бот запущен. Не закрывай это окно — пока оно открыто, он сканирует рынок." -ForegroundColor Green
Write-Host "Упадёт из-за сети — подниму сам через 10 секунд. Остановить: Ctrl+C."
Write-Host ""

while ($true) {
    & $venvPython memebot.py
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Write-Host "Бот остановлен."
        break
    }
    Write-Host "Бот упал (код $code). Перезапуск через 10 секунд..." -ForegroundColor Yellow
    Start-Sleep -Seconds 10
}
