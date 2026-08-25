#!/usr/bin/env bash
# Citadel — установка и запуск одной командой (macOS / Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/platp3022-collab/Citadel.aiii/claude/crypto-bot-auto-strategy-enawqd/install.sh | bash
#
# Скачивает проект в ~/Citadel, ставит зависимости и открывает панель.
# Повторный запуск обновляет код, не трогая ваши данные (data/ и .env).
set -u

BRANCH="claude/crypto-bot-auto-strategy-enawqd"
ZIP="https://github.com/platp3022-collab/Citadel.aiii/archive/refs/heads/${BRANCH}.zip"
TARGET="${CITADEL_DIR:-$HOME/Citadel}"

step() { printf '  %s\n' "$1"; }
bad()  { printf '\n  %s\n\n' "$1"; exit 1; }

printf '\n  CITADEL — торговый стенд\n  ------------------------\n\n'

# ── 1. Python ───────────────────────────────────────────────────────────────
PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null || continue
    PY="$candidate"; break
done
[ -n "$PY" ] || bad "Нужен Python 3.11 или новее.
  macOS:  brew install python@3.12
  Ubuntu: sudo apt install python3 python3-venv"

# ── 2. Скачивание ───────────────────────────────────────────────────────────
step "Скачиваю проект…"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$ZIP" -o "$TMP/citadel.zip" || bad "Не удалось скачать проект. Проверь интернет."
if command -v unzip >/dev/null 2>&1; then
    unzip -q "$TMP/citadel.zip" -d "$TMP/unpack"
else
    "$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
        "$TMP/citadel.zip" "$TMP/unpack" || bad "Не удалось распаковать архив."
fi
SRC="$(find "$TMP/unpack" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -n "$SRC" ] || bad "Архив пуст — попробуй ещё раз."

step "Кладу в $TARGET"
mkdir -p "$TARGET"
# данные и настройки пользователя не трогаем
(cd "$SRC" && for item in * .[!.]*; do
    [ -e "$item" ] || continue
    case "$item" in data|.env) continue ;; esac
    cp -R "$item" "$TARGET/"
done)

# ── 3. Окружение ────────────────────────────────────────────────────────────
cd "$TARGET" || bad "Не могу перейти в $TARGET"
if [ ! -x .venv/bin/python ]; then
    step "Готовлю окружение, это займёт минуту…"
    "$PY" -m venv .venv || bad "Не удалось создать окружение (.venv).
  На Ubuntu может не хватать пакета: sudo apt install python3-venv"
fi

step "Проверяю зависимости…"
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
    || bad "Не удалось поставить зависимости. Проверь интернет."

# ── 4. Запуск ───────────────────────────────────────────────────────────────
printf '\n  Готово. Открываю панель в браузере.\n'
printf '  Это окно не закрывай — пока оно открыто, панель работает.\n'
printf '  В следующий раз: %s/run-web.sh\n\n' "$TARGET"
# при запуске через curl | bash стандартный ввод занят скриптом — берём терминал
if { : </dev/tty; } 2>/dev/null; then          # терминал есть и его правда можно открыть
    exec .venv/bin/python webui.py "$@" </dev/tty
else
    exec .venv/bin/python webui.py "$@"
fi
