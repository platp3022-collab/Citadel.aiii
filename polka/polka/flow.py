"""Логика Полки: захват мысли, карточка, напоминания, экспорт в Claude Code."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .brain import Brain
from .config import Config
from .db import Store
from .telegram import Telegram, esc

log = logging.getLogger("polka.flow")

# Насколько «горит» мысль. Пятёрка светится, единица лежит тихо.
IMPORTANCE_WORD = {
    1: "мимолётное",
    2: "обычное",
    3: "стоит вернуться",
    4: "важное",
    5: "нельзя потерять",
}

# Пресеты напоминаний: код -> (подпись, режим, интервал в минутах, задержка первого)
REMINDER_PRESETS: dict[str, tuple[str, str, int, int]] = {
    "1h":    ("через час",      "once",  0,    60),
    "eve":   ("вечером",        "once",  0,    -1),      # -1: считаем до 19:00
    "morn":  ("завтра утром",   "once",  0,    -2),      # -2: считаем до 09:00
    "5h":    ("каждые 5 часов", "every", 300,  300),
    "day":   ("каждый день",    "every", 1440, 1440),
    "off":   ("без напоминания", "once", 0,    0),
}

# Сколько раз повторить, если не подтвердили, и через сколько минут.
ESCALATION_STEPS: dict[int, list[int]] = {
    5: [15, 25, 45],
    4: [30, 60],
    3: [90],
}
MAX_ESCALATION = 3


class Flow:
    def __init__(self, cfg: Config, store: Store, brain: Brain, tg: Telegram):
        self.cfg = cfg
        self.store = store
        self.brain = brain
        self.tg = tg

    # ── время ─────────────────────────────────────────────────────────────
    def _tz(self) -> timezone:
        return timezone(timedelta(hours=self.cfg.timezone_offset))

    def local_now(self) -> datetime:
        return datetime.now(self._tz())

    def _next_local_hour(self, hour: int) -> float:
        """Ближайший момент, когда локальные часы покажут указанный час."""
        now = self.local_now()
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    def in_quiet_hours(self, at: float) -> bool:
        hour = datetime.fromtimestamp(at, self._tz()).hour
        start, end = self.cfg.quiet_from, self.cfg.quiet_to
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end       # окно через полночь

    def after_quiet_hours(self, at: float) -> float:
        moment = datetime.fromtimestamp(at, self._tz())
        target = moment.replace(hour=self.cfg.quiet_to, minute=0, second=0, microsecond=0)
        if target <= moment:
            target += timedelta(days=1)
        return target.timestamp()

    # ── захват ────────────────────────────────────────────────────────────
    async def capture(self, text: str, source: str = "telegram",
                      notify: bool = True) -> dict[str, Any]:
        """Принять мысль, разложить по полке, при необходимости завести напоминание."""
        text = (text or "").strip()
        if not text:
            raise ValueError("пустая мысль")

        thought_id = await self.store.add_thought(text, source=source)
        shelves = await self.store.list_shelves()
        card = await self.brain.sort(text, shelves)

        shelf_key = await self.store.ensure_shelf(
            card["shelf"], card.get("new_shelf_title") or "", ""
        )
        await self.store.update_thought(
            thought_id,
            title=card["title"],
            summary=card["summary"],
            shelf=shelf_key,
            kind=card["kind"],
            importance=card["importance"],
            tags=card["tags"],
            claude_worthy=int(card["claude_worthy"]),
            pending=json.dumps(card["reminder"], ensure_ascii=False)
            if card["needs_reminder"] else "",
        )

        if card["needs_reminder"] and not card["ask_user"]:
            await self.schedule_from_card(thought_id, card["reminder"])

        thought = await self.store.get_thought(thought_id)
        if notify and thought:
            await self.tg.send(
                self.render_card(thought, card),
                buttons=self.card_buttons(thought_id, thought["importance"],
                                          bool(thought["claude_worthy"])),
            )
        return thought or {"id": thought_id}

    async def schedule_from_card(self, thought_id: str,
                                 reminder: dict[str, Any]) -> str | None:
        delay = int(reminder.get("delay_min") or 0)
        mode = reminder.get("mode", "once")
        interval = int(reminder.get("interval_min") or 0)
        if mode == "once" and delay <= 0:
            return None
        first = time.time() + max(delay, 1) * 60
        await self.store.drop_reminders(thought_id)
        return await self.store.add_reminder(thought_id, mode, interval, first)

    async def set_reminder_preset(self, thought_id: str, code: str) -> str:
        """Поставить напоминание по кнопке. Возвращает подпись для ответа."""
        label, mode, interval, delay = REMINDER_PRESETS.get(code, REMINDER_PRESETS["off"])
        await self.store.drop_reminders(thought_id)
        await self.store.update_thought(thought_id, pending="")
        if code == "off":
            return "без напоминания"
        if delay == -1:
            first = self._next_local_hour(19)
        elif delay == -2:
            first = self._next_local_hour(9)
        else:
            first = time.time() + max(delay, 1) * 60
        await self.store.add_reminder(thought_id, mode, interval, first)
        return label

    # ── отрисовка ─────────────────────────────────────────────────────────
    def render_card(self, thought: dict[str, Any],
                    card: dict[str, Any] | None = None) -> str:
        importance = int(thought["importance"])
        lines = [f"<b>{esc(thought['title'])}</b>"]
        if thought.get("summary"):
            lines.append(esc(thought["summary"]))
        lines.append("")
        lines.append(
            f"полка: <code>{esc(thought['shelf'])}</code>   "
            f"важность: {importance}/5, {IMPORTANCE_WORD[importance]}"
        )
        if card and card.get("needs_reminder") and card.get("ask_user"):
            lines.append("")
            lines.append("Когда напомнить?")
        return "\n".join(lines)

    def card_buttons(self, thought_id: str, importance: int,
                     claude_worthy: bool) -> list[list[dict[str, Any]]]:
        marks = [
            {"text": ("•" if n == importance else "") + str(n),
             "callback_data": f"imp:{thought_id}:{n}"}
            for n in range(1, 6)
        ]
        rows = [marks, [
            {"text": "через час", "callback_data": f"rem:{thought_id}:1h"},
            {"text": "вечером", "callback_data": f"rem:{thought_id}:eve"},
            {"text": "завтра", "callback_data": f"rem:{thought_id}:morn"},
        ], [
            {"text": "каждые 5 часов", "callback_data": f"rem:{thought_id}:5h"},
            {"text": "каждый день", "callback_data": f"rem:{thought_id}:day"},
        ]]
        tail = [{"text": "не напоминать", "callback_data": f"rem:{thought_id}:off"}]
        if claude_worthy:
            tail.append({"text": "развернуть в промт",
                         "callback_data": f"prompt:{thought_id}"})
        rows.append(tail)
        if self.cfg.public_url:
            rows.append([{"text": "Открыть полку",
                          "web_app": {"url": self.cfg.public_url}}])
        return rows

    # ── доставка напоминаний ──────────────────────────────────────────────
    async def fire_reminder(self, reminder: dict[str, Any]) -> None:
        """Отправить напоминание и назначить следующий заход, если его не подтвердят."""
        thought_id = reminder["thought_id"]
        thought = await self.store.get_thought(thought_id)
        if not thought or thought["status"] in ("done", "deleted"):
            await self.store.ack_reminder(reminder["id"])
            return

        importance = int(thought["importance"])
        escalation = int(reminder["escalation"]) + 1
        now = time.time()

        # Ночью будим только то, что нельзя терять.
        if self.in_quiet_hours(now) and importance < 5:
            await self.store.mark_fired(reminder["id"], self.after_quiet_hours(now),
                                        int(reminder["escalation"]))
            return

        await self.tg.send(
            self.render_reminder(thought, escalation),
            buttons=self.reminder_buttons(reminder["id"], thought_id),
        )
        # Второе короткое сообщение даёт вторую вибрацию: так труднее пролистать.
        if importance == 5 and escalation >= 2:
            await self.tg.send(f"↑ <b>{esc(thought['title'])}</b>")

        await self.store.log_fire(reminder["id"], thought_id, escalation)

        next_at = self._next_fire(reminder, importance, escalation)
        await self.store.mark_fired(reminder["id"], next_at, escalation)

    def _next_fire(self, reminder: dict[str, Any], importance: int,
                   escalation: int) -> float | None:
        """Когда напомнить снова. None - напоминание закрывается."""
        now = time.time()
        if reminder["mode"] == "every":
            interval = max(15, int(reminder["interval_min"] or 15))
            base = now + interval * 60
            # Важное без подтверждения переспрашивает раньше, чем через полный интервал.
            steps = ESCALATION_STEPS.get(importance, [])
            if steps and escalation <= len(steps):
                base = min(base, now + steps[escalation - 1] * 60)
            return self.after_quiet_hours(base) if self.in_quiet_hours(base) and importance < 5 else base

        steps = ESCALATION_STEPS.get(importance, [])
        if escalation > min(len(steps), MAX_ESCALATION) or not steps:
            return None
        return now + steps[escalation - 1] * 60

    def render_reminder(self, thought: dict[str, Any], escalation: int) -> str:
        importance = int(thought["importance"])
        head = {
            1: "Напоминаю",
            2: "Напоминаю",
            3: "Ты просил вернуться к этому",
            4: "Это важное",
            5: "Это нельзя потерять",
        }[importance]
        if escalation > 1:
            head = f"{head}. Повтор {escalation}"
        lines = [f"<b>{esc(head)}</b>", "", f"<b>{esc(thought['title'])}</b>"]
        body = thought.get("summary") or thought.get("raw") or ""
        if body and body != thought["title"]:
            lines.append(esc(body[:600]))
        if escalation >= 2:
            lines.append("")
            lines.append("Нажми кнопку, иначе я вернусь ещё раз.")
        return "\n".join(lines)

    def reminder_buttons(self, reminder_id: str,
                         thought_id: str) -> list[list[dict[str, Any]]]:
        return [[
            {"text": "сделал", "callback_data": f"ack:{reminder_id}"},
            {"text": "+1 час", "callback_data": f"snz:{reminder_id}:60"},
            {"text": "+1 день", "callback_data": f"snz:{reminder_id}:1440"},
        ], [
            {"text": "больше не напоминать", "callback_data": f"stop:{reminder_id}"},
        ]]

    # ── Claude Code ───────────────────────────────────────────────────────
    async def build_prompt(self, thought_id: str) -> str:
        thought = await self.store.get_thought(thought_id)
        if not thought:
            raise KeyError(thought_id)
        if thought["claude_prompt"]:
            return thought["claude_prompt"]
        prompt = await self.brain.to_claude_prompt(thought["title"], thought["raw"])
        await self.store.update_thought(thought_id, claude_prompt=prompt)
        return prompt

    # ── сводка ────────────────────────────────────────────────────────────
    async def burning(self) -> str:
        rows = await self.store.query(
            "SELECT * FROM thoughts WHERE status = 'shelved' AND importance >= 4"
            " ORDER BY importance DESC, created_at DESC LIMIT 12"
        )
        if not rows:
            return "Ничего не горит. Полки в порядке."
        lines = ["<b>Горит прямо сейчас</b>", ""]
        for row in rows:
            lines.append(f"{row['importance']}/5  <b>{esc(row['title'])}</b>")
            if row["summary"]:
                lines.append(f"     {esc(row['summary'][:120])}")
        return "\n".join(lines)
