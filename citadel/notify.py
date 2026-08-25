# -*- coding: utf-8 -*-
"""Уведомления в Telegram (stdlib, без зависимостей). Молча выключены, если нет токена."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from . import console

log = logging.getLogger("citadel.notify")


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", enabled: bool = True,
                 echo: bool = True):
        self.token, self.chat_id = token, chat_id
        self.enabled = bool(enabled and token and chat_id)
        self.echo = echo                    # дублировать ли сообщения в консоль

    def send(self, text: str) -> bool:
        if self.echo:
            console.write(text)      # без html-тегов и без падения на эмодзи
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id, "text": text[:4000],
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
                return json.loads(r.read().decode()).get("ok", False)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("Telegram недоступен: %s", e)
            return False
