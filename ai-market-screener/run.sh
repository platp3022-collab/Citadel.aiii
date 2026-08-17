#!/usr/bin/env bash
# AI Market Screener - launcher for macOS and Linux. Double-click, or ./run.sh
set -e
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || {
  echo "  Python 3 not found. Install it from https://python.org/downloads"
  read -rp "  Press Enter to close." _
  exit 1
}

if [ ! -x ".venv/bin/python" ]; then
  echo "  First run: setting up. This takes a couple of minutes, once."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  echo "  Ready."
fi

.venv/bin/python webapp.py
