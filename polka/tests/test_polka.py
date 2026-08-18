"""Тесты Полки: разбор карточки, эскалация напоминаний, тихие часы, подпись WebApp."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polka.api import check_init_data
from polka.brain import SORT_SCHEMA, fallback_card, normalize_card
from polka.config import Config
from polka.db import Store
from polka.flow import Flow

TOKEN = "123456:TEST-TOKEN-NOT-REAL"


def make_config(**overrides) -> Config:
    base = dict(
        bot_token=TOKEN, chat_id="42", anthropic_key="", model="claude-opus-5",
        openai_key="", capture_token="secret", public_url="", host="127.0.0.1",
        port=0, quiet_from=0, quiet_to=0, timezone_offset=3,
        telegram_api="https://api.telegram.org",
    )
    base.update(overrides)
    return Config(**base)



def quiet_window_around_now(offset_hours: int = 3) -> dict[str, int]:
    """Окно тишины, гарантированно накрывающее текущий момент."""
    from datetime import datetime, timedelta, timezone as tz
    hour = datetime.now(tz(timedelta(hours=offset_hours))).hour
    return {"quiet_from": hour, "quiet_to": (hour + 1) % 24}

class FakeBrain:
    """Мозг с заранее заданным ответом: тесты не ходят в сеть."""

    enabled = True

    def __init__(self, card):
        self.card = card

    async def sort(self, text, shelves):
        return normalize_card(dict(self.card), text)

    async def to_claude_prompt(self, title, text):
        return f"# {title}\n\n{text}"


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []
        self.docs: list[tuple[str, str]] = []

    async def send(self, text, buttons=None, silent=False, chat_id=None):
        self.sent.append(text)
        return len(self.sent)

    async def send_document(self, filename, content, caption="", chat_id=None):
        self.docs.append((filename, content))


class CardTests(unittest.TestCase):
    def test_fallback_never_empty(self):
        card = fallback_card("  ")
        self.assertTrue(card["title"])
        self.assertEqual(card["shelf"], "inbox")

    def test_normalize_clamps_out_of_range(self):
        card = normalize_card({
            "importance": 99, "kind": "чтототакое", "shelf": "  ИДЕИ  ",
            "reminder": {"mode": "every", "interval_min": 1, "delay_min": -5},
            "tags": ["A", "  ", "b" * 50, "c", "d", "e"],
        }, "текст")
        self.assertEqual(card["importance"], 5)
        self.assertEqual(card["kind"], "note")
        self.assertEqual(card["shelf"], "идеи")
        self.assertEqual(card["reminder"]["interval_min"], 15)   # чаще 15 минут не звеним
        self.assertEqual(card["reminder"]["delay_min"], 0)
        self.assertEqual(len(card["tags"]), 4)

    def test_normalize_keeps_valid_values(self):
        card = normalize_card({
            "title": "Продлить домен", "summary": "истекает в пятницу",
            "shelf": "work", "kind": "task", "importance": 4,
            "needs_reminder": True, "ask_user": False, "claude_worthy": False,
            "reminder": {"mode": "once", "interval_min": 0, "delay_min": 120},
            "tags": ["домен"],
        }, "сырой текст")
        self.assertEqual(card["title"], "Продлить домен")
        self.assertEqual(card["importance"], 4)
        self.assertEqual(card["reminder"]["delay_min"], 120)


class SchemaTests(unittest.TestCase):
    """Строгая схема structured outputs принимает не любой JSON Schema.

    minimum и maximum у чисел она отвергает с 400, и разбор молча уходил
    в резерв: карточка приходила, но полка всегда была «Входящее».
    """

    UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                   "multipleOf", "minLength", "maxLength", "pattern",
                   "minItems", "maxItems", "uniqueItems"}

    def walk(self, node, path="schema"):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in self.UNSUPPORTED:
                    self.fail(f"{path}: ключ {key} строгая схема не принимает")
                self.walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                self.walk(value, f"{path}[{index}]")

    def test_schema_has_no_unsupported_keywords(self):
        self.walk(SORT_SCHEMA)

    def test_objects_are_closed_and_fully_required(self):
        def check(node, path="schema"):
            if isinstance(node, dict) and node.get("type") == "object":
                properties = set(node.get("properties", {}))
                self.assertIs(node.get("additionalProperties"), False,
                              f"{path}: объект должен быть закрыт")
                self.assertEqual(set(node.get("required", [])), properties,
                                 f"{path}: обязательными должны быть все поля")
                for name, value in node.get("properties", {}).items():
                    check(value, f"{path}.{name}")
        check(SORT_SCHEMA)

    def test_importance_is_limited_to_five_levels(self):
        self.assertEqual(SORT_SCHEMA["properties"]["importance"]["enum"], [1, 2, 3, 4, 5])


class InitDataTests(unittest.TestCase):
    def sign(self, fields: dict[str, str]) -> str:
        check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode({**fields, "hash": digest})

    def test_valid_signature_passes(self):
        data = self.sign({"auth_date": str(int(time.time())), "user": '{"id":42}'})
        self.assertIsNotNone(check_init_data(data, TOKEN))

    def test_tampered_payload_rejected(self):
        data = self.sign({"auth_date": str(int(time.time())), "user": '{"id":42}'})
        self.assertIsNone(check_init_data(data.replace("42", "43"), TOKEN))

    def test_stale_auth_date_rejected(self):
        old = str(int(time.time()) - 48 * 3600)
        self.assertIsNone(check_init_data(self.sign({"auth_date": old}), TOKEN))

    def test_missing_hash_rejected(self):
        self.assertIsNone(check_init_data("auth_date=1&user=x", TOKEN))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")
        self.tg = FakeTelegram()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def flow(self, card, **cfg_overrides) -> Flow:
        return Flow(make_config(**cfg_overrides), self.store,
                    FakeBrain(card), self.tg)

    async def test_capture_puts_thought_on_shelf(self):
        flow = self.flow({
            "title": "Продлить домен", "summary": "истекает в пятницу",
            "shelf": "work", "kind": "task", "importance": 4,
            "needs_reminder": True, "ask_user": False, "claude_worthy": False,
            "reminder": {"mode": "once", "interval_min": 0, "delay_min": 60},
            "tags": [],
        })
        thought = await flow.capture("продлить домен", source="cli")
        self.assertEqual(thought["shelf"], "work")
        self.assertEqual(thought["importance"], 4)
        reminders = await self.store.reminders_for(thought["id"])
        self.assertEqual(len(reminders), 1)
        self.assertGreater(reminders[0]["next_fire_at"], time.time())
        self.assertEqual(len(self.tg.sent), 1)

    async def test_capture_creates_new_shelf_on_demand(self):
        flow = self.flow({**fallback_card("x"), "shelf": "muzyka",
                          "new_shelf_title": "Музыка"})
        await flow.capture("послушать новый альбом", source="cli")
        keys = [s["key"] for s in await self.store.list_shelves()]
        self.assertIn("muzyka", keys)

    async def test_ask_user_defers_reminder(self):
        flow = self.flow({**fallback_card("x"), "needs_reminder": True,
                          "ask_user": True,
                          "reminder": {"mode": "once", "interval_min": 0,
                                       "delay_min": 60}})
        thought = await flow.capture("что-то важное", source="cli")
        self.assertEqual(await self.store.reminders_for(thought["id"]), [])

    async def test_critical_reminder_escalates_then_stops(self):
        flow = self.flow({**fallback_card("x"), "importance": 5})
        thought = await flow.capture("нельзя потерять", source="cli")
        rid = await self.store.add_reminder(thought["id"], "once", 0, time.time() - 1)

        fired = 0
        for _ in range(5):
            due = await self.store.due_reminders(time.time() + 10 * 3600)
            if not due:
                break
            await flow.fire_reminder(due[0])
            fired += 1
        # три ступени эскалации: 15, 25, 45 минут, потом напоминание закрывается
        self.assertEqual(fired, 4)
        rows = await self.store.query("SELECT active FROM reminders WHERE id = ?", (rid,))
        self.assertEqual(rows[0]["active"], 0)

    async def test_low_importance_fires_once(self):
        flow = self.flow({**fallback_card("x"), "importance": 2})
        thought = await flow.capture("мелочь", source="cli")
        await self.store.add_reminder(thought["id"], "once", 0, time.time() - 1)
        due = await self.store.due_reminders(time.time())
        await flow.fire_reminder(due[0])
        self.assertEqual(await self.store.due_reminders(time.time() + 10 * 3600), [])

    async def test_acked_reminder_does_not_fire(self):
        flow = self.flow({**fallback_card("x"), "importance": 5})
        thought = await flow.capture("сделано", source="cli")
        rid = await self.store.add_reminder(thought["id"], "once", 0, time.time() - 1)
        await self.store.ack_reminder(rid)
        self.assertEqual(await self.store.due_reminders(time.time()), [])

    async def test_quiet_hours_postpone_non_critical(self):
        flow = self.flow({**fallback_card("x"), "importance": 3},
                         **quiet_window_around_now())
        thought = await flow.capture("подождёт до утра", source="cli")
        await self.store.add_reminder(thought["id"], "once", 0, time.time() - 1)
        due = await self.store.due_reminders(time.time())
        await flow.fire_reminder(due[0])
        self.assertEqual(self.tg.sent[1:], [])          # первое - карточка захвата
        rows = await self.store.query("SELECT next_fire_at FROM reminders")
        self.assertGreater(rows[0]["next_fire_at"], time.time())

    async def test_quiet_hours_do_not_hold_critical(self):
        flow = self.flow({**fallback_card("x"), "importance": 5},
                         **quiet_window_around_now())
        thought = await flow.capture("пожар", source="cli")
        await self.store.add_reminder(thought["id"], "once", 0, time.time() - 1)
        due = await self.store.due_reminders(time.time())
        await flow.fire_reminder(due[0])
        self.assertEqual(len(self.tg.sent), 2)

    async def test_preset_replaces_previous_reminder(self):
        flow = self.flow(fallback_card("x"))
        thought = await flow.capture("мысль", source="cli")
        await flow.set_reminder_preset(thought["id"], "5h")
        await flow.set_reminder_preset(thought["id"], "day")
        active = await self.store.reminders_for(thought["id"])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["interval_min"], 1440)

    async def test_preset_off_clears_reminders(self):
        flow = self.flow(fallback_card("x"))
        thought = await flow.capture("мысль", source="cli")
        await flow.set_reminder_preset(thought["id"], "1h")
        await flow.set_reminder_preset(thought["id"], "off")
        self.assertEqual(await self.store.reminders_for(thought["id"]), [])

    async def test_prompt_is_built_once_and_cached(self):
        flow = self.flow({**fallback_card("x"), "claude_worthy": True})
        thought = await flow.capture("сделать приложение для мыслей", source="cli")
        first = await flow.build_prompt(thought["id"])
        stored = await self.store.get_thought(thought["id"])
        self.assertEqual(stored["claude_prompt"], first)
        self.assertEqual(await flow.build_prompt(thought["id"]), first)

    async def test_done_thought_closes_its_reminder(self):
        flow = self.flow({**fallback_card("x"), "importance": 5})
        thought = await flow.capture("уже не надо", source="cli")
        await self.store.add_reminder(thought["id"], "once", 0, time.time() - 1)
        await self.store.update_thought(thought["id"], status="done")
        due = await self.store.due_reminders(time.time())
        await flow.fire_reminder(due[0])
        self.assertEqual(len(self.tg.sent), 1)          # только карточка захвата
        self.assertEqual(await self.store.due_reminders(time.time()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
