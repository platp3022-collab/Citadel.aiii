# -*- coding: utf-8 -*-
"""
Вывод в консоль, который не падает на Windows.

Классическая беда: консоль Windows работает в кодировке вроде cp1251, а бот
печатает эмодзи и рамки. Обычный print на таком символе роняет весь процесс с
UnicodeEncodeError — прямо посреди торговли.

Поэтому при старте вывод переключается на UTF-8, а печать всё равно идёт через
защищённую функцию: если терминал совсем древний, символ заменится, но процесс
продолжит работать.
"""
from __future__ import annotations

import os
import re
import sys

#: чем заменяем эмодзи, если консоль их не тянет
FALLBACK = {
    "🟢": "[+]", "🔴": "[-]", "✅": "[ok]", "⚠️": "[!]", "⛔️": "[stop]", "🧠": "[*]",
    "🔍": "[?]", "🚀": "[>]", "📊": "[#]", "📈": "[^]", "🔗": "[tx]", "🔎": "[?]",
    "🟡": "[~]", "❌": "[x]", "▲": "^", "▼": "v", "·": "-", "—": "-", "…": "...",
    "═": "=", "─": "-", "│": "|", "×": "x", "≥": ">=", "≤": "<=", "≫": ">>", "→": "->",
}

_TAGS = re.compile(r"<[^>]+>")


def setup() -> None:
    """Переводит стандартный вывод на UTF-8. Вызывается один раз при старте."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass                      # поток подменён или не поддерживает — печать всё равно защищена


def plain(text: str) -> str:
    """Убирает html-разметку: в консоли она только мешает."""
    return _TAGS.sub("", text)


def downgrade(text: str) -> str:
    """Заменяет символы, которых может не быть в кодировке консоли."""
    for bad, good in FALLBACK.items():
        text = text.replace(bad, good)
    return text


def write(text: str, strip_tags: bool = True) -> None:
    """Печатает, что бы ни случилось: сначала как есть, потом упрощая."""
    if strip_tags:
        text = plain(text)
    try:
        print(text, flush=True)
        return
    except UnicodeEncodeError:
        pass
    try:
        print(downgrade(text), flush=True)
        return
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"), flush=True)
