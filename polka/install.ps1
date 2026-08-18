# Полка: установка и запуск одной командой.
#
#   $f="$env:TEMP\polka.ps1"; iwr -useb https://raw.githubusercontent.com/platp3022-collab/Citadel.aiii/refs/heads/claude/hello-pmjyoy/polka/install.ps1 -OutFile $f; powershell -ExecutionPolicy Bypass -File $f
#
# Скачивание в файл, а не выполнение из потока: при выполнении из потока
# PowerShell портит кириллицу, а из файла с меткой UTF-8 читает верно.
#
# Скачивает проект в %USERPROFILE%\Polka, ставит зависимости, спрашивает ключи,
# поднимает временный адрес и вешает кнопку мини-приложения боту.

$ErrorActionPreference = 'Stop'

# Без этого консоль печатает кириллицу вопросительными знаками.
try {
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    [Console]::InputEncoding  = [Text.Encoding]::UTF8
    $OutputEncoding = [Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

$Repo   = 'platp3022-collab/Citadel.aiii'
$Branch = 'claude/hello-pmjyoy'
$Home_  = [Environment]::GetFolderPath('UserProfile')
$Dest   = Join-Path $Home_ 'Polka'
$App    = Join-Path $Dest 'polka'

function Step($text) { Write-Host "`n>> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`n!! $text" -ForegroundColor Red; Read-Host 'Enter чтобы закрыть'; exit 1 }

# ── Python ───────────────────────────────────────────────────────────────
$Py = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $Py = $candidate; break }
}
if (-not $Py) {
    Fail "Не нашёл Python. Поставь его с python.org и обязательно отметь галочку 'Add python.exe to PATH', потом запусти эту команду снова."
}
Step "Python: $((& $Py --version) 2>&1)"

# ── скачивание ───────────────────────────────────────────────────────────
Step 'Качаю Полку с гитхаба'
$Zip = Join-Path $env:TEMP 'polka-download.zip'
$Unpacked = Join-Path $env:TEMP 'polka-unpacked'
if (Test-Path $Unpacked) { Remove-Item $Unpacked -Recurse -Force }

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -UseBasicParsing "https://codeload.github.com/$Repo/zip/refs/heads/$Branch" -OutFile $Zip
Expand-Archive -Path $Zip -DestinationPath $Unpacked -Force
Remove-Item $Zip -Force

$Root = Get-ChildItem $Unpacked -Directory | Select-Object -First 1
if (-not $Root) { Fail 'Архив пришёл пустой. Проверь интернет и повтори.' }

# Обновляемся поверх, ничего не удаляя. Папку нельзя стереть, если в ней стоит
# консоль или лежит запущенный процесс, а настройки и записанные мысли внутри.
$EnvPath = Join-Path $App '.env'

if (Test-Path $Dest) {
    Step 'Нашёл прошлую установку, обновляю поверх'

    # Уходим из папки, иначе любые операции в ней спотыкаются о текущий каталог.
    Set-Location $Home_

    # Гасим то, что осталось от прошлого запуска: иначе файлы заняты.
    Get-Process -Name 'cloudflared' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($Dest) } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'polka' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1

    # Старую сборку интерфейса чистим: имена файлов в ней завязаны на хеш.
    $OldAssets = Join-Path $App 'static\assets'
    if (Test-Path $OldAssets) { Remove-Item $OldAssets -Recurse -Force -ErrorAction SilentlyContinue }

    Copy-Item (Join-Path $Root.FullName '*') -Destination $Dest -Recurse -Force
} else {
    Move-Item $Root.FullName $Dest
}
Remove-Item $Unpacked -Recurse -Force -ErrorAction SilentlyContinue
Step "Положил сюда: $Dest"

Set-Location $App

# ── зависимости ──────────────────────────────────────────────────────────
Step 'Ставлю зависимости'
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail 'Не поставились зависимости. Пришли текст ошибки выше.' }

# ── настройка ────────────────────────────────────────────────────────────
$needsSetup = $true
if (Test-Path $EnvPath) {
    if (Select-String -Path $EnvPath -Pattern '^TELEGRAM_BOT_TOKEN=.' -Quiet) { $needsSetup = $false }
}
if ($needsSetup) {
    Step 'Настройка: три вопроса'
    & $Py -m polka.wizard
    if ($LASTEXITCODE -ne 0) { Fail 'Настройка не завершилась.' }
} else {
    Step 'Настройки уже есть, пропускаю вопросы'
}

# ── cloudflared: временный https-адрес ───────────────────────────────────
$Cf = Join-Path $App 'cloudflared.exe'
if (-not (Test-Path $Cf)) {
    Step 'Качаю cloudflared, он выдаёт временный адрес для мини-приложения'
    $arch = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { '386' }
    Invoke-WebRequest -UseBasicParsing `
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-$arch.exe" `
        -OutFile $Cf
}
$env:PATH = "$App;$env:PATH"

# ── запуск ───────────────────────────────────────────────────────────────
Step 'Запускаю Полку'
$polka = Start-Process -FilePath $Py -ArgumentList '-m', 'polka' -WorkingDirectory $App -PassThru -WindowStyle Minimized
Start-Sleep -Seconds 4
if ($polka.HasExited) { Fail 'Полка не поднялась. Запусти вручную: python -m polka' }

Step 'Поднимаю адрес и вешаю кнопку боту'
& $Py -m polka.setup --tunnel
$tunnelResult = $LASTEXITCODE

if (-not $polka.HasExited) { Stop-Process -Id $polka.Id -Force -ErrorAction SilentlyContinue }

if ($tunnelResult -eq 2) {
    # Сеть не пускает к сервису туннелей. Кнопки в Telegram не будет, но всё
    # остальное работает, поэтому просто запускаем Полку и показываем адрес.
    Write-Host "`nЗапускаю Полку без мини-приложения. Бот и напоминания работают." -ForegroundColor Yellow
    Write-Host "Интерфейс открой в браузере по ссылке ниже.`n" -ForegroundColor Yellow
    & $Py -m polka
}

Read-Host 'Enter чтобы закрыть'
