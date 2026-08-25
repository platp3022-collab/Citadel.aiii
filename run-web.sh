#!/usr/bin/env bash
# Запуск панели управления в один клик. Mac/Linux.
cd "$(dirname "$0")" || exit 1

echo
echo "  CITADEL — панель управления"
echo "  ---------------------------"
echo

fail() {
    echo
    echo "  $1"
    echo
    read -r -p "  Нажми Enter, чтобы закрыть..." _
    exit 1
}

PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done
[ -n "$PY" ] || fail "Нужен Python 3.11 или новее. Mac: brew install python@3.12, Ubuntu: sudo apt install python3"

if [ ! -x .venv/bin/python ]; then
    echo "  Готовлю окружение, это займёт минуту..."
    "$PY" -m venv .venv || fail "Не удалось создать окружение (.venv). На Ubuntu может не хватать пакета python3-venv."
fi

echo "  Проверяю зависимости..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
    || fail "Не удалось поставить зависимости. Проверь интернет и запусти снова."

echo "  Запускаю. Сейчас откроется браузер."
echo "  Это окно не закрывай — пока оно открыто, панель работает."
echo
.venv/bin/python webui.py "$@"
