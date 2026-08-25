#Requires -Version 5.1
<#
    Ставит и запускает мем-коин сканер одной строкой:

        iex (iwr -useb "https://raw.githubusercontent.com/platp3022-collab/Citadel.aiii/claude/meme-coin-analyzer-bot-3qqsiy/deploy/bootstrap.ps1").Content

    Качает код в %USERPROFILE%\memebot, ставит зависимости и передаёт
    управление run.ps1. Повторный запуск обновляет код: .env, база и venv
    остаются на месте, бот просто перезапускается на свежей версии.
#>

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$repo   = "platp3022-collab/Citadel.aiii"
$branch = "claude/meme-coin-analyzer-bot-3qqsiy"
$target = Join-Path $HOME "memebot"
$zipUrl = "https://github.com/$repo/archive/refs/heads/$branch.zip"
$tmpZip = Join-Path $env:TEMP "memebot.zip"
$tmpDir = Join-Path $env:TEMP "memebot_unpack"

Write-Host "==> Качаю код" -ForegroundColor Cyan
try {
    Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $tmpZip
} catch {
    Write-Host "Не удалось скачать архив: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Если репозиторий приватный — скачай ZIP через браузер:" -ForegroundColor Yellow
    Write-Host "  $zipUrl"
    Write-Host "распакуй в $target и запусти там:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File .\run.ps1"
    exit 1
}

Write-Host "==> Распаковываю в $target" -ForegroundColor Cyan
if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
Expand-Archive -LiteralPath $tmpZip -DestinationPath $tmpDir -Force
$src = Get-ChildItem -LiteralPath $tmpDir -Directory | Select-Object -First 1
if (-not $src) {
    Write-Host "В архиве пусто — что-то пошло не так." -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Path $target -Force | Out-Null
# robocopy аккуратно обновляет код и не трогает настройки, базу и окружение
$null = robocopy $src.FullName $target /E /NFL /NDL /NJH /NJS /NC /NS `
        /XF .env memebot.db /XD data .venv .git
if ($LASTEXITCODE -ge 8) {
    Write-Host "Не удалось скопировать файлы (robocopy $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}
$global:LASTEXITCODE = 0
Remove-Item $tmpZip, $tmpDir -Recurse -Force -ErrorAction SilentlyContinue

Set-Location -LiteralPath $target
Write-Host "==> Запускаю" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $target "run.ps1")
