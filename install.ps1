# Citadel — установка и запуск одной командой (Windows).
#
#   powershell -c "irm https://raw.githubusercontent.com/platp3022-collab/Citadel.aiii/claude/crypto-bot-auto-strategy-enawqd/install.ps1 | iex"
#
# Скачивает проект в %USERPROFILE%\Citadel, ставит зависимости и открывает панель.
# Повторный запуск обновляет код, не трогая ваши данные (data\ и .env).

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"     # иначе вывод бота через конвейер PowerShell рвётся на эмодзи
$env:PYTHONUTF8 = "1"

$Branch = "claude/crypto-bot-auto-strategy-enawqd"
$Zip    = "https://github.com/platp3022-collab/Citadel.aiii/archive/refs/heads/$Branch.zip"
$Home_  = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
$Target = Join-Path $Home_ "Citadel"

function Step($text) { Write-Host "  $text" -ForegroundColor Cyan }
function Bad($text)  { Write-Host ""; Write-Host "  $text" -ForegroundColor Yellow; Write-Host "" }

Write-Host ""
Write-Host "  CITADEL — торговый стенд" -ForegroundColor Green
Write-Host "  ------------------------"
Write-Host ""

# ── 1. Python ───────────────────────────────────────────────────────────────
$py = $null
foreach ($candidate in @(@("py", "-3"), @("python"))) {
    try {
        $exe = $candidate[0]
        $args = @($candidate[1..($candidate.Length - 1)]) + @("-c", "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)")
        & $exe @args 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $candidate; break }
    } catch { }
}
if (-not $py) {
    Bad "Python 3.11 или новее не найден."
    Write-Host "  1. Открой https://www.python.org/downloads/"
    Write-Host "  2. Скачай и установи Python 3.11+"
    Write-Host "  3. ВАЖНО: поставь галочку 'Add python.exe to PATH'"
    Write-Host "  4. Закрой это окно, открой новое и запусти команду снова"
    Write-Host ""
    return
}

# ── 2. Скачивание ───────────────────────────────────────────────────────────
Step "Скачиваю проект…"
$tmpZip = Join-Path $env:TEMP "citadel.zip"
$tmpDir = Join-Path $env:TEMP "citadel-unzip"
if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
try {
    Invoke-WebRequest -Uri $Zip -OutFile $tmpZip -UseBasicParsing
} catch {
    Bad "Не удалось скачать проект. Проверь интернет и попробуй снова."
    return
}
Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force
$src = (Get-ChildItem $tmpDir -Directory | Select-Object -First 1).FullName

Step "Кладу в $Target"
New-Item -ItemType Directory -Path $Target -Force | Out-Null
# данные и настройки пользователя не трогаем
Get-ChildItem $src -Force | ForEach-Object {
    if ($_.Name -in @("data", ".env")) { return }
    Copy-Item $_.FullName -Destination $Target -Recurse -Force
}
Remove-Item $tmpZip, $tmpDir -Recurse -Force -ErrorAction SilentlyContinue

# ── 3. Окружение ────────────────────────────────────────────────────────────
Set-Location $Target
$venvPy = Join-Path $Target ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Step "Готовлю окружение, это займёт минуту…"
    $exe = $py[0]
    $rest = @($py[1..($py.Length - 1)]) + @("-m", "venv", ".venv")
    & $exe @rest
    if (-not (Test-Path $venvPy)) { Bad "Не удалось создать окружение (.venv)."; return }
}

Step "Проверяю зависимости…"
& $venvPy -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { Bad "Не удалось поставить зависимости. Проверь интернет."; return }

# ── 4. Запуск ───────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  Готово. Открываю панель в браузере." -ForegroundColor Green
Write-Host "  Это окно не закрывай — пока оно открыто, панель работает."
Write-Host "  В следующий раз: двойной клик по $Target\run-web.bat"
Write-Host ""
& $venvPy webui.py
