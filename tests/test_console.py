# -*- coding: utf-8 -*-
"""Вывод в консоль не должен ронять бота на Windows."""
from __future__ import annotations

import io
import sys
import unittest

from citadel import console
from citadel.notify import Notifier


class NarrowConsole(io.TextIOWrapper):
    """Консоль в cp1251 — ровно то, что стоит на Windows по умолчанию."""

    def __init__(self, encoding: str = "cp1251"):
        super().__init__(io.BytesIO(), encoding=encoding, errors="strict",
                         write_through=True)

    def value(self) -> str:
        self.flush()
        return self.buffer.getvalue().decode(self.encoding, "replace")


class TestConsole(unittest.TestCase):
    def setUp(self):
        self.real = sys.stdout

    def tearDown(self):
        sys.stdout = self.real

    def test_plain_print_of_emoji_would_crash(self):
        """Проверка предпосылки: обычный print на такой консоли падает."""
        sys.stdout = NarrowConsole()
        with self.assertRaises(UnicodeEncodeError):
            print("🧠 новая стратегия")

    def test_write_survives_emoji(self):
        sys.stdout = out = NarrowConsole()
        console.write("🧠 новая стратегия #4")
        text = out.value()
        sys.stdout = self.real
        self.assertIn("новая стратегия #4", text)
        self.assertIn("[*]", text)                 # эмодзи заменилась, но строка дошла

    def test_write_survives_ascii_only_console(self):
        sys.stdout = out = NarrowConsole(encoding="ascii")
        console.write("🟢 Покупка BTC/USDT по 152,63")
        text = out.value()
        sys.stdout = self.real
        self.assertIn("BTC/USDT", text)

    def test_html_tags_are_stripped(self):
        sys.stdout = out = NarrowConsole("utf-8")
        console.write("<b>Покупка BTC/USDT</b>\n<pre>ВХОД: RSI14 &gt; 50</pre>")
        text = out.value()
        sys.stdout = self.real
        self.assertNotIn("<b>", text)
        self.assertNotIn("<pre>", text)
        self.assertIn("Покупка BTC/USDT", text)

    def test_notifier_does_not_crash_the_bot(self):
        sys.stdout = out = NarrowConsole()
        Notifier(enabled=False).send("🧠 <b>BTC/USDT: новая стратегия #7</b>\nстоп 2.5×ATR")
        text = out.value()
        sys.stdout = self.real
        self.assertIn("новая стратегия #7", text)
        self.assertNotIn("<b>", text)

    def test_every_emoji_used_by_the_bot_has_a_fallback(self):
        import pathlib
        import re

        emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")
        used = set()
        for path in pathlib.Path("citadel").rglob("*.py"):
            for match in emoji.finditer(path.read_text(encoding="utf-8")):
                used.add(match.group())
        missing = {ch for ch in used
                   if ch not in "".join(console.FALLBACK) and ch != "️"}
        self.assertEqual(missing, set(), f"нет замены для: {missing}")

    def test_setup_is_safe_to_call_twice(self):
        console.setup()
        console.setup()
        self.assertEqual(sys.stdout, self.real)


if __name__ == "__main__":
    unittest.main()
