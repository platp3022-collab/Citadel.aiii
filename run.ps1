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
function Clear-InputBuffer {
    # после `iex (iwr ...)` в буфере остаётся перевод строки, и первый Read-Host
    # съедает его вместо ответа — вычищаем, иначе первый вопрос уйдёт пустым
    try {
        while ([Console]::KeyAvailable) { [Console]::ReadKey($true) | Out-Null }
    } catch { }
}

function Read-Required([string]$Prompt) {
    # спрашиваем, пока не ответят: пустой токен молча ломает всё дальше
    while ($true) {
        Clear-InputBuffer
        $value = Read-Host $Prompt
        if (-not [string]::IsNullOrWhiteSpace($value)) { return $value.Trim() }
        Write-Host "Это поле обязательно — без него бот не сможет слать алерты." -ForegroundColor Yellow
    }
}

if (-not (Test-Path ".env")) {
    Write-Host ""
    Write-Host "Первый запуск — настроим бота. Четыре вопроса, дальше он всё делает сам." -ForegroundColor Cyan
    Write-Host ""
    $tgToken = Read-Required "Токен бота от @BotFather"
    $tgChat  = Read-Required "Твой chat_id (узнать: напиши боту /id)"
    Clear-InputBuffer
    $anthKey = Read-Host "Ключ Anthropic (Enter — пропустить)"
    Clear-InputBuffer
    $preset  = Read-Host "Профиль axiom/axiom_strict/fomo/safe/degen [AXIOM]"
    if ([string]::IsNullOrWhiteSpace($preset)) { $preset = "AXIOM" }
    $anthKey = "$anthKey".Trim()
    $preset  = "$preset".Trim()
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

# ---- cloudflared: без него статистику не открыть внутри Telegram ----
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Write-Host "Ставлю cloudflared — через него статистика открывается в Telegram..." -ForegroundColor Cyan
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Cloudflare.cloudflared --silent --accept-source-agreements --accept-package-agreements 2>$null | Out-Null
        # winget не обновляет PATH в текущем окне — подхватываем вручную
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path", "User")
    }
    if (Get-Command cloudflared -ErrorAction SilentlyContinue) {
        Write-Host "cloudflared готов." -ForegroundColor Green
    } else {
        Write-Host "Не поставился — статистика будет только на этом компьютере." -ForegroundColor Yellow
        Write-Host "Поставить вручную: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    }
    Write-Host ""
}

# ---- не давать компьютеру уснуть, пока бот работает ----
# Ставим флаг только на время работы этого окна: системные настройки питания
# не трогаем, закрыл окно — всё вернулось как было.
try {
    Add-Type -Namespace Power -Name Sleep -MemberDefinition @"
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
"@ -ErrorAction Stop
    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
    [Power.Sleep]::SetThreadExecutionState([uint32]"0x80000041") | Out-Null
    Write-Host "Сон отключён на время работы бота (экран гаснуть может)." -ForegroundColor DarkGray
} catch {
    Write-Host "Не смог запретить сон — проверь настройки питания вручную." -ForegroundColor Yellow
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

# возвращаем обычное поведение сна
try { [Power.Sleep]::SetThreadExecutionState([uint32]"0x80000000") | Out-Null } catch { }

