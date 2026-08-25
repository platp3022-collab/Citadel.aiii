#!/usr/bin/env bash
# Двойной клик на macOS открывает .command в Терминале.
exec "$(dirname "$0")/run-web.sh" "$@"
